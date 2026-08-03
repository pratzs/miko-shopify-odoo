# -*- coding: utf-8 -*-
"""Tests for the Shopify connector.

Nothing here touches the network. Every test either drives the import code with a
recorded payload, or drives the HTTP client against a stubbed transport, so the
suite gives the same answer on a laptop with no internet as it does in CI.

The tests are written around the ways a connector loses money or duplicates data,
because those are the failures that matter: importing twice, attaching a sale to
the wrong variant, dropping a tax, and importing a total that is not what the
customer paid.
"""
import json

from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase

from ..models.shopify_client import ShopifyClient, ShopifyError, _redact


def gid(kind, number):
    return 'gid://shopify/%s/%s' % (kind, number)


def money(amount, currency='NZD'):
    return {'shopMoney': {'amount': str(amount), 'currencyCode': currency}}


class FakeResponse(object):
    def __init__(self, status_code=200, body=None, headers=None, text=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(body or {})

    def json(self):
        if self._body is None:
            raise ValueError('not json')
        return self._body


class ShopifyCase(TransactionCase):
    """Shared fixtures.

    setUp rather than setUpClass on purpose: cls.env in setUpClass only works
    from Odoo 15, and this module supports 16 through 19 from one source tree.
    """

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.channel = self.env['miko.ecommerce.channel'].create({
            'name': 'Test Store',
            'platform': 'shopify',
            'shopify_domain': 'testshop.myshopify.com',
            'shopify_currency': self.company.currency_id.name,
            'company_id': self.company.id,
        })
        self.channel.sudo().shopify_token = 'shpat_testtoken0000'

    def _tax_group(self):
        """A tax group that satisfies this series' schema.

        account.tax.tax_group_id is NOT NULL on 17 and 18 with no usable default,
        while 16 and 19 fill it themselves. The group model's own required fields
        also differ by series, so this probes the registry rather than assuming a
        shape: guessing produced a NotNullViolation naming a column, which reads
        like a broken schema instead of a broken fixture.
        """
        Group = self.env['account.tax.group']
        domain = [('company_id', '=', self.company.id)] if 'company_id' in Group._fields else []
        group = Group.search(domain, limit=1)
        if group:
            return group
        values = {'name': 'Miko Test Group'}
        if 'company_id' in Group._fields:
            values['company_id'] = self.company.id
        if 'country_id' in Group._fields:
            values['country_id'] = self._country().id
        return Group.create(values)

    def _country(self):
        return (self.company.account_fiscal_country_id
                or self.company.country_id
                or self.env['res.country'].search([('code', '=', 'NZ')], limit=1))

    def _tax(self, name, amount):
        """A sale tax that will actually insert on every series.

        country_id and tax_group_id are both NOT NULL on account_tax, and omitting
        either fails in the database rather than the ORM.
        """
        return self.env['account.tax'].create({
            'name': name, 'amount': amount, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'company_id': self.company.id,
            'country_id': self._country().id,
            'tax_group_id': self._tax_group().id,
        })


class TestClient(ShopifyCase):
    """The HTTP layer, against a stubbed transport."""

    def test_a_token_is_never_written_into_an_error(self):
        leaked = "connection failed for shpat_abc123DEF456 at host"
        self.assertNotIn('abc123DEF456', _redact(leaked))
        self.assertIn('shpat_***', _redact(leaked))

    def test_a_wrong_token_says_so_instead_of_retrying(self):
        client = ShopifyClient('testshop.myshopify.com', 'shpat_x')
        calls = []

        def post(*args, **kwargs):
            calls.append(1)
            return FakeResponse(401, {})

        with patch.object(client._session, 'post', side_effect=post):
            with self.assertRaises(ShopifyError) as caught:
                client.call('query { shop { name } }')
        # Retrying a 401 only makes the user wait longer for the same answer.
        self.assertEqual(len(calls), 1, 'a 401 must not be retried')
        self.assertIn('access token', str(caught.exception))

    def test_a_429_is_retried_and_then_succeeds(self):
        client = ShopifyClient('testshop.myshopify.com', 'shpat_x')
        responses = [
            FakeResponse(429, {}, headers={'Retry-After': '0'}),
            FakeResponse(200, {'data': {'shop': {'name': 'Test Store'}}}),
        ]

        with patch.object(client._session, 'post', side_effect=responses), \
                patch('odoo.addons.miko_shopify.models.shopify_client.time.sleep'):
            data = client.call('query { shop { name } }')
        self.assertEqual(data['shop']['name'], 'Test Store')

    def test_graphql_errors_arrive_with_http_200_and_must_not_pass(self):
        """The failure mode that makes a sync silently do nothing."""
        client = ShopifyClient('testshop.myshopify.com', 'shpat_x')
        body = {'errors': [{'message': 'Field is not defined',
                            'extensions': {'code': 'undefinedField'}}]}
        with patch.object(client._session, 'post', return_value=FakeResponse(200, body)):
            with self.assertRaises(ShopifyError):
                client.call('query { nope }')

    def test_a_missing_scope_explains_how_to_fix_it(self):
        client = ShopifyClient('testshop.myshopify.com', 'shpat_x')
        body = {'errors': [{'message': 'Access denied',
                            'extensions': {'code': 'ACCESS_DENIED',
                                           'requiredAccess': 'read_orders'}}]}
        with patch.object(client._session, 'post', return_value=FakeResponse(200, body)):
            with self.assertRaises(ShopifyError) as caught:
                client.call('query { orders { edges { node { id } } } }')
        message = str(caught.exception)
        self.assertIn('read_orders', message)
        self.assertIn('reinstall', message.lower())

    def test_html_instead_of_json_names_the_likely_cause(self):
        client = ShopifyClient('testshop.myshopify.com', 'shpat_x')
        with patch.object(client._session, 'post',
                          return_value=FakeResponse(200, None, text='<html>')):
            with self.assertRaises(ShopifyError) as caught:
                client.call('query { shop { name } }')
        self.assertIn('myshopify.com', str(caught.exception))

    def test_pagination_follows_the_cursor_to_the_end(self):
        client = ShopifyClient('testshop.myshopify.com', 'shpat_x')
        pages = [
            {'things': {'edges': [{'node': {'id': 1}}, {'node': {'id': 2}}],
                        'pageInfo': {'hasNextPage': True, 'endCursor': 'c1'}}},
            {'things': {'edges': [{'node': {'id': 3}}],
                        'pageInfo': {'hasNextPage': False, 'endCursor': None}}},
        ]
        seen_cursors = []

        def fake_call(query, variables=None):
            seen_cursors.append((variables or {}).get('after'))
            return pages[len(seen_cursors) - 1]

        with patch.object(client, 'call', side_effect=fake_call):
            nodes = list(client.paginate('query', {}, 'things'))
        self.assertEqual([n['id'] for n in nodes], [1, 2, 3])
        self.assertEqual(seen_cursors, [None, 'c1'])

    def test_no_credentials_is_refused_before_any_request(self):
        with self.assertRaises(ShopifyError):
            ShopifyClient('', '')


class TestChannel(ShopifyCase):

    def test_a_custom_domain_is_refused(self):
        """It serves the storefront but not the Admin API, so it 404s later."""
        with self.assertRaises(ValidationError):
            self.channel.shopify_domain = 'www.example.com'

    def _draft_with_domain(self, typed):
        """A record that is not saved yet, which is where an onchange runs.

        Assigning to a stored field on a saved record writes it, and the write
        fires the constraint before the onchange ever gets to repair the value.
        That is correct behaviour on both sides; it just means an onchange has to
        be tested on a draft, the way the form actually uses it.
        """
        return self.env['miko.ecommerce.channel'].new({
            'name': 'Draft', 'platform': 'shopify', 'shopify_domain': typed})

    def test_a_pasted_url_is_repaired_rather_than_rejected(self):
        draft = self._draft_with_domain('https://testshop.myshopify.com/admin/products')
        draft._onchange_shopify_domain()
        self.assertEqual(draft.shopify_domain, 'testshop.myshopify.com')

    def test_a_bare_handle_becomes_a_full_domain(self):
        draft = self._draft_with_domain('testshop')
        draft._onchange_shopify_domain()
        self.assertEqual(draft.shopify_domain, 'testshop.myshopify.com')

    def test_a_token_for_a_different_store_is_refused(self):
        """Otherwise every sync imports another company's orders into this one."""
        class Client(object):
            def shop(self):
                return {'name': 'Someone Else', 'myshopifyDomain': 'other.myshopify.com'}

        with patch.object(type(self.channel), '_shopify_client', return_value=Client()):
            self.channel.action_test_connection()
        self.assertEqual(self.channel.connection_state, 'error')
        self.assertIn('other.myshopify.com', self.channel.connection_message)

    def test_a_working_connection_records_what_it_found(self):
        class Client(object):
            def shop(self):
                return {'name': 'Test Store',
                        'myshopifyDomain': 'testshop.myshopify.com',
                        'currencyCode': 'NZD'}

        with patch.object(type(self.channel), '_shopify_client', return_value=Client()):
            self.channel.action_test_connection()
        self.assertEqual(self.channel.connection_state, 'ok')
        self.assertEqual(self.channel.shopify_shop_name, 'Test Store')

    def test_a_failure_is_reported_not_raised(self):
        """One broken store must not stop the others being tested."""
        class Client(object):
            def shop(self):
                raise ShopifyError('nope')

        with patch.object(type(self.channel), '_shopify_client', return_value=Client()):
            self.channel.action_test_connection()
        self.assertEqual(self.channel.connection_state, 'error')


class TestProducts(ShopifyCase):

    def _product_node(self, variants, options=None):
        return {
            'id': gid('Product', 100),
            'title': 'Test Shirt',
            'handle': 'test-shirt',
            'options': options or [],
            'variants': {'edges': [{'node': v} for v in variants]},
        }

    def test_a_simple_product_imports_once_and_stays_one_product(self):
        node = self._product_node([{
            'id': gid('ProductVariant', 200), 'sku': 'SHIRT-1', 'barcode': '',
            'price': '25.00', 'title': 'Default Title',
            'selectedOptions': [{'name': 'Title', 'value': 'Default Title'}],
        }])
        template = self.channel._upsert_shopify_template(node)
        self.channel._upsert_shopify_variants(node, template)

        again = self.channel._upsert_shopify_template(node)
        self.assertEqual(template, again, 're-running must not create a second product')
        self.assertEqual(
            self.env['product.template'].search_count([('name', '=', 'Test Shirt')]), 1)

    def test_a_variant_is_matched_on_its_exact_option_values(self):
        options = [{'name': 'Colour', 'values': ['Blue', 'Red']},
                   {'name': 'Size', 'values': ['S', 'M']}]
        variants = [{
            'id': gid('ProductVariant', 201), 'sku': 'BLUE-S', 'barcode': '',
            'price': '25.00', 'title': 'Blue / S',
            'selectedOptions': [{'name': 'Colour', 'value': 'Blue'},
                                {'name': 'Size', 'value': 'S'}],
        }]
        node = self._product_node(variants, options)
        template = self.channel._upsert_shopify_template(node)
        self.channel._upsert_shopify_variants(node, template)

        product = self.env['miko.ecommerce.mapping'].find_odoo_record(
            self.channel, 'product.product', gid('ProductVariant', 201))
        self.assertTrue(product, 'the blue small variant should have been linked')
        self.assertEqual(product.default_code, 'BLUE-S')
        labels = {v.name for v in product.product_template_attribute_value_ids}
        self.assertEqual(labels, {'Blue', 'S'})

    def test_a_variant_with_no_odoo_equivalent_is_left_unlinked(self):
        """Never attach a sale to a nearly-matching variant."""
        options = [{'name': 'Colour', 'values': ['Blue']}]
        node = self._product_node([{
            'id': gid('ProductVariant', 202), 'sku': 'GREEN-S', 'barcode': '',
            'price': '25.00', 'title': 'Green / S',
            'selectedOptions': [{'name': 'Colour', 'value': 'Green'}],
        }], options)
        template = self.channel._upsert_shopify_template(node)
        self.channel._upsert_shopify_variants(node, template)

        product = self.env['miko.ecommerce.mapping'].find_odoo_record(
            self.channel, 'product.product', gid('ProductVariant', 202))
        self.assertFalse(product, 'a green variant must not be linked to the blue one')

    def test_a_clashing_barcode_is_skipped_rather_than_aborting_the_import(self):
        self.env['product.product'].create({'name': 'Existing', 'barcode': '9400000000001'})
        node = self._product_node([{
            'id': gid('ProductVariant', 203), 'sku': 'DUP-1',
            'barcode': '9400000000001', 'price': '10.00', 'title': 'Default Title',
            'selectedOptions': [{'name': 'Title', 'value': 'Default Title'}],
        }])
        template = self.channel._upsert_shopify_template(node)
        self.channel._upsert_shopify_variants(node, template)      # must not raise

        product = self.env['miko.ecommerce.mapping'].find_odoo_record(
            self.channel, 'product.product', gid('ProductVariant', 203))
        self.assertTrue(product)
        self.assertEqual(product.default_code, 'DUP-1')
        self.assertNotEqual(product.barcode, '9400000000001')


class TestCustomers(ShopifyCase):

    def _customer_node(self, number=300, email='buyer@example.com'):
        return {
            'id': gid('Customer', number),
            'firstName': 'Ada', 'lastName': 'Lovelace',
            'email': email, 'phone': '+6421000000',
            'defaultAddress': {
                'firstName': 'Ada', 'lastName': 'Lovelace', 'company': '',
                'address1': '1 Queen Street', 'address2': '', 'city': 'Auckland',
                'provinceCode': '', 'zip': '1010', 'countryCodeV2': 'NZ',
                'phone': '+6421000000',
            },
        }

    def test_an_existing_contact_is_reused_on_an_exact_email_match(self):
        existing = self.env['res.partner'].create({
            'name': 'Ada L', 'email': 'buyer@example.com'})
        partner = self.channel._upsert_shopify_customer(self._customer_node())
        self.assertEqual(partner, existing, 'must not create a duplicate contact')

    def test_a_value_typed_in_odoo_is_never_overwritten(self):
        existing = self.env['res.partner'].create({
            'name': 'Ada L', 'email': 'buyer@example.com',
            'street': '99 Corrected Road'})
        partner = self.channel._upsert_shopify_customer(self._customer_node())
        self.assertEqual(partner.street, '99 Corrected Road')

    def test_the_country_is_resolved_from_the_iso_code(self):
        partner = self.channel._upsert_shopify_customer(self._customer_node())
        self.assertEqual(partner.country_id.code, 'NZ')

    def test_a_customer_with_no_name_still_gets_a_findable_one(self):
        node = self._customer_node(number=301, email='noname@example.com')
        node['firstName'] = node['lastName'] = ''
        partner = self.channel._upsert_shopify_customer(node)
        self.assertEqual(partner.name, 'noname@example.com')

    def test_importing_the_same_customer_twice_creates_one_contact(self):
        node = self._customer_node(number=302, email='twice@example.com')
        first = self.channel._upsert_shopify_customer(node)
        second = self.channel._upsert_shopify_customer(node)
        self.assertEqual(first, second)


class TestOrders(ShopifyCase):

    def setUp(self):
        super().setUp()
        self.product = self.env['product.product'].create({
            'name': 'Imported Widget', 'default_code': 'WIDGET-1',
            'type': 'consu', 'list_price': 100.0,
        })
        self.channel.default_customer_id = self.env['res.partner'].create(
            {'name': 'Shopify guest'})

    def _order_node(self, number=400, tax_lines=None, total='115.00', qty=1):
        return {
            'id': gid('Order', number),
            'name': '#%s' % number,
            'createdAt': '2026-01-15T03:04:05Z',
            'cancelledAt': None,
            'currentTotalPriceSet': money(total),
            'customer': None,
            'shippingLines': {'edges': []},
            'lineItems': {'edges': [{'node': {
                'id': gid('LineItem', 500), 'title': 'Imported Widget',
                'quantity': qty, 'sku': 'WIDGET-1',
                'variant': {'id': gid('ProductVariant', 600)},
                'originalUnitPriceSet': money('100.00'),
                'discountedUnitPriceSet': money('100.00'),
                'taxLines': tax_lines if tax_lines is not None else [],
            }}]},
        }

    def test_an_order_imports_with_the_right_line_and_total(self):
        order = self.channel._import_one_shopify_order(
            self._order_node(total='100.00'))
        self.assertEqual(len(order.order_line), 1)
        self.assertEqual(order.order_line.product_id, self.product)
        self.assertEqual(order.amount_total, 100.0)
        self.assertTrue(order.shopify_total_matches)
        self.assertEqual(order.shopify_order_name, '#400')

    def test_importing_the_same_order_twice_creates_one_order(self):
        """The failure that costs days to unpick."""
        node = self._order_node(number=401, total='100.00')
        self.channel._import_one_shopify_order(node)
        Mapping = self.env['miko.ecommerce.mapping']
        self.assertTrue(Mapping.already_imported(self.channel, 'sale.order', node['id']))
        self.assertEqual(
            self.env['sale.order'].search_count([('shopify_order_name', '=', '#401')]), 1)

    def test_a_line_discount_is_kept_as_a_discount(self):
        node = self._order_node(number=402, total='80.00')
        node['lineItems']['edges'][0]['node']['discountedUnitPriceSet'] = money('80.00')
        order = self.channel._import_one_shopify_order(node)
        self.assertEqual(order.order_line.price_unit, 100.0)
        self.assertAlmostEqual(order.order_line.discount, 20.0, places=4)
        self.assertAlmostEqual(order.amount_total, 80.0, places=2)

    def test_a_total_that_disagrees_is_flagged_and_left_as_a_quotation(self):
        """Shopify says 150, the lines add to 100. Never invoice that."""
        order = self.channel._import_one_shopify_order(
            self._order_node(number=403, total='150.00'))
        self.assertFalse(order.shopify_total_matches)
        self.assertEqual(order.state, 'draft')

    def test_a_flagged_order_is_not_auto_confirmed(self):
        self.channel.auto_confirm_orders = True
        order = self.channel._import_one_shopify_order(
            self._order_node(number=404, total='150.00'))
        self.assertEqual(order.state, 'draft')

    def test_an_unmapped_tax_stops_the_import_by_default(self):
        """Better a refused order than an invoice short by the tax."""
        self.assertEqual(self.channel.unmapped_tax_policy, 'block')
        node = self._order_node(
            number=405, total='115.00',
            tax_lines=[{'title': 'Unknown Duty', 'rate': 0.075,
                        'priceSet': money('7.50')}])
        with self.assertRaises(UserError) as caught:
            self.channel._import_one_shopify_order(node)
        self.assertIn('Unknown Duty', str(caught.exception))

    def test_an_unmapped_tax_is_recorded_so_it_can_be_mapped(self):
        node = self._order_node(
            number=406, total='115.00',
            tax_lines=[{'title': 'Unknown Duty', 'rate': 0.075,
                        'priceSet': money('7.50')}])
        try:
            self.channel._import_one_shopify_order(node)
        except UserError:
            pass
        row = self.env['miko.shopify.tax'].search([
            ('channel_id', '=', self.channel.id), ('title', '=', 'Unknown Duty')])
        self.assertTrue(row, 'the tax must be listed so somebody can map it')

    def test_a_mapped_tax_is_applied_to_the_line(self):
        tax = self._tax('GST 15', 15.0)
        self.env['miko.shopify.tax'].create({
            'channel_id': self.channel.id, 'title': 'GST', 'rate': 0.15,
            'tax_id': tax.id,
        })
        node = self._order_node(
            number=407, total='115.00',
            tax_lines=[{'title': 'GST', 'rate': 0.15, 'priceSet': money('15.00')}])
        order = self.channel._import_one_shopify_order(node)
        field = self.channel._sol_tax_field()
        self.assertIn(tax, order.order_line[field])
        self.assertAlmostEqual(order.amount_total, 115.0, places=2)
        self.assertTrue(order.shopify_total_matches)

    def test_ignoring_unmapped_taxes_is_possible_but_never_the_default(self):
        self.channel.unmapped_tax_policy = 'ignore'
        node = self._order_node(
            number=408, total='100.00',
            tax_lines=[{'title': 'Unknown Duty', 'rate': 0.075,
                        'priceSet': money('7.50')}])
        order = self.channel._import_one_shopify_order(node)
        self.assertTrue(order)

    def test_a_guest_order_uses_the_fallback_customer(self):
        order = self.channel._import_one_shopify_order(
            self._order_node(number=409, total='100.00'))
        self.assertEqual(order.partner_id, self.channel.default_customer_id)

    def test_a_guest_order_with_no_fallback_says_exactly_what_to_set(self):
        self.channel.default_customer_id = False
        with self.assertRaises(UserError) as caught:
            self.channel._import_one_shopify_order(
                self._order_node(number=410, total='100.00'))
        self.assertIn('fallback customer', str(caught.exception))

    def test_an_unknown_product_can_be_refused_instead_of_invented(self):
        self.channel.create_missing_products = False
        node = self._order_node(number=411, total='100.00')
        node['lineItems']['edges'][0]['node']['sku'] = 'NOT-IN-ODOO'
        node['lineItems']['edges'][0]['node']['variant'] = {'id': gid('ProductVariant', 999)}
        with self.assertRaises(UserError) as caught:
            self.channel._import_one_shopify_order(node)
        self.assertIn('NOT-IN-ODOO', str(caught.exception))

    def test_shipping_arrives_as_its_own_line(self):
        node = self._order_node(number=412, total='110.00')
        node['shippingLines'] = {'edges': [{'node': {
            'title': 'Standard shipping',
            'originalPriceSet': money('10.00'),
            'taxLines': [],
        }}]}
        order = self.channel._import_one_shopify_order(node)
        self.assertEqual(len(order.order_line), 2)
        self.assertAlmostEqual(order.amount_total, 110.0, places=2)
        self.assertTrue(order.shopify_total_matches)

    def test_a_cancelled_order_is_recorded_but_never_confirmed(self):
        self.channel.auto_confirm_orders = True
        node = self._order_node(number=413, total='100.00')
        node['cancelledAt'] = '2026-01-16T00:00:00Z'
        order = self.channel._import_one_shopify_order(node)
        self.assertEqual(order.state, 'draft')

    def test_the_order_date_comes_from_shopify_not_from_now(self):
        order = self.channel._import_one_shopify_order(
            self._order_node(number=414, total='100.00'))
        self.assertEqual(order.date_order.year, 2026)
        self.assertEqual(order.date_order.month, 1)
        self.assertEqual(order.date_order.day, 15)


class TestTaxMapping(ShopifyCase):

    def test_an_unambiguous_rate_is_suggested(self):
        tax = self._tax('Odd Rate 13.5', 13.5)
        found = self.env['miko.shopify.tax'].resolve(
            self.channel, {'title': 'ODD', 'rate': 0.135})
        self.assertEqual(found, tax)

    def test_an_ambiguous_rate_is_never_guessed(self):
        """Two candidates at the same rate means somebody has to choose."""
        self._tax('Odd Rate A', 13.5)
        self._tax('Odd Rate B', 13.5)
        found = self.env['miko.shopify.tax'].resolve(
            self.channel, {'title': 'ODD', 'rate': 0.135})
        self.assertFalse(found, 'guessing between two taxes is not allowed')

    def test_the_same_tax_is_only_recorded_once(self):
        Tax = self.env['miko.shopify.tax']
        Tax.resolve(self.channel, {'title': 'GST', 'rate': 0.15})
        Tax.resolve(self.channel, {'title': 'GST', 'rate': 0.15})
        rows = Tax.search([('channel_id', '=', self.channel.id), ('title', '=', 'GST')])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.order_count, 2)

    def test_the_unique_index_exists_in_the_database(self):
        self.env.cr.execute(
            "SELECT indexdef FROM pg_indexes"
            " WHERE tablename = 'miko_shopify_tax'"
            "   AND indexname = 'miko_shopify_tax_uniq'")
        row = self.env.cr.fetchone()
        self.assertTrue(row, 'the unique index must exist on every series')
        self.assertIn('UNIQUE', row[0].upper())
