# -*- coding: utf-8 -*-
"""Publishing Odoo stock levels back to Shopify.

This is the direction that oversells. If Odoo knows there are two left and Shopify
still says twelve, the store keeps taking orders that cannot be filled, and every
one of those becomes a refund and an apology.

Deliberate choices, each because the obvious alternative is wrong:

**Absolute set, not adjustment.** `inventorySetQuantities` writes the number Odoo
believes. An adjust-by-delta call assumes the two systems agreed before it ran,
and if they ever drift the drift compounds silently with every sync.

**Free quantity by default, not on hand.** On-hand includes stock already promised
to other orders. Publishing it is how a warehouse ends up owing the same unit to
two customers. Configurable, because a business that never reserves may genuinely
want on-hand.

**One named location.** Shopify tracks stock per location and a write needs to say
which. Guessing when a store has several would silently update the wrong shelf, so
nothing is exported until a location is chosen.

**Never invents a link.** Only variants that were mapped by an import are touched.
A product that has never been matched to Shopify is left completely alone.
"""
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Shopify accepts up to 250 quantities per call. Chunked well below that: a
# rejected batch costs the whole batch, and smaller batches fail smaller.
BATCH = 100

LOCATION_QUERY = """
query { locations(first: 25, includeInactive: false) {
  edges { node { id name isActive fulfillsOnlineOrders } } } }
"""

# An inventory item can only hold a quantity at a location it is STOCKED at, and
# a product Shopify has just created is stocked nowhere. Setting quantities
# without this returns "The specified inventory item is not stocked at the
# location" - a userError, so HTTP 200 and an otherwise valid response. Verified
# against a live store: absent level, activate, then set, then the quantity reads
# back correctly.
LEVELS_QUERY = """
query ($ids: [ID!]!, $loc: ID!) {
  nodes(ids: $ids) {
    ... on InventoryItem {
      id tracked
      inventoryLevel(locationId: $loc) { id }
    }
  }
}
"""

ACTIVATE = """
mutation ($id: ID!, $loc: ID!) {
  inventoryActivate(inventoryItemId: $id, locationId: $loc) {
    inventoryLevel { id }
    userErrors { field message }
  }
}
"""

SET_QUANTITIES = """
mutation ($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup { id }
    userErrors { field message }
  }
}
"""


class ProductProduct(models.Model):
    _inherit = 'product.product'

    shopify_inventory_item_id = fields.Char(
        readonly=True, copy=False, index=True,
        help="Shopify's inventory item id for this variant. Stock is written "
             "against this rather than the variant, because that is what "
             "Shopify's inventory API addresses.")


