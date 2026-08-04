# -*- coding: utf-8 -*-
"""Shopify credentials and the settings that decide what a sync is allowed to do.

The defaults here are conservative on purpose. A connector that arrives already
confirming orders, invoicing them and writing stock back is a connector that
makes a mess on day one out of records nobody can unpick afterwards. Every action
that changes something has to be switched on by somebody who meant to switch it on.
"""
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .shopify_client import DEFAULT_API_VERSION, ShopifyClient, ShopifyError

# example.myshopify.com. Anything else is a custom domain, which answers on the
# storefront but not on the Admin API, and produces a confusing 404 later.
DOMAIN_RE = re.compile(r'^[a-z0-9][a-z0-9-]*\.myshopify\.com$')


class MikoEcommerceChannel(models.Model):
    _inherit = 'miko.ecommerce.channel'

    platform = fields.Selection(
        selection_add=[('shopify', 'Shopify')], ondelete={'shopify': 'set default'})

    shopify_domain = fields.Char(
        string='Store domain',
        help="The myshopify.com domain, such as example.myshopify.com. Not the "
             "custom domain customers see: that one does not answer the Admin API.")
    shopify_token = fields.Char(
        string='Admin API access token', groups='base.group_system',
        help="From the custom app in Settings > Apps and sales channels > "
             "Develop apps. Shown once, when the app is installed.\n\n"
             "Stored in this database like any other setting, so treat database "
             "access as equivalent to store access, and revoke the token in "
             "Shopify if that ever stops being true.")
    shopify_api_version = fields.Char(
        string='API version', default=DEFAULT_API_VERSION,
        help="Pinned deliberately. Shopify supports each quarterly version for a "
             "year, and pinning stops a Shopify release changing the shape of a "
             "response underneath a working integration.")

    shopify_shop_name = fields.Char(readonly=True)
    shopify_currency = fields.Char(readonly=True)
    # -- what a sync may do -------------------------------------------------
    # ------------------------------------------------------------------
    @api.constrains('shopify_domain', 'platform')
    def _check_shopify_domain(self):
        for channel in self:
            if channel.platform != 'shopify' or not channel.shopify_domain:
                continue
            domain = channel.shopify_domain.strip().lower()
            if not DOMAIN_RE.match(domain):
                raise ValidationError(_(
                    "'%s' is not a Shopify store domain. It has to be the "
                    "myshopify.com one, such as example.myshopify.com.\n\n"
                    "The custom domain customers visit will not work here: it "
                    "serves the storefront, not the Admin API.") % channel.shopify_domain)

    @api.onchange('shopify_domain')
    def _onchange_shopify_domain(self):
        """Accept what people actually paste.

        Everybody pastes a full URL at least once. Repairing it silently is
        friendlier than refusing it, and the constraint above still catches
        anything that is genuinely not a store domain.
        """
        if not self.shopify_domain:
            return
        cleaned = self.shopify_domain.strip().lower()
        cleaned = re.sub(r'^https?://', '', cleaned).split('/')[0].split('?')[0]
        if cleaned and '.' not in cleaned:
            cleaned = '%s.myshopify.com' % cleaned      # they typed just the handle
        self.shopify_domain = cleaned

    # ------------------------------------------------------------------
    def _shopify_client(self):
        """An authenticated client for this channel."""
        self.ensure_one()
        if self.platform != 'shopify':
            raise UserError(_("%s is not a Shopify store.") % self.name)
        # sudo() only to read the token field, which is restricted to system
        # users so it cannot be read off the form by everyone who can run a sync.
        return ShopifyClient(self.shopify_domain,
                             self.sudo().shopify_token,
                             self.shopify_api_version)

    def action_test_connection(self):
        """Prove the credentials work, and say precisely what failed if not.

        Worth its own button. Every other operation is slow enough that finding
        out about a wrong token halfway through a product import wastes real time.
        """
        for channel in self:
            try:
                shop = channel._shopify_client().shop()
            except ShopifyError as err:
                channel.write({
                    'connection_state': 'error',
                    'connection_message': str(err),
                })
                continue
            except Exception as err:               # noqa: BLE001 - shown to the user
                channel.write({
                    'connection_state': 'error',
                    'connection_message': _("Unexpected problem: %s") % err,
                })
                continue

            returned = (shop.get('myshopifyDomain') or '').lower()
            if returned and returned != (channel.shopify_domain or '').lower():
                # The token belongs to a different store than the one configured.
                # Left alone, every subsequent sync would import the wrong store's
                # orders into this company.
                channel.write({
                    'connection_state': 'error',
                    'connection_message': _(
                        "That token belongs to %(actual)s, not %(configured)s. "
                        "Importing would bring the wrong store's data into this "
                        "company.") % {'actual': returned,
                                       'configured': channel.shopify_domain},
                })
                continue

            channel.write({
                'connection_state': 'ok',
                'connection_message': _(
                    "Connected to %(name)s. Store currency %(cur)s.") % {
                        'name': shop.get('name') or returned,
                        'cur': shop.get('currencyCode') or '?'},
                'shopify_shop_name': shop.get('name'),
                'shopify_currency': shop.get('currencyCode'),
            })
        return True

    # ------------------------------------------------------------------
    def _shopify_currency(self):
        """The Odoo currency matching the store, or the company's.

        A mismatch here is the quiet kind of wrong: orders import, totals look
        plausible, and every figure is out by the exchange rate.
        """
        self.ensure_one()
        code = (self.shopify_currency or '').upper()
        if not code:
            return self.company_id.currency_id
        currency = self.env['res.currency'].with_context(active_test=False).search(
            [('name', '=', code)], limit=1)
        if not currency:
            raise UserError(_(
                "The Shopify store sells in %s and there is no such currency in "
                "Odoo. Add it before importing, or every imported total will be "
                "wrong by the exchange rate.") % code)
        if not currency.active:
            currency.sudo().write({'active': True})
        return currency


    def _default_field_maps(self):
        """Shopify's own default field mappings, seeded per store.

        The engine holds the mechanism and knows nothing about Shopify's field
        names; this is where they live.
        """
        if self.platform != 'shopify':
            return super()._default_field_maps()
        return [
            ('product', 'in', 'title', 'name', True),
            ('product', 'in', 'descriptionHtml', 'description_sale', False),
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
