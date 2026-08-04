# -*- coding: utf-8 -*-
"""Bringing Shopify products, and their variants, into Odoo.

Variants are the hard part and the reason most catalogue imports go wrong.

Shopify stores a flat list of variants, each carrying its own option values.
Odoo stores attribute LINES on the template and generates the variants itself as
the full cross product. The two models only line up when the Shopify product is a
complete matrix, and plenty of real products are not: a shirt sold in small blue,
medium blue and medium red has three variants in Shopify and four in Odoo.

So this never invents a link. It builds the attribute lines, lets Odoo generate
what it will, and then matches each Shopify variant to the Odoo variant carrying
exactly the same option values. A Shopify variant with no match is reported and
left alone. The alternative, attaching it to the nearest-looking variant, puts
sales against the wrong SKU, and nobody finds that until stock is counted.
"""
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

# Shopify gives a product with no real options a single variant whose option is
# literally called "Title" with the value "Default Title". Turning that into an
# Odoo attribute would put a meaningless variant axis on every simple product.
PLACEHOLDER_OPTION = 'title'
PLACEHOLDER_VALUE = 'default title'

PRODUCT_QUERY = """
query ($first: Int!, $after: String, $filter: String) {
  products(first: $first, after: $after, query: $filter, sortKey: UPDATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id title handle status vendor productType descriptionHtml
      options { name values }
      variants(first: 100) { edges { node {
        id title sku barcode price
        selectedOptions { name value }
        inventoryItem { id unitCost { amount } measurement { weight { value unit } } }
      } } }
    } }
  }
}
"""


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def _miko_shopify_values(self, node, channel):
        return {
            'name': node.get('title') or _('Unnamed Shopify product'),
            'type': 'consu',
            'sale_ok': True,
            'purchase_ok': False,
            'company_id': False,   # shared, so multi-company installs still work
        }


