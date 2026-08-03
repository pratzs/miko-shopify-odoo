# -*- coding: utf-8 -*-
"""Telling Shopify an order has actually shipped.

Without this the customer never gets a dispatch email, never gets a tracking
number, and the storefront shows their order unfulfilled for ever. It is the
loudest gap a one-way connector has, because the person who notices is the
customer.

**Two triggers, because businesses differ.** Validating the delivery is the moment
the goods really left, so it is the default. But plenty of businesses never run
Inventory at all, and for them the invoice IS dispatch; waiting for a delivery
that will never exist means their customers are told nothing, ever. The store
chooses.

**The Shopify call lives on the channel**, not on the picking or the invoice. Both
triggers need exactly the same conversation with Shopify, and having it in one
place is the difference between one thing to keep correct and two that drift.

Two defaults are deliberately timid. Nothing is sent until the store switches it
on, because writing into a live storefront is not something a module should start
doing because it was installed. And `notifyCustomer` is false, because turning
this on over a backlog would email real people about orders that shipped weeks
ago, and that cannot be taken back.

Odoo's tracking fields come from the optional `delivery` module and are simply not
present in every install, so they are read through the registry rather than by
attribute.
"""
import logging

from odoo import _, fields, models

_logger = logging.getLogger(__name__)

FULFILMENT_ORDERS = """
query ($id: ID!) {
  order(id: $id) {
    id name
    fulfillmentOrders(first: 20) { edges { node { id status } } }
  }
}
"""

FULFILMENT_CREATE = """
mutation ($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment { id status }
    userErrors { field message }
  }
}
"""

# Only these can still be acted on. Anything else is already shipped, cancelled or
# on hold, and posting against it is an error rather than a duplicate.
OPEN_STATES = {'OPEN', 'IN_PROGRESS', 'SCHEDULED'}


