# -*- coding: utf-8 -*-
"""Shopify orders as Odoo sales orders.

The one thing an order import must never do is create the same order twice, and
the second thing is never to create one whose total silently disagrees with what
the customer was actually charged.

Both are handled explicitly. Identity goes through the mapping table before
anything is written, so a re-run, a crashed sync, or two overlapping schedules all
converge on the same single order. And once the order is built, the Odoo total is
compared against the figure Shopify reported. If they disagree by more than
currency rounding, the order is left in draft and flagged, because a difference
here means a discount, a fee or a tax was not carried across, and an invoice sent
on that basis is wrong in a way that reaches the customer.
"""
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

MONEY = 'shopMoney { amount currencyCode }'

ORDER_QUERY = """
query ($first: Int!, $after: String, $filter: String) {
  orders(first: $first, after: $after, query: $filter, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id name createdAt note cancelledAt displayFinancialStatus
      currentTotalPriceSet { %(money)s }
      customer { id firstName lastName
                 defaultEmailAddress { emailAddress }
                 defaultPhoneNumber { phoneNumber } }
      shippingAddress { firstName lastName company address1 address2 city
                        provinceCode zip countryCodeV2 phone }
      shippingLines(first: 10) { edges { node {
        title
        originalPriceSet { %(money)s }
        taxLines { title rate priceSet { %(money)s } }
      } } }
      lineItems(first: 250) { edges { node {
        id title quantity sku
        variant { id }
        originalUnitPriceSet { %(money)s }
        discountedUnitPriceSet { %(money)s }
        taxLines { title rate priceSet { %(money)s } }
      } } }
    } }
  }
}
""" % {'money': MONEY}


def _amount(money_set):
    """A money set from Shopify as a float, defaulting to zero."""
    try:
        return float(((money_set or {}).get('shopMoney') or {}).get('amount') or 0.0)
    except (TypeError, ValueError):
        return 0.0


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    shopify_order_name = fields.Char(
        readonly=True, copy=False, index=True,
        help="The order number as the customer sees it in Shopify, such as #1042.")
    shopify_total = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="What Shopify said the customer was charged.")
    shopify_total_matches = fields.Boolean(
        readonly=True, copy=False, default=True,
        help="False when the Odoo total does not agree with the Shopify total. "
             "An order in that state is not safe to invoice until the difference "
             "is understood.")