class MikoEcommerceChannel(models.Model):
    _inherit = 'miko.ecommerce.channel'

    def action_import_products(self):
        """Import the catalogue. Safe to run again: every write is an upsert."""
        for channel in self:
            channel._import_shopify_products()
        return True

    # ------------------------------------------------------------------
    def _job_import_product(self, payload, job=None):
        """Import one product. The loop and the Retry button both come here.

        Having a single entry point is what makes Retry mean anything: replaying
        a job runs exactly the code that failed, against the payload that failed.
        """
        self.ensure_one()
        template = self._upsert_shopify_template(payload)
        self._upsert_shopify_variants(payload, template)
        return template

    def _import_shopify_products(self):
        self.ensure_one()
        self._require_direction('product_direction', 'in', _("Products"))
        client = self._shopify_client()
        Mapping = self.env['miko.ecommerce.mapping']
        Job = self.env['miko.ecommerce.job']

        filter_string = None
        if self.import_from_date:
            filter_string = "updated_at:>='%s'" % fields.Datetime.to_string(
                self.import_from_date).replace(' ', 'T') + 'Z'

        seen = 0
        for node in client.paginate(PRODUCT_QUERY, {'filter': filter_string},
                                    'products', page_size=25):
            job = Job.enqueue(self, 'import_product', node.get('id'), node,
                              external_ref=node.get('title'))
            try:
                template = self._job_import_product(node, job)
                job.mark_done(template)
                seen += 1
            except Exception as err:           # noqa: BLE001 - kept, not lost
                # One bad product must never abandon the rest of the catalogue.
                # The job holds the payload, so a retry needs no second API call.
                _logger.exception("miko_shopify: product %s failed", node.get('id'))
                job.mark_failed(err, blocked=job._is_blocking(err),
                                guidance=job._guidance_for(err))
        self._touch_sync()
        return seen

    def _upsert_shopify_template(self, node):
        """Create or update the Odoo template behind one Shopify product."""
        Mapping = self.env['miko.ecommerce.mapping']
        Template = self.env['product.template']
        template = Mapping.find_odoo_record(self, 'product.template', node['id'])
        values = Template._miko_shopify_values(node, self)

        if not template:
            template = Template.create(values)
            # Linked immediately, in the same transaction as the create. Linking
            # later in the caller left a window where a crash in between produced
            # a product with no mapping, which the next run could not recognise
            # and so created again. That is the duplicate this table exists to
            # prevent, reintroduced by doing the two steps apart.
            Mapping.link(self, template, node['id'], node.get('handle'))
        else:
            # Only the name is refreshed. Overwriting type, costing or accounting
            # on every sync would undo whatever the accountant configured in Odoo,
            # every time somebody edits a title in Shopify.
            if template.name != values['name']:
                template.name = values['name']
        self._apply_shopify_options(node, template)
        return template

    def _apply_shopify_options(self, node, template):
        """Mirror Shopify's options as Odoo attribute lines.

        Only ever adds. Removing an attribute line in Odoo deletes the variants
        underneath it, and those variants are referenced by every historical
        order line, so a sync must never do it as a side effect.
        """
        options = [o for o in (node.get('options') or [])
                   if (o.get('name') or '').strip().lower() != PLACEHOLDER_OPTION
                   or len(o.get('values') or []) > 1]
        if not options:
            return

        Attribute = self.env['product.attribute']
        Value = self.env['product.attribute.value']
        for option in options:
            name = (option.get('name') or '').strip()
            if not name:
                continue
            attribute = Attribute.search([('name', '=', name)], limit=1)
            if not attribute:
                attribute = Attribute.create({'name': name, 'create_variant': 'always'})

            wanted = []
            for raw in option.get('values') or []:
                label = (raw or '').strip()
                if not label:
                    continue
                value = Value.search(
                    [('attribute_id', '=', attribute.id), ('name', '=', label)], limit=1)
                if not value:
                    value = Value.create({'attribute_id': attribute.id, 'name': label})
                wanted.append(value.id)
            if not wanted:
                continue

            line = template.attribute_line_ids.filtered(
                lambda l, a=attribute: l.attribute_id == a)
            if line:
                missing = [v for v in wanted if v not in line.value_ids.ids]
                if missing:
                    line.value_ids = [(4, v) for v in missing]
            else:
                template.attribute_line_ids = [(0, 0, {
                    'attribute_id': attribute.id,
                    'value_ids': [(6, 0, wanted)],
                })]

    def _upsert_shopify_variants(self, node, template):
        """Link each Shopify variant to the Odoo variant with the same options."""
        Mapping = self.env['miko.ecommerce.mapping']
        edges = ((node.get('variants') or {}).get('edges')) or []
        variants = [e['node'] for e in edges if e.get('node')]

        for variant in variants:
            product = Mapping.find_odoo_record(self, 'product.product', variant['id'])
            if not product:
                product = self._match_shopify_variant(variant, template)
            if not product:
                _logger.warning(
                    "miko_shopify: variant %s of '%s' has no matching Odoo variant "
                    "and was left unlinked rather than attached to the wrong one",
                    variant.get('sku') or variant.get('id'), template.name)
                continue

            updates = {}
            sku = (variant.get('sku') or '').strip()
            if sku and product.default_code != sku:
                updates['default_code'] = sku
            barcode = (variant.get('barcode') or '').strip()
            # Barcodes are unique across the whole database in Odoo, so a clash
            # would abort the entire import. Skipping one barcode is a far smaller
            # problem than a catalogue that will not sync at all.
            if barcode and product.barcode != barcode:
                clash = self.env['product.product'].with_context(
                    active_test=False).search(
                        [('barcode', '=', barcode), ('id', '!=', product.id)], limit=1)
                if clash:
                    _logger.warning(
                        "miko_shopify: barcode %s already belongs to another "
                        "product, left unchanged on %s", barcode, product.display_name)
                else:
                    updates['barcode'] = barcode
            inv = (variant.get('inventoryItem') or {}).get('id')
            if inv and product.shopify_inventory_item_id != inv:
                updates['shopify_inventory_item_id'] = inv
            price = variant.get('price')
            if price is not None:
                try:
                    updates['lst_price'] = float(price)
                except (TypeError, ValueError):
                    pass
            if updates:
                product.write(updates)
            Mapping.link(self, product, variant['id'], sku or variant.get('title'))

    @api.model
    def _variant_option_key(self, pairs):
        """A comparable key for a set of option values, order independent."""
        return frozenset(
            ((name or '').strip().lower(), (value or '').strip().lower())
            for name, value in pairs
            if (name or '').strip().lower() != PLACEHOLDER_OPTION
            or (value or '').strip().lower() != PLACEHOLDER_VALUE)

    def _match_shopify_variant(self, variant, template):
        """The Odoo variant whose option values are exactly this variant's.

        Exact set equality, nothing looser. A subset match would happily pick a
        blue shirt in any size, and the wrong size is a real parcel going to a
        real customer.
        """
        products = template.product_variant_ids
        if not products:
            return self.env['product.product'].browse()

        wanted = self._variant_option_key(
            (o.get('name'), o.get('value'))
            for o in (variant.get('selectedOptions') or []))

        if not wanted:
            # A product with no real options. Only unambiguous when Odoo agrees
            # there is exactly one variant.
            return products if len(products) == 1 else self.env['product.product'].browse()

        for product in products:
            values = product.product_template_attribute_value_ids
            actual = self._variant_option_key(
                (v.attribute_id.name, v.name) for v in values)
            if actual == wanted:
                return product
        return self.env['product.product'].browse()