class MikoEcommerceChannel(models.Model):
    _inherit = 'miko.ecommerce.channel'

    fulfil_trigger = fields.Selection(
        [('delivery', 'When the delivery is validated'),
         ('invoice', 'When the invoice is posted'),
         ('both', 'Whichever happens first')],
        default='delivery', required=True, string='Fulfil on',
        help="Validating the delivery is when the goods actually left, so it is "
             "the default. Businesses that do not run Inventory want the invoice "
             "instead, or their customers are never told anything.")
    export_fulfilments = fields.Boolean(
        string='Send fulfilments to Shopify', default=False,
        help="When the trigger fires, mark the Shopify order shipped and pass the "
             "tracking number across. Off by default: this writes into a live "
             "storefront.")
    notify_customer_on_fulfilment = fields.Boolean(
        string='Email the customer', default=False,
        help="Let Shopify send its shipping confirmation. Off by default, because "
             "switching it on over a backlog emails real people about orders that "
             "shipped weeks ago, and that cannot be taken back.")

    # ------------------------------------------------------------------
    def _fulfilment_wanted(self, trigger):
        """Whether this store fulfils on the trigger that just fired."""
        self.ensure_one()
        return (self.platform == 'shopify'
                and self.export_fulfilments
                and self.fulfil_trigger in (trigger, 'both'))

    def post_shopify_fulfilment(self, order, tracking=None, carrier=None,
                                source=None):
        """Mark a Shopify order shipped. Returns the fulfilment id, or False.

        The single place the fulfilment conversation happens, used by both the
        delivery and the invoice paths.
        """
        self.ensure_one()
        row = self.env['miko.ecommerce.mapping'].search([
            ('channel_id', '=', self.id),
            ('model_name', '=', 'sale.order'),
            ('odoo_id', '=', order.id),
        ], limit=1)
        if not row:
            return False                       # not an order that came from Shopify

        client = self._shopify_client()
        data = client.call(FULFILMENT_ORDERS, {'id': row.external_id})
        node = data.get('order') or {}
        open_orders = [
            e['node']['id']
            for e in ((node.get('fulfillmentOrders') or {}).get('edges') or [])
            if (e.get('node') or {}).get('status') in OPEN_STATES
        ]
        if not open_orders:
            _logger.info("miko_shopify: %s has nothing left to fulfil", order.name)
            return False

        payload = {
            'lineItemsByFulfillmentOrder': [{'fulfillmentOrderId': fo}
                                            for fo in open_orders],
            'notifyCustomer': bool(self.notify_customer_on_fulfilment),
        }
        if tracking:
            payload['trackingInfo'] = {'number': tracking}
            if carrier:
                payload['trackingInfo']['company'] = carrier

        job = self.env['miko.ecommerce.job'].enqueue(
            self, 'export_fulfilment', row.external_id, payload,
            external_ref=order.name, direction='out')
        return self._job_export_fulfilment(payload, job)

    def _job_export_fulfilment(self, payload, job=None):
        """Post one fulfilment. Re-runnable, so Retry means something."""
        self.ensure_one()
        result = self._shopify_client().call(FULFILMENT_CREATE,
                                             {'fulfillment': payload})
        block = result.get('fulfillmentCreate') or {}
        errors = block.get('userErrors') or []
        if errors:
            # Arrives with HTTP 200 and an otherwise valid body. Not reading it
            # means reporting a shipment Shopify never recorded.
            message = "; ".join(e.get('message', '') for e in errors)
            if job:
                job.mark_blocked(message, _(
                    "Shopify would not accept this fulfilment. Usually the order "
                    "is already fulfilled, cancelled, or on hold in Shopify. Open "
                    "it there, then use Retry."))
            _logger.warning("miko_shopify: Shopify refused a fulfilment: %s", message)
            return False
        created = (block.get('fulfillment') or {}).get('id')
        if job:
            job.mark_done()
        return created


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    shopify_fulfilment_id = fields.Char(
        readonly=True, copy=False,
        help="The Shopify fulfilment this delivery created. Its presence is what "
             "stops the same delivery being posted twice.")

    def _action_done(self):
        """Post once the delivery is genuinely done.

        Hooked here rather than on button_validate because this is what actually
        completes a transfer, whatever route was taken to it: the button, a
        backorder wizard, or another module doing it in code.
        """
        result = super()._action_done()
        for picking in self:
            try:
                picking._miko_fulfil_on_delivery()
            except Exception:        # noqa: BLE001
                # A delivery must always be allowed to complete in Odoo. Shopify
                # being unreachable cannot be allowed to block the warehouse.
                _logger.exception(
                    "miko_shopify: could not post fulfilment for %s", picking.name)
        return result

    def _miko_sale_order(self):
        """The Odoo sale order behind this delivery, if there is one."""
        self.ensure_one()
        if 'sale_id' in self._fields and self.sale_id:
            return self.sale_id
        # sale_stock provides sale_id; without it, fall back to the procurement
        # group, which is how the link is actually made underneath.
        if 'group_id' in self._fields and self.group_id:
            return self.env['sale.order'].search(
                [('procurement_group_id', '=', self.group_id.id)], limit=1)
        return self.env['sale.order'].browse()

    def _miko_tracking(self):
        """(number, carrier) from whatever fields this install actually has."""
        self.ensure_one()
        number = self.carrier_tracking_ref if 'carrier_tracking_ref' in self._fields else None
        carrier = ''
        if 'carrier_id' in self._fields and self.carrier_id:
            carrier = self.carrier_id.name or ''
        return (number or '').strip(), carrier.strip()

    def _miko_fulfil_on_delivery(self):
        self.ensure_one()
        if self.picking_type_id.code != 'outgoing' or self.state != 'done':
            return False
        if self.shopify_fulfilment_id:
            return False                       # never post the same one twice

        order = self._miko_sale_order()
        if not order:
            return False
        channel = self.env['miko.ecommerce.mapping']._channel_for(order)
        if not channel or not channel._fulfilment_wanted('delivery'):
            return False

        number, carrier = self._miko_tracking()
        created = channel.post_shopify_fulfilment(order, number, carrier)
        if created:
            self.shopify_fulfilment_id = created
        return bool(created)


class AccountMove(models.Model):
    _inherit = 'account.move'

    shopify_fulfilment_id = fields.Char(
        readonly=True, copy=False,
        help="Set when posting this invoice fulfilled the Shopify order, so the "
             "same invoice cannot fulfil it twice.")

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        for move in self:
            if move.move_type != 'out_invoice' or move.state != 'posted':
                continue
            try:
                move._miko_fulfil_on_invoice()
            except Exception:            # noqa: BLE001
                # Posting an invoice is an accounting action and must never be
                # blocked because a storefront was unreachable.
                _logger.exception(
                    "miko_shopify: could not fulfil from invoice %s", move.name)
        return posted

    def _miko_fulfil_on_invoice(self):
        self.ensure_one()
        if self.shopify_fulfilment_id:
            return False
        Line = self.env['account.move.line']
        if 'sale_line_ids' not in Line._fields:
            return False                       # sale_stock / sale not linking here
        orders = self.line_ids.sale_line_ids.order_id
        for order in orders:
            channel = self.env['miko.ecommerce.mapping']._channel_for(order)
            if not channel or not channel._fulfilment_wanted('invoice'):
                continue
            created = channel.post_shopify_fulfilment(order, source=self)
            if created:
                self.shopify_fulfilment_id = created
                return True
        return False