class MikoEcommerceChannel(models.Model):
    _inherit = 'miko.ecommerce.channel'

    def action_import_orders(self):
        for channel in self:
            channel._import_shopify_orders()
        return True

    # ------------------------------------------------------------------
    def _import_shopify_orders(self):
        self.ensure_one()
        client = self._shopify_client()
        Job = self.env['miko.ecommerce.job']
        Mapping = self.env['miko.ecommerce.mapping']

        filter_parts = ['status:any']
        if self.import_from_date:
            filter_parts.append("created_at:>='%sZ'" % fields.Datetime.to_string(
                self.import_from_date).replace(' ', 'T'))

        count = 0
        for node in client.paginate(ORDER_QUERY, {'filter': ' AND '.join(filter_parts)},
                                    'orders', page_size=25):
            job = Job.enqueue(self, 'import_order', node.get('id'), node)

            # Checked before anything is written, not after. This single line is
            # what makes a re-run harmless.
            if Mapping.already_imported(self, 'sale.order', node['id']):
                job.mark_skipped(_("Already imported."))
                continue
            try:
                order = self._job_import_order(node, job)
                job.mark_done(order)
                count += 1
            except Exception as err:            # noqa: BLE001 - kept, not lost
                _logger.exception("miko_shopify: order %s failed", node.get('name'))
                job.mark_failed(err, blocked=job._is_blocking(err),
                                guidance=job._guidance_for(err))
        self._touch_sync()
        return count

    def _job_import_order(self, payload, job=None):
        """Import one order, skipping it if it already arrived.

        The already-imported check lives here rather than only in the loop, so a
        Retry on a job that actually succeeded cannot create a second order.
        """
        self.ensure_one()
        Mapping = self.env['miko.ecommerce.mapping']
        existing = Mapping.find_odoo_record(self, 'sale.order', payload.get('id'))
        if existing:
            return existing
        return self._import_one_shopify_order(payload)

    # ------------------------------------------------------------------
    def _import_one_shopify_order(self, node):
        self.ensure_one()
        Mapping = self.env['miko.ecommerce.mapping']
        currency = self._shopify_currency()

        partner = self._shopify_order_partner(node)
        values = {
            'partner_id': partner.id,
            'company_id': self.company_id.id,
            'currency_id': currency.id,
            'origin': node.get('name') or False,
            'client_order_ref': node.get('name') or False,
            'shopify_order_name': node.get('name') or False,
            'shopify_total': _amount(node.get('currentTotalPriceSet')),
            'date_order': self._shopify_datetime(node.get('createdAt')),
            'order_line': self._shopify_order_lines(node),
        }
        if self.team_id:
            values['team_id'] = self.team_id.id
        if self.warehouse_id:
            values['warehouse_id'] = self.warehouse_id.id
        if self.pricelist_id:
            values['pricelist_id'] = self.pricelist_id.id

        order = self.env['sale.order'].create(values)
        Mapping.link(self, order, node['id'], node.get('name'))

        self._verify_shopify_total(order, node)

        if node.get('cancelledAt'):
            # Cancelled in Shopify: import it for the record, but never let it
            # flow into confirmation, delivery or invoicing.
            order.message_post(body=_("Cancelled in Shopify before import."))
            return order

        if self.auto_confirm_orders and order.shopify_total_matches:
            order.action_confirm()
            if self.auto_create_invoice:
                self._invoice_shopify_order(order)
        return order

    def _verify_shopify_total(self, order, node):
        """Refuse to pretend the numbers agree when they do not."""
        expected = _amount(node.get('currentTotalPriceSet'))
        actual = order.amount_total
        rounding = order.currency_id.rounding or 0.01
        if abs(expected - actual) <= max(rounding, 0.01):
            return True
        order.shopify_total_matches = False
        order.message_post(body=_(
            "<p><b>This order was not imported at the price the customer paid.</b></p>"
            "<p>Shopify charged %(expected)s. This order adds up to %(actual)s, a "
            "difference of %(diff)s.</p>"
            "<p>Usually a discount, a shipping fee or a tax that has no Odoo "
            "equivalent configured yet. It has been left as a quotation rather "
            "than confirmed, because invoicing it would bill the wrong amount.</p>"
        ) % {
            'expected': '%.2f' % expected,
            'actual': '%.2f' % actual,
            'diff': '%.2f' % (expected - actual),
        })
        _logger.warning("miko_shopify: total mismatch on %s: shopify %.2f, odoo %.2f",
                        node.get('name'), expected, actual)
        return False

    def _invoice_shopify_order(self, order):
        invoice = order._create_invoices()
        if invoice and self.journal_id:
            invoice.journal_id = self.journal_id
        return invoice

    # ------------------------------------------------------------------
    @staticmethod
    def _shopify_datetime(value):
        """An ISO 8601 timestamp from Shopify as a naive UTC datetime."""
        if not value:
            return fields.Datetime.now()
        text = str(value).replace('T', ' ').replace('Z', '')
        if '+' in text:
            text = text.split('+')[0]
        return fields.Datetime.to_datetime(text.split('.')[0].strip()) \
            or fields.Datetime.now()

    def _shopify_order_partner(self, node):
        """Who to bill. Never guessed, never left blank."""
        customer = node.get('customer')
        if customer and customer.get('id'):
            partner = self.env['miko.ecommerce.mapping'].find_odoo_record(
                self, 'res.partner', customer['id'])
            if not partner:
                from .shopify_customer import flatten_customer
                partner = self._upsert_shopify_customer(flatten_customer(customer))
            return partner
        if self.default_customer_id:
            return self.default_customer_id     # guest checkout, as configured
        raise UserError(_(
            "Shopify order %s has no customer, and this store has no fallback "
            "customer set. Set one on the store record so guest checkouts have "
            "somewhere to go.") % (node.get('name') or ''))

    # ------------------------------------------------------------------
    def _shopify_order_lines(self, node):
        commands = []
        for edge in ((node.get('lineItems') or {}).get('edges')) or []:
            item = edge.get('node') or {}
            commands.append((0, 0, self._shopify_product_line(item)))
        for edge in ((node.get('shippingLines') or {}).get('edges')) or []:
            line = edge.get('node') or {}
            if _amount(line.get('originalPriceSet')):
                commands.append((0, 0, self._shopify_shipping_line(line)))
        return commands

    def _sol_tax_field(self):
        """The name of the tax field on a sale order line, on this series.

        Odoo 19 renamed sale.order.line.tax_id to tax_ids. Resolved from the
        registry rather than hardcoded, because writing the wrong one does not
        fail loudly: Odoo raises on an unknown field at create time, so the whole
        order import dies on exactly one series and works everywhere else.
        """
        return 'tax_ids' if 'tax_ids' in self.env['sale.order.line']._fields else 'tax_id'

    def _shopify_product_line(self, item):
        product = self._resolve_shopify_product(item)
        unit = _amount(item.get('originalUnitPriceSet'))
        net = _amount(item.get('discountedUnitPriceSet'))

        # The discount is kept as a discount rather than folded into the price,
        # so the order still shows what the product normally sells for and every
        # margin report downstream stays truthful.
        discount = 0.0
        if unit and net < unit:
            discount = round((unit - net) / unit * 100.0, 4)

        return {
            'product_id': product.id,
            'name': item.get('title') or product.display_name,
            'product_uom_qty': float(item.get('quantity') or 0.0),
            'price_unit': unit or net,
            'discount': discount,
            self._sol_tax_field(): [
                (6, 0, self._resolve_shopify_taxes(item.get('taxLines')).ids)],
        }

    def _shopify_shipping_line(self, line):
        product = self._shopify_delivery_product()
        return {
            'product_id': product.id,
            'name': line.get('title') or _('Shipping'),
            'product_uom_qty': 1.0,
            'price_unit': _amount(line.get('originalPriceSet')),
            self._sol_tax_field(): [
                (6, 0, self._resolve_shopify_taxes(line.get('taxLines')).ids)],
        }

    def _resolve_shopify_taxes(self, tax_lines):
        """Odoo taxes for Shopify's tax lines, honouring the store's policy."""
        Tax = self.env['miko.ecommerce.tax']
        taxes = self.env['account.tax'].browse()
        for tax_line in tax_lines or []:
            if not _amount(tax_line.get('priceSet')) and not tax_line.get('rate'):
                continue
            found = Tax.resolve(self, tax_line)
            if found:
                taxes |= found
            elif self.unmapped_tax_policy == 'block':
                raise UserError(_(
                    "Shopify sent a '%(title)s' tax at %(rate)s%% and there is no "
                    "Odoo tax mapped to it yet.\n\n"
                    "Map it under the store's Taxes tab, then retry this order. "
                    "Importing without it would produce an invoice short by the "
                    "tax amount.") % {
                        'title': tax_line.get('title') or _('Tax'),
                        'rate': round(float(tax_line.get('rate') or 0.0) * 100, 4)})
        return taxes

    def _resolve_shopify_product(self, item):
        """The Odoo product for an order line, in order of how sure we can be."""
        Mapping = self.env['miko.ecommerce.mapping']
        Product = self.env['product.product']

        variant = item.get('variant') or {}
        if variant.get('id'):
            product = Mapping.find_odoo_record(self, 'product.product', variant['id'])
            if product:
                return product

        sku = (item.get('sku') or '').strip()
        if sku:
            product = Product.search([('default_code', '=', sku)], limit=1)
            if product:
                if variant.get('id'):
                    Mapping.link(self, product, variant['id'], sku)
                return product

        if not self.create_missing_products:
            raise UserError(_(
                "Order line '%(title)s'%(sku)s does not match any Odoo product, "
                "and this store is set not to create products.\n\n"
                "Import the catalogue first, or set the SKU in Odoo to match "
                "Shopify.") % {
                    'title': item.get('title') or '?',
                    'sku': ' (SKU %s)' % sku if sku else ''})

        product = Product.create({
            'name': item.get('title') or _('Shopify product'),
            'default_code': sku or False,
            'type': 'consu',
            'list_price': _amount(item.get('originalUnitPriceSet')),
            'sale_ok': True,
        })
        if variant.get('id'):
            Mapping.link(self, product, variant['id'], sku)
        _logger.info("miko_shopify: created product '%s' from an order line",
                     product.display_name)
        return product

    def _shopify_delivery_product(self):
        """One shipping product per store, reused rather than recreated."""
        self.ensure_one()
        reference = 'MIKO-SHIP-%s' % self.id
        product = self.env['product.product'].with_context(active_test=False).search(
            [('default_code', '=', reference)], limit=1)
        if product:
            return product
        return self.env['product.product'].create({
            'name': _('Shipping (%s)') % self.name,
            'default_code': reference,
            'type': 'service',
            'invoice_policy': 'order',
            'list_price': 0.0,
            'sale_ok': True,
            'purchase_ok': False,
        })
