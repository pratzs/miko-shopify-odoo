# -*- coding: utf-8 -*-
"""Which Shopify field goes to which Odoo field, decided by the person who owns
the data rather than by us.

Every business puts things in different places. One keeps the Shopify vendor in
`product.template.manufacturer`, another wants it as a tag, a third does not want
it at all. Hard-coding a guess means the connector is wrong for most of them and
there is nothing they can do about it.

**What is NOT mappable, deliberately.** Identity is fixed: the Shopify id, the
mapping row, the order total, the tax lines. Letting those be repointed would let
somebody quietly break duplicate prevention, and the whole product rests on that.
So the table covers descriptive fields, where being wrong costs a bad description
rather than a double-charged customer.

Defaults are seeded per store on first use so the common case works untouched, and
every row can be switched off rather than deleted, so turning one back on later
does not mean remembering what it was.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

ENTITIES = [
    ('product', 'Product'),
    ('customer', 'Customer'),
    ('order', 'Order'),
]
DIRECTIONS = [
    ('in', 'Shopify to Odoo'),
    ('out', 'Odoo to Shopify'),
]

ODOO_MODEL = {
    'product': 'product.template',
    'customer': 'res.partner',
    'order': 'sale.order',
}

# (entity, direction, shopify field, odoo field, on by default)
DEFAULT_MAPS = [
    ('product', 'in', 'title', 'name', True),
    ('product', 'in', 'descriptionHtml', 'description_sale', False),
    ('product', 'in', 'vendor', 'default_code', False),
    ('product', 'out', 'title', 'name', True),
    ('product', 'out', 'descriptionHtml', 'description_sale', True),
    ('customer', 'in', 'email', 'email', True),
    ('customer', 'in', 'phone', 'phone', True),
    ('customer', 'in', 'note', 'comment', False),
    ('customer', 'out', 'email', 'email', True),
    ('customer', 'out', 'phone', 'phone', True),
    ('order', 'in', 'name', 'client_order_ref', True),
    ('order', 'in', 'note', 'note', False),
]


class MikoShopifyFieldMap(models.Model):
    _name = 'miko.shopify.field.map'
    _description = 'Shopify field mapping'
    _order = 'channel_id, entity, direction, shopify_field'
    _rec_name = 'shopify_field'

    channel_id = fields.Many2one(
        'miko.ecommerce.channel', string='Store', required=True,
        ondelete='cascade', index=True)
    entity = fields.Selection(ENTITIES, required=True, index=True)
    direction = fields.Selection(DIRECTIONS, required=True, index=True)
    shopify_field = fields.Char(
        required=True,
        help="The name Shopify uses, such as title, vendor or descriptionHtml.")
    odoo_field = fields.Char(
        required=True,
        help="The technical name of the Odoo field, such as description_sale.")
    odoo_model = fields.Char(compute='_compute_odoo_model', store=True)
    active = fields.Boolean(
        default=True,
        help="Switch a mapping off rather than deleting it, so turning it back on "
             "later does not mean remembering what it used to be.")
    note = fields.Char()

    def init(self):
        """One mapping per store, entity, direction and Shopify field.

        An index rather than _sql_constraints: Odoo 19 replaced that mechanism, so
        a declaration written once is silently absent on one series, and duplicate
        rows here mean nobody can tell which mapping is actually being used.
        """
        super().init()
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS miko_shopify_field_map_uniq
                ON miko_shopify_field_map (channel_id, entity, direction, shopify_field)
        """)

    @api.depends('entity')
    def _compute_odoo_model(self):
        for rec in self:
            rec.odoo_model = ODOO_MODEL.get(rec.entity, False)

    @api.constrains('entity', 'odoo_field')
    def _check_odoo_field_exists(self):
        """Refuse a field that is not there, at the moment it is typed.

        Otherwise the mistake surfaces halfway through a sync as an unexplained
        failure on one record, which is a much worse place to learn about it.
        """
        for rec in self:
            model_name = ODOO_MODEL.get(rec.entity)
            if not model_name or not rec.odoo_field:
                continue
            model = self.env.get(model_name)
            if model is None:
                continue
            if rec.odoo_field not in model._fields:
                raise ValidationError(_(
                    "'%(field)s' is not a field on %(model)s. Check the technical "
                    "name in Settings, Technical, Fields.") % {
                        'field': rec.odoo_field, 'model': model_name})
            field = model._fields[rec.odoo_field]
            # Being computed does not make a field unwritable. Plenty of Odoo
            # fields are computed, stored and still editable - sale.order.note is
            # exactly that, and an earlier version of this check refused it. What
            # actually matters is whether a write survives: a readonly field is
            # recomputed over, and a computed field that is neither stored nor
            # given an inverse has nowhere to put the value.
            writable = (not field.readonly) and (field.store or bool(field.inverse))
            if not writable:
                raise ValidationError(_(
                    "'%(field)s' on %(model)s cannot be written to: Odoo "
                    "calculates it and would overwrite anything put there. Pick a "
                    "field that keeps its own value.") % {
                        'field': rec.odoo_field, 'model': model_name})


class MikoEcommerceChannel(models.Model):
    _inherit = 'miko.ecommerce.channel'

    field_map_ids = fields.One2many(
        'miko.shopify.field.map', 'channel_id', string='Field mappings')

    def _seed_field_maps(self):
        """Create the default mappings for a store, once, without overwriting."""
        FieldMap = self.env['miko.shopify.field.map']
        for channel in self:
            existing = {
                (m.entity, m.direction, m.shopify_field)
                for m in FieldMap.with_context(active_test=False).search(
                    [('channel_id', '=', channel.id)])
            }
            for entity, direction, shop_field, odoo_field, on in DEFAULT_MAPS:
                if (entity, direction, shop_field) in existing:
                    continue
                model = self.env.get(ODOO_MODEL[entity])
                if model is None or odoo_field not in model._fields:
                    continue          # the field belongs to a module not installed
                FieldMap.create({
                    'channel_id': channel.id, 'entity': entity,
                    'direction': direction, 'shopify_field': shop_field,
                    'odoo_field': odoo_field, 'active': on,
                })
        return True

    def action_seed_field_maps(self):
        self._seed_field_maps()
        return True

    # ------------------------------------------------------------------
    def _field_maps(self, entity, direction):
        """Active mappings as {shopify field: odoo field}."""
        self.ensure_one()
        return {
            m.shopify_field: m.odoo_field
            for m in self.field_map_ids
            if m.entity == entity and m.direction == direction and m.active
        }

    def _apply_maps_in(self, entity, node, target_model):
        """Values for an Odoo record, taken from a Shopify payload via the maps.

        Only writes what the payload actually carries, so a missing key leaves the
        Odoo field alone instead of blanking it.
        """
        self.ensure_one()
        model = self.env[target_model]
        values = {}
        for shop_field, odoo_field in self._field_maps(entity, 'in').items():
            if shop_field not in (node or {}):
                continue
            if odoo_field not in model._fields:
                continue              # mapping points at a field this DB lacks
            raw = node.get(shop_field)
            values[odoo_field] = raw if raw not in (None, '') else False
        return values

    def _apply_maps_out(self, entity, record):
        """A Shopify payload fragment built from an Odoo record via the maps."""
        self.ensure_one()
        payload = {}
        for shop_field, odoo_field in self._field_maps(entity, 'out').items():
            if odoo_field not in record._fields:
                continue
            value = record[odoo_field]
            if value in (False, None):
                continue
            payload[shop_field] = value if isinstance(value, str) else str(value)
        return payload