class MikoEcommerceChannel(models.Model):
    _inherit = 'miko.ecommerce.channel'

    export_stock = fields.Boolean(
        string='Publish stock to Shopify', default=False,
        help="Off by default. Writing stock into a live storefront is not "
             "something a module should start doing because it was installed.")
    shopify_location_id = fields.Char(
        string='Shopify location',
        help="Which Shopify location stock is written to. Required before any "
             "stock is exported: a store with several locations would otherwise "
             "have the wrong one updated silently.")
    shopify_location_name = fields.Char(readonly=True)
    stock_source = fields.Selection(
        [('free', 'Available to promise'), ('on_hand', 'On hand')],
        default='free', required=True, string='Quantity to publish',
        help="Available to promise excludes stock already reserved for other "
             "orders. Publishing on hand is how the same unit gets sold twice.")

    # ------------------------------------------------------------------
    def action_fetch_shopify_locations(self):
        """Read the store's locations so one can be chosen rather than typed."""
        self.ensure_one()
        data = self._shopify_client().call(LOCATION_QUERY)
        nodes = [e['node'] for e in ((data.get('locations') or {}).get('edges') or [])]
        if not nodes:
            raise UserError(_("This Shopify store has no active locations."))

        online = [n for n in nodes if n.get('fulfillsOnlineOrders')] or nodes
        if not self.shopify_location_id:
            self.shopify_location_id = online[0]['id']
            self.shopify_location_name = online[0].get('name')
        listing = "\n".join("- %s" % n.get('name') for n in nodes)
        self.connection_message = _(
            "Locations found:\n%(list)s\n\nUsing: %(chosen)s") % {
                'list': listing, 'chosen': self.shopify_location_name or '?'}
        return True

    def action_export_stock(self):
        for channel in self:
            channel._export_shopify_stock()
        return True

    # ------------------------------------------------------------------
    def _stock_export_products(self):
        """Mapped variants that Shopify can actually receive stock for."""
        self.ensure_one()
        rows = self.env['miko.ecommerce.mapping'].search([
            ('channel_id', '=', self.id),
            ('model_name', '=', 'product.product'),
        ])
        products = self.env['product.product'].browse(rows.mapped('odoo_id')).exists()
        # Services have no stock to publish, and a variant with no inventory item
        # id was never matched to Shopify, so there is nothing to write against.
        return products.filtered(
            lambda p: p.type == 'consu' and p.shopify_inventory_item_id)

    def _quantity_for(self, product):
        """The number to publish, in the store's warehouse if one is set."""
        self.ensure_one()
        if self.warehouse_id:
            product = product.with_context(warehouse_id=self.warehouse_id.id)
        value = product.free_qty if self.stock_source == 'free' else product.qty_available
        # Shopify will not accept a negative available quantity, and a negative
        # on-hand in Odoo is a data problem, not something to push to a storefront.
        return max(0, int(value or 0))

    def _ensure_stocked(self, client, item_ids):
        """Make sure each inventory item exists at the chosen location.

        Shopify will not accept a quantity for an item that is not stocked at the
        location, and a freshly created product is stocked nowhere. Checked in
        bulk first so only the items that actually need it cost an extra call.

        An untracked item is reported rather than changed: turning tracking on
        decides whether the storefront can oversell, and that is a merchandising
        decision for the store owner, not a side effect of publishing a number.
        """
        self.ensure_one()
        if not item_ids:
            return []
        data = client.call(LEVELS_QUERY, {'ids': list(item_ids),
                                          'loc': self.shopify_location_id})
        activated, untracked = [], []
        for node in (data.get('nodes') or []):
            if not node:
                continue
            if not node.get('tracked'):
                untracked.append(node.get('id'))
            if node.get('inventoryLevel'):
                continue
            result = client.call(ACTIVATE, {'id': node['id'],
                                            'loc': self.shopify_location_id})
            block = result.get('inventoryActivate') or {}
            errors = block.get('userErrors') or []
            if errors:
                _logger.warning("miko_shopify: could not stock %s at %s: %s",
                                node['id'], self.shopify_location_name,
                                "; ".join(e.get('message', '') for e in errors))
            else:
                activated.append(node['id'])
        if activated:
            _logger.info("miko_shopify: stocked %s new item(s) at %s",
                         len(activated), self.shopify_location_name or 'the location')
        if untracked:
            _logger.warning(
                "miko_shopify: %s item(s) have inventory tracking switched off in "
                "Shopify, so the quantities published for them will not stop the "
                "storefront selling. Turn tracking on in Shopify for those products.",
                len(untracked))
        return activated

    def _export_shopify_stock(self):
        self.ensure_one()
        if not self.export_stock:
            raise UserError(_(
                "Publishing stock is switched off for %s. Turn on 'Publish stock "
                "to Shopify' on the store first.") % self.name)
        if not self.shopify_location_id:
            raise UserError(_(
                "No Shopify location is set for %s. Press 'Fetch Locations' and "
                "choose one: a store with more than one location would otherwise "
                "have the wrong one updated without saying so.") % self.name)

        client = self._shopify_client()
        Job = self.env['miko.ecommerce.job']
        products = self._stock_export_products()
        sent = 0

        for start in range(0, len(products), BATCH):
            batch = products[start:start + BATCH]
            quantities = [{
                'inventoryItemId': p.shopify_inventory_item_id,
                'locationId': self.shopify_location_id,
                'quantity': self._quantity_for(p),
            } for p in batch]

            job = Job.enqueue(self, 'export_stock', None, {'count': len(quantities)},
                              direction='out')
            try:
                self._ensure_stocked(client, [q['inventoryItemId'] for q in quantities])
                data = client.call(SET_QUANTITIES, {'input': {
                    'name': 'available',
                    'reason': 'correction',
                    # Without this Shopify demands the quantity it currently holds
                    # and rejects the write if it has moved since we read it. We
                    # are the authority here, so we set rather than compare.
                    'ignoreCompareQuantity': True,
                    'quantities': quantities,
                }})
                errors = ((data.get('inventorySetQuantities') or {}).get('userErrors')) or []
                if errors:
                    # userErrors arrive alongside HTTP 200 and an otherwise valid
                    # response. Not reading them means reporting success for a
                    # write Shopify refused.
                    raise UserError("; ".join(
                        e.get('message', '') for e in errors))
                job.mark_done()
                sent += len(quantities)
            except Exception as err:        # noqa: BLE001 - kept, not lost
                _logger.exception("miko_shopify: stock batch failed for %s", self.name)
                job.mark_failed(err)

        self._touch_sync()
        _logger.info("miko_shopify: published stock for %s variants on %s",
                     sent, self.name)
        return sent
