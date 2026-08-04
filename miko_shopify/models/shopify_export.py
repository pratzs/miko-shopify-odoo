# -*- coding: utf-8 -*-
"""Pushing products and customers the other way, Odoo to Shopify.

Which direction each thing travels is the store owner's decision, not ours, and
the three answers are all legitimate. A business whose catalogue lives in Odoo
pushes out. One that merchandises in Shopify pulls in. One that does both wants
both. Picking for them means being wrong for two thirds of them.

**Direction is enforced, not suggested.** Asking to push products when the store
is set to pull refuses with a sentence saying so, rather than quietly doing it.
Somebody sets the direction precisely so the other way cannot happen by accident.

**Nothing is created twice.** An export checks the mapping table first, exactly
like an import: if the record already has a Shopify id it is updated, never
created again. A second Shopify product for something already listed is as bad as
a duplicate order and much harder to notice.

**A word of caution that belongs in the code.** These mutations are pinned to the
2025-07 Admin API. The tests drive them against a stubbed transport, which proves
the calling code, the direction rules and the error handling, but it cannot prove
Shopify accepts the exact input shape. Run a sandbox store through this before
relying on it.
"""
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PRODUCT_CREATE = """
mutation ($product: ProductCreateInput!) {
  productCreate(product: $product) {
    product { id handle }
    userErrors { field message }
  }
}
"""

PRODUCT_UPDATE = """
mutation ($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id handle }
    userErrors { field message }
  }
}
"""

CUSTOMER_CREATE = """
mutation ($input: CustomerInput!) {
  customerCreate(input: $input) {
    customer { id }
    userErrors { field message }
  }
}
"""

CUSTOMER_UPDATE = """
mutation ($input: CustomerInput!) {
  customerUpdate(input: $input) {
    customer { id }
    userErrors { field message }
  }
}
"""

DIRECTIONS = [
    ('in', 'Shopify to Odoo'),
    ('out', 'Odoo to Shopify'),
    ('both', 'Both ways'),
]


def user_errors(block):
    """Shopify reports refusals in userErrors, alongside HTTP 200.

    Every mutation has to be checked this way. A response that looks completely
    valid and contains a refusal is the most expensive shape of failure, because
    the caller reports success.
    """
    return "; ".join(e.get('message', '') for e in (block or {}).get('userErrors') or [])


