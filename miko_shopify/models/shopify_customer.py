# -*- coding: utf-8 -*-
"""Shopify customers and their addresses, as Odoo contacts.

Two rules do most of the work here.

**Never merge on a weak signal.** An existing Odoo contact is reused only when the
email matches exactly, because email is the one field Shopify actually verifies.
Matching on name would merge the two unrelated people called John Smith, and
un-merging contacts after their orders and invoices have been attached is not
something anyone gets to do cleanly.

**Never overwrite what somebody typed in Odoo.** A sync fills fields that are
empty and leaves the rest. The alternative is a connector that reverts a corrected
address every time it runs, which teaches people to stop correcting addresses.
"""
import logging

from odoo import _, fields, models

_logger = logging.getLogger(__name__)

ADDRESS_FIELDS = """
  firstName lastName company address1 address2 city province provinceCode
  zip country countryCodeV2 phone
"""

CUSTOMER_QUERY = """
query ($first: Int!, $after: String, $filter: String) {
  customers(first: $first, after: $after, query: $filter, sortKey: UPDATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id firstName lastName note
      defaultEmailAddress { emailAddress }
      defaultPhoneNumber { phoneNumber }
      defaultAddress { %s }
    } }
  }
}
""" % ADDRESS_FIELDS


def flatten_customer(node):
    """Lift Shopify's nested email and phone up to flat keys.

    Customer.email and Customer.phone were REMOVED from the Admin API; 2025-07
    exposes them as defaultEmailAddress { emailAddress } and
    defaultPhoneNumber { phoneNumber }. CustomerInput, confusingly, still takes
    flat `email` and `phone` on the way in, so only reading changed.

    Flattening once, here, means everything downstream keeps a stable shape:
    the field mappings, the partner values, and crucially the payload stored on
    the job, so a job queued today still replays correctly.
    """
    node = dict(node or {})
    if 'email' not in node:
        node['email'] = ((node.get('defaultEmailAddress') or {}).get('emailAddress') or '')
    if 'phone' not in node:
        node['phone'] = ((node.get('defaultPhoneNumber') or {}).get('phoneNumber') or '')
    return node


class MikoEcommerceChannel(models.Model):
    _inherit = 'miko.ecommerce.channel'

    def action_import_customers(self):
        for channel in self:
            channel._import_shopify_customers()
        return True

    # ------------------------------------------------------------------
    def _job_import_customer(self, payload, job=None):
        """Import one customer. Shared by the loop and by Retry."""
        self.ensure_one()
        return self._upsert_shopify_customer(flatten_customer(payload))

    def _import_shopify_customers(self):
        self.ensure_one()
        self._require_direction('customer_direction', 'in', _("Customers"))
        client = self._shopify_client()
        Job = self.env['miko.ecommerce.job']

        filter_string = None
        if self.import_from_date:
            filter_string = "updated_at:>='%sZ'" % fields.Datetime.to_string(
                self.import_from_date).replace(' ', 'T')

        count = 0
        for raw in client.paginate(CUSTOMER_QUERY, {'filter': filter_string},
                                   'customers', page_size=50):
            node = flatten_customer(raw)
            job = Job.enqueue(self, 'import_customer', node.get('id'), node,
                              external_ref=node.get('email'))
            try:
                partner = self._job_import_customer(node, job)
                job.mark_done(partner)
                count += 1
            except Exception as err:            # noqa: BLE001 - kept, not lost
                _logger.exception("miko_shopify: customer %s failed", node.get('id'))
                job.mark_failed(err, blocked=job._is_blocking(err),
                                guidance=job._guidance_for(err))
        self._touch_sync()
        return count

    def _upsert_shopify_customer(self, node):
        """The Odoo contact for one Shopify customer, created if needed."""
        Mapping = self.env['miko.ecommerce.mapping']
        Partner = self.env['res.partner']

        partner = Mapping.find_odoo_record(self, 'res.partner', node['id'])
        email = (node.get('email') or '').strip()

        if not partner and email:
            # Exact email only. Anything looser merges strangers.
            partner = Partner.search([
                ('email', '=ilike', email),
                ('parent_id', '=', False),
                '|', ('company_id', '=', self.company_id.id), ('company_id', '=', False),
            ], limit=1)

        values = self._shopify_partner_values(node)
        if not partner:
            partner = Partner.create(values)
        else:
            partner.write(self._only_missing(partner, values))

        Mapping.link(self, partner, node['id'], email or partner.name)
        self._sync_shopify_address(partner, node.get('defaultAddress'), 'delivery')
        return partner

    # ------------------------------------------------------------------
    @staticmethod
    def _only_missing(record, values):
        """Keep only the values whose field is currently empty on the record."""
        return {k: v for k, v in values.items() if v and not record[k]}

    def _shopify_partner_values(self, node):
        address = node.get('defaultAddress') or {}
        name = ' '.join(p for p in [(node.get('firstName') or '').strip(),
                                    (node.get('lastName') or '').strip()] if p)
        if not name:
            name = (address.get('company') or '').strip()
        if not name:
            # Never create a nameless contact: it is unfindable afterwards.
            name = (node.get('email') or '').strip() or _('Shopify customer')
        values = {
            'name': name,
            'email': (node.get('email') or '').strip() or False,
            'phone': (node.get('phone') or address.get('phone') or '').strip() or False,
            'customer_rank': 1,
            'company_id': self.company_id.id,
        }
        values.update(self._shopify_address_values(address))
        return values

    def _shopify_address_values(self, address):
        """Shopify address fields as Odoo ones, resolving country and state.

        Countries are resolved by ISO code rather than name: Shopify's names do
        not always match Odoo's, and an unresolved country silently breaks tax
        and shipping rules that are keyed on it.
        """
        if not address:
            return {}
        values = {
            'street': (address.get('address1') or '').strip() or False,
            'street2': (address.get('address2') or '').strip() or False,
            'city': (address.get('city') or '').strip() or False,
            'zip': (address.get('zip') or '').strip() or False,
        }
        code = (address.get('countryCodeV2') or '').strip().upper()
        country = self.env['res.country'].search([('code', '=', code)], limit=1) if code else None
        if country:
            values['country_id'] = country.id
            province = (address.get('provinceCode') or '').strip().upper()
            if province:
                state = self.env['res.country.state'].search([
                    ('country_id', '=', country.id), ('code', '=', province),
                ], limit=1)
                if state:
                    values['state_id'] = state.id
        elif code:
            _logger.warning(
                "miko_shopify: country code %s is not in Odoo; the address was "
                "imported without it", code)
        return values

    def _sync_shopify_address(self, partner, address, address_type):
        """Keep a child address contact in step, without duplicating it."""
        if not address:
            return self.env['res.partner'].browse()
        values = self._shopify_address_values(address)
        if not any(values.values()):
            return self.env['res.partner'].browse()

        label = ' '.join(p for p in [(address.get('firstName') or '').strip(),
                                     (address.get('lastName') or '').strip()] if p)
        values.update({
            'name': label or (address.get('company') or '').strip() or partner.name,
            'type': address_type,
            'parent_id': partner.id,
            'phone': (address.get('phone') or '').strip() or False,
        })

        existing = self.env['res.partner'].search([
            ('parent_id', '=', partner.id),
            ('type', '=', address_type),
            ('street', '=', values.get('street') or False),
            ('zip', '=', values.get('zip') or False),
        ], limit=1)
        if existing:
            return existing
        return self.env['res.partner'].create(values)
