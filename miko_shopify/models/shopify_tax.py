# -*- coding: utf-8 -*-
"""Shopify tax lines to Odoo taxes.

Tax is where connectors leak money, and it leaks silently. Shopify calculates tax
its own way and sends the result as named lines on each order. Odoo calculates tax
from its own rules. Neither can be trusted to reproduce the other, so this module
does not try: it matches what Shopify actually sent to a tax the accountant chose.

Anything Shopify sends that has no Odoo equivalent is recorded here, unmapped and
visible, rather than being dropped. Dropping it produces invoices that are short by
exactly the tax amount, and nobody discovers that until a return is filed.
"""
from odoo import _, api, fields, models


class MikoShopifyTax(models.Model):
    _name = 'miko.shopify.tax'
    _description = 'Shopify tax to Odoo tax'
    _order = 'channel_id, title'
    _rec_name = 'title'

    channel_id = fields.Many2one(
        'miko.ecommerce.channel', string='Store', required=True,
        ondelete='cascade', index=True)
    title = fields.Char(
        required=True, index=True,
        help="The name Shopify puts on the tax line, such as 'GST' or 'VAT'.")
    rate = fields.Float(
        digits=(16, 6),
        help="The rate Shopify applied, as a fraction. 0.15 is 15 percent.")
    rate_percent = fields.Float(
        string='Rate %', compute='_compute_rate_percent', store=True, digits=(16, 4))
    tax_id = fields.Many2one(
        'account.tax', string='Odoo tax', ondelete='restrict',
        domain="[('type_tax_use', '=', 'sale')]",
        help="Leave empty to have imports stop and ask, which is the safe default.")
    company_id = fields.Many2one(
        'res.company', related='channel_id.company_id', store=True)
    first_seen = fields.Datetime(readonly=True, default=fields.Datetime.now)
    order_count = fields.Integer(
        readonly=True, default=0,
        help="How many imported orders carried this tax. A high number with no "
             "Odoo tax set is the most expensive thing on this screen.")

    def init(self):
        """One row per store, tax name and rate, on every Odoo series.

        Written as an index rather than _sql_constraints because Odoo 19 replaced
        that mechanism, and a declaration that silently does not exist on one
        series lets duplicate rows accumulate until nobody can tell which mapping
        is being used.
        """
        super().init()
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS miko_shopify_tax_uniq
                ON miko_shopify_tax (channel_id, title, rate)
        """)

    @api.depends('rate')
    def _compute_rate_percent(self):
        for rec in self:
            rec.rate_percent = (rec.rate or 0.0) * 100.0

    # ------------------------------------------------------------------
    @api.model
    def resolve(self, channel, tax_line):
        """The Odoo tax for one Shopify tax line, recording it if it is new.

        Returns an `account.tax` recordset, empty when nothing is mapped yet. The
        caller decides what an empty result means, because that is the store
        owner's policy rather than this function's.
        """
        title = (tax_line.get('title') or '').strip() or _('Tax')
        rate = float(tax_line.get('rate') or 0.0)
        row = self.search([
            ('channel_id', '=', channel.id),
            ('title', '=', title),
            ('rate', '>=', rate - 1e-9),
            ('rate', '<=', rate + 1e-9),
        ], limit=1)
        if not row:
            row = self.create({
                'channel_id': channel.id, 'title': title, 'rate': rate,
            })
            row._suggest_tax()
        row.order_count += 1
        return row.tax_id

    def _suggest_tax(self):
        """Pre-fill the mapping when exactly one Odoo tax has the same rate.

        A suggestion, not a decision: it fills the field so it can be reviewed,
        and only when there is no ambiguity at all. Two candidates means guessing,
        and guessing tax is precisely what this model exists to stop.
        """
        for rec in self:
            if rec.tax_id or not rec.rate:
                continue
            target = rec.rate * 100.0
            candidates = self.env['account.tax'].search([
                ('type_tax_use', '=', 'sale'),
                ('amount_type', '=', 'percent'),
                ('company_id', '=', rec.company_id.id),
                ('amount', '>=', target - 0.01),
                ('amount', '<=', target + 0.01),
            ])
            if len(candidates) == 1:
                rec.tax_id = candidates


class MikoEcommerceChannel(models.Model):
    _inherit = 'miko.ecommerce.channel'

    shopify_tax_ids = fields.One2many(
        'miko.shopify.tax', 'channel_id', string='Shopify taxes',
        help="Filled in as taxes are encountered. A row with no Odoo tax is a "
             "row that will stop the next import that needs it.")