class MikoEcommerceChannel(models.Model):
    _inherit = 'miko.ecommerce.channel'

    product_direction = fields.Selection(
        DIRECTIONS, default='in', required=True, string='Products',
        help="Which way product data travels. Orders are always Shopify to Odoo: "
             "that is where they are placed.")
    customer_direction = fields.Selection(
        DIRECTIONS, default='in', required=True, string='Customers')

    # ------------------------------------------------------------------
    def _require_direction(self, setting, wanted, what):
        """Refuse politely when this is not the direction the store chose."""
        self.ensure_one()
        value = self[setting]
        if value == wanted or value == 'both':
            return True
        raise UserError(_(
            "%(what)s on %(store)s is set to '%(current)s', so it cannot be sent "
            "the other way.\n\nChange it on the store's Directions tab if that is "
            "what you want.") % {
                'what': what, 'store': self.name,
                'current': dict(DIRECTIONS).get(value, value)})

    # ------------------------------------- products out
    def action_export_products(self):
        for channel in self:
            channel._export_shopify_products()
        return True

    def _exportable_products(self):
        """Products this store should push. Only ones marked for sale."""
        self.ensure_one()
        return self.env['product.template'].search([
            ('sale_ok', '=', True),
            '|', ('company_id', '=', self.company_id.id), ('company_id', '=', False),
        ])

    def _export_shopify_products(self):
        self.ensure_one()
        self._require_direction('product_direction', 'out', _("Products"))
        count = 0
        for template in self._exportable_products():
            job = self.env['miko.ecommerce.job'].enqueue(
                self, 'export_product', None, {'template_id': template.id},
                external_ref=template.name, direction='out')
            try:
                self._job_export_product(job.get_payload(), job)
            except Exception as err:        # noqa: BLE001 - kept, not lost
                _logger.exception("miko_shopify: export of %s failed", template.name)
                job.mark_failed(err, blocked=job._is_blocking(err),
                                guidance=job._guidance_for(err))
            else:
                job.mark_done(template)
                count += 1
        self._touch_sync()
        return count

    def _job_export_product(self, payload, job=None):
        """Create or update one product in Shopify. Re-runnable."""
        self.ensure_one()
        template = self.env['product.template'].browse(
            (payload or {}).get('template_id') or 0).exists()
        if not template:
            raise UserError(_(
                "The Odoo product this job refers to no longer exists. The job can "
                "be deleted."))

        Mapping = self.env['miko.ecommerce.mapping']
        row = Mapping.search([
            ('channel_id', '=', self.id),
            ('model_name', '=', 'product.template'),
            ('odoo_id', '=', template.id),
        ], limit=1)

        body = {'title': template.name}
        body.update(self._apply_maps_out('product', template))

        client = self._shopify_client()
        if row:
            body['id'] = row.external_id
            data = client.call(PRODUCT_UPDATE, {'product': body})
            block = data.get('productUpdate') or {}
        else:
            data = client.call(PRODUCT_CREATE, {'product': body})
            block = data.get('productCreate') or {}

        errors = user_errors(block)
        if errors:
            raise UserError(_("Shopify refused the product '%(name)s': %(err)s")
                            % {'name': template.name, 'err': errors})

        created = (block.get('product') or {})
        if created.get('id'):
            Mapping.link(self, template, created['id'], created.get('handle'))
        return template

    # ------------------------------------- customers out
    def action_export_customers(self):
        for channel in self:
            channel._export_shopify_customers()
        return True

    def _exportable_customers(self):
        """Contacts worth pushing: real customers with an email.

        Shopify identifies a customer by email, so one without an email cannot be
        matched later and would create a new record on every run.
        """
        self.ensure_one()
        return self.env['res.partner'].search([
            ('customer_rank', '>', 0),
            ('email', '!=', False),
            ('parent_id', '=', False),
            '|', ('company_id', '=', self.company_id.id), ('company_id', '=', False),
        ])

    def _export_shopify_customers(self):
        self.ensure_one()
        self._require_direction('customer_direction', 'out', _("Customers"))
        count = 0
        for partner in self._exportable_customers():
            job = self.env['miko.ecommerce.job'].enqueue(
                self, 'export_customer', None, {'partner_id': partner.id},
                external_ref=partner.name, direction='out')
            try:
                self._job_export_customer(job.get_payload(), job)
            except Exception as err:        # noqa: BLE001 - kept, not lost
                _logger.exception("miko_shopify: export of %s failed", partner.name)
                job.mark_failed(err, blocked=job._is_blocking(err),
                                guidance=job._guidance_for(err))
            else:
                job.mark_done(partner)
                count += 1
        self._touch_sync()
        return count

    def _job_export_customer(self, payload, job=None):
        self.ensure_one()
        partner = self.env['res.partner'].browse(
            (payload or {}).get('partner_id') or 0).exists()
        if not partner:
            raise UserError(_(
                "The Odoo contact this job refers to no longer exists. The job can "
                "be deleted."))
        if not partner.email:
            raise UserError(_(
                "%s has no email address. Shopify identifies customers by email, "
                "so without one this contact would be created again on every "
                "single run.") % partner.display_name)

        Mapping = self.env['miko.ecommerce.mapping']
        row = Mapping.search([
            ('channel_id', '=', self.id),
            ('model_name', '=', 'res.partner'),
            ('odoo_id', '=', partner.id),
        ], limit=1)

        first, _sep, last = (partner.name or '').partition(' ')
        body = {'firstName': first or partner.name, 'lastName': last or ''}
        body.update(self._apply_maps_out('customer', partner))

        client = self._shopify_client()
        if row:
            body['id'] = row.external_id
            data = client.call(CUSTOMER_UPDATE, {'input': body})
            block = data.get('customerUpdate') or {}
        else:
            data = client.call(CUSTOMER_CREATE, {'input': body})
            block = data.get('customerCreate') or {}

        errors = user_errors(block)
        if errors:
            raise UserError(_("Shopify refused the contact '%(name)s': %(err)s")
                            % {'name': partner.display_name, 'err': errors})

        created = (block.get('customer') or {})
        if created.get('id'):
            Mapping.link(self, partner, created['id'], partner.email)
        return partner
