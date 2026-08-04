# -*- coding: utf-8 -*-
"""Tests for the two-way half: directions, exports, mapping, scheduling, retry.

Same rule as the import tests: nothing touches the network. A fake client answers
by looking at which GraphQL document it was handed, so the calling code, the
direction rules and the userErrors handling are all exercised for real.

What these deliberately do NOT prove is that Shopify accepts the exact input shape
of each mutation. Only a live store can prove that, and it is written down as the
one thing to check on a sandbox before selling this.
"""
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase


class FakeClient(object):
    """Answers by looking at which document it was given."""

    def __init__(self, product_errors=None, customer_errors=None,
                 fulfilment_errors=None, open_fulfilment=True,
                 stocked=True, tracked=True):
        self.calls = []
        self.activated = []
        self.stocked = stocked
        self.tracked = tracked
        self.product_errors = product_errors or []
        self.customer_errors = customer_errors or []
        self.fulfilment_errors = fulfilment_errors or []
        self.open_fulfilment = open_fulfilment

    def call(self, query, variables=None):
        self.calls.append((query, variables))
        if 'productCreate' in query:
            return {'productCreate': {'product': {'id': 'gid://shopify/Product/1',
                                                  'handle': 'h'},
                                      'userErrors': self.product_errors}}
        if 'productUpdate' in query:
            return {'productUpdate': {'product': {'id': 'gid://shopify/Product/1',
                                                  'handle': 'h'},
                                      'userErrors': self.product_errors}}
        if 'customerCreate' in query:
            return {'customerCreate': {'customer': {'id': 'gid://shopify/Customer/1'},
                                       'userErrors': self.customer_errors}}
        if 'customerUpdate' in query:
            return {'customerUpdate': {'customer': {'id': 'gid://shopify/Customer/1'},
                                       'userErrors': self.customer_errors}}
        if 'fulfillmentOrders' in query:
            edges = ([{'node': {'id': 'gid://shopify/FulfillmentOrder/1',
                                'status': 'OPEN'}}] if self.open_fulfilment else [])
            return {'order': {'id': 'x', 'name': '#1', 'fulfillmentOrders': {'edges': edges}}}
        if 'fulfillmentCreate' in query:
            return {'fulfillmentCreate': {
                'fulfillment': {'id': 'gid://shopify/Fulfillment/9', 'status': 'SUCCESS'},
                'userErrors': self.fulfilment_errors}}
        if 'inventoryActivate' in query:
            self.activated.append(variables.get('id'))
            return {'inventoryActivate': {'inventoryLevel': {'id': 'lvl'},
                                          'userErrors': []}}
        if 'nodes(ids:' in query.replace(' ', '') or 'inventoryLevel(locationId' in query:
            return {'nodes': [{'id': i, 'tracked': self.tracked,
                               'inventoryLevel': {'id': 'lvl'} if self.stocked else None}
                              for i in (variables or {}).get('ids') or []]}
        if 'inventorySetQuantities' in query:
            return {'inventorySetQuantities': {'inventoryAdjustmentGroup': {'id': 'g'},
                                               'userErrors': []}}
        if 'locations' in query:
            return {'locations': {'edges': [
                {'node': {'id': 'gid://shopify/Location/1', 'name': 'Main',
                          'isActive': True, 'fulfillsOnlineOrders': True}}]}}
        return {}


class SyncCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.channel = self.env['miko.ecommerce.channel'].create({
            'name': 'Two Way Store',
            'platform': 'shopify',
            'shopify_domain': 'twoway.myshopify.com',
            'company_id': self.company.id,
        })
        self.channel.sudo().shopify_token = 'shpat_x'
        self.client = FakeClient()

    def _with_client(self, client=None):
        return patch.object(type(self.channel), '_shopify_client',
                            return_value=client or self.client)


class TestDirections(SyncCase):

    def test_pushing_products_is_refused_when_the_store_pulls(self):
        """A direction is set precisely so the other way cannot happen."""
        self.channel.product_direction = 'in'
        with self._with_client():
            with self.assertRaises(UserError) as caught:
                self.channel._export_shopify_products()
        self.assertIn('Products', str(caught.exception))

    def test_pulling_products_is_refused_when_the_store_pushes(self):
        self.channel.product_direction = 'out'
        with self._with_client():
            with self.assertRaises(UserError):
                self.channel._import_shopify_products()

    def test_both_ways_allows_either(self):
        self.channel.product_direction = 'both'
        self.assertTrue(self.channel._require_direction(
            'product_direction', 'out', 'Products'))
        self.assertTrue(self.channel._require_direction(
            'product_direction', 'in', 'Products'))

    def test_customers_have_their_own_direction(self):
        self.channel.product_direction = 'out'
        self.channel.customer_direction = 'in'
        with self._with_client():
            with self.assertRaises(UserError):
                self.channel._export_shopify_customers()


class TestProductExport(SyncCase):

    def setUp(self):
        super().setUp()
        self.channel.product_direction = 'out'
        self.template = self.env['product.template'].create(
            {'name': 'Exported Widget', 'sale_ok': True})

    def test_a_product_is_created_then_linked(self):
        with self._with_client():
            self.channel._job_export_product({'template_id': self.template.id})
        found = self.env['miko.ecommerce.mapping'].find_odoo_record(
            self.channel, 'product.template', 'gid://shopify/Product/1')
        self.assertEqual(found, self.template)

    def test_a_second_export_updates_rather_than_creating_again(self):
        """A duplicate Shopify product is as bad as a duplicate order."""
        with self._with_client():
            self.channel._job_export_product({'template_id': self.template.id})
            self.client.calls.clear()
            self.channel._job_export_product({'template_id': self.template.id})
        docs = " ".join(q for q, _v in self.client.calls)
        self.assertIn('productUpdate', docs)
        self.assertNotIn('productCreate', docs)

    def test_a_refusal_in_usererrors_is_not_treated_as_success(self):
        """userErrors arrive with HTTP 200 and an otherwise valid body."""
        client = FakeClient(product_errors=[{'field': 'title', 'message': 'Title is bad'}])
        with self._with_client(client):
            with self.assertRaises(UserError) as caught:
                self.channel._job_export_product({'template_id': self.template.id})
        self.assertIn('Title is bad', str(caught.exception))

    def test_a_deleted_product_says_the_job_can_be_removed(self):
        bad = self.template.id
        self.template.unlink()
        with self._with_client():
            with self.assertRaises(UserError) as caught:
                self.channel._job_export_product({'template_id': bad})
        self.assertIn('no longer exists', str(caught.exception))


class TestCustomerExport(SyncCase):

    def setUp(self):
        super().setUp()
        self.channel.customer_direction = 'out'

    def test_a_customer_without_an_email_is_refused_with_the_reason(self):
        """Shopify matches customers by email; without one it duplicates forever."""
        partner = self.env['res.partner'].create({'name': 'No Email', 'customer_rank': 1})
        with self._with_client():
            with self.assertRaises(UserError) as caught:
                self.channel._job_export_customer({'partner_id': partner.id})
        self.assertIn('email', str(caught.exception).lower())

    def test_a_customer_is_created_then_linked(self):
        partner = self.env['res.partner'].create(
            {'name': 'Ada Lovelace', 'email': 'ada@example.com', 'customer_rank': 1})
        with self._with_client():
            self.channel._job_export_customer({'partner_id': partner.id})
        found = self.env['miko.ecommerce.mapping'].find_odoo_record(
            self.channel, 'res.partner', 'gid://shopify/Customer/1')
        self.assertEqual(found, partner)


class TestFieldMapping(SyncCase):

    def test_defaults_are_seeded_once_and_not_duplicated(self):
        self.channel._seed_field_maps()
        first = len(self.channel.field_map_ids)
        self.channel._seed_field_maps()
        self.assertEqual(len(self.channel.field_map_ids), first)

    def test_a_field_that_does_not_exist_is_refused_when_typed(self):
        """Better here than as an unexplained failure mid-sync."""
        with self.assertRaises(ValidationError):
            self.env['miko.ecommerce.field.map'].create({
                'channel_id': self.channel.id, 'entity': 'product',
                'direction': 'in', 'shopify_field': 'title',
                'odoo_field': 'not_a_real_field',
            })

    def test_a_computed_field_is_refused_because_it_cannot_be_written(self):
        with self.assertRaises(ValidationError):
            self.env['miko.ecommerce.field.map'].create({
                'channel_id': self.channel.id, 'entity': 'order',
                'direction': 'in', 'shopify_field': 'total',
                'odoo_field': 'amount_total',
            })

    def test_an_inbound_map_moves_the_value_it_is_pointed_at(self):
        self.env['miko.ecommerce.field.map'].create({
            'channel_id': self.channel.id, 'entity': 'customer',
            'direction': 'in', 'shopify_field': 'note', 'odoo_field': 'comment',
        })
        values = self.channel._apply_maps_in(
            'customer', {'note': 'VIP since 2019'}, 'res.partner')
        self.assertEqual(values.get('comment'), 'VIP since 2019')

    def test_a_key_the_payload_lacks_leaves_the_odoo_field_alone(self):
        """Absent must not mean blank, or a sync erases data it never saw."""
        self.env['miko.ecommerce.field.map'].create({
            'channel_id': self.channel.id, 'entity': 'customer',
            'direction': 'in', 'shopify_field': 'note', 'odoo_field': 'comment',
        })
        values = self.channel._apply_maps_in('customer', {}, 'res.partner')
        self.assertNotIn('comment', values)

    def test_switching_a_map_off_stops_it_applying(self):
        row = self.env['miko.ecommerce.field.map'].create({
            'channel_id': self.channel.id, 'entity': 'customer',
            'direction': 'in', 'shopify_field': 'note', 'odoo_field': 'comment',
        })
        row.active = False
        values = self.channel._apply_maps_in(
            'customer', {'note': 'ignored'}, 'res.partner')
        self.assertEqual(values, {})

    def test_the_unique_index_exists_in_the_database(self):
        self.env.cr.execute(
            "SELECT indexdef FROM pg_indexes"
            " WHERE tablename = 'miko_ecommerce_field_map'"
            "   AND indexname = 'miko_ecommerce_field_map_uniq'")
        row = self.env.cr.fetchone()
        self.assertTrue(row)
        self.assertIn('UNIQUE', row[0].upper())


class TestStockExport(SyncCase):

    def setUp(self):
        super().setUp()
        self.channel.export_stock = True

    def test_nothing_is_published_until_a_location_is_chosen(self):
        """A store with several locations would have the wrong one updated."""
        with self._with_client():
            with self.assertRaises(UserError) as caught:
                self.channel._export_shopify_stock()
        self.assertIn('location', str(caught.exception).lower())

    def test_publishing_is_refused_while_the_setting_is_off(self):
        self.channel.export_stock = False
        self.channel.shopify_location_id = 'gid://shopify/Location/1'
        with self._with_client():
            with self.assertRaises(UserError):
                self.channel._export_shopify_stock()

    def test_a_negative_quantity_is_never_sent(self):
        """Shopify rejects it, and a negative on hand is an Odoo data problem."""
        product = self.env['product.product'].create(
            {'name': 'Oversold', 'type': 'consu'})
        with patch.object(type(product), 'free_qty', -5):
            self.assertEqual(self.channel._quantity_for(product), 0)

    def test_fetching_locations_chooses_one_that_fulfils_online_orders(self):
        with self._with_client():
            self.channel.action_fetch_shopify_locations()
        self.assertEqual(self.channel.shopify_location_id, 'gid://shopify/Location/1')
        self.assertEqual(self.channel.shopify_location_name, 'Main')

    def test_only_mapped_variants_are_ever_touched(self):
        self.env['product.product'].create({'name': 'Never Synced', 'type': 'consu'})
        self.assertFalse(self.channel._stock_export_products())


class TestScheduledSync(SyncCase):

    def test_installing_changes_nothing_until_it_is_switched_on(self):
        self.assertFalse(self.channel.auto_sync)
        ran = []
        with patch.object(type(self.channel), '_run_scheduled_sync',
                          side_effect=lambda: ran.append(1)):
            self.env['miko.ecommerce.channel']._cron_shopify_sync()
        self.assertEqual(ran, [], 'a store must not sync until asked to')

    def test_a_store_that_is_not_due_yet_is_skipped(self):
        self.channel.write({'auto_sync': True, 'sync_interval_minutes': 60})
        self.channel.last_sync = fields.Datetime.now()
        self.assertFalse(self.channel._sync_is_due())

    def test_a_store_already_running_is_never_started_twice(self):
        """Odoo's scheduler will fire again while the last run is still going."""
        self.channel.write({'auto_sync': True, 'sync_running': True})
        self.assertFalse(self.channel._sync_is_due())

    def test_a_store_that_has_never_synced_is_due(self):
        self.channel.write({'auto_sync': True, 'last_sync': False})
        self.assertTrue(self.channel._sync_is_due())

    def test_a_failing_sync_clears_the_running_flag(self):
        """Otherwise the store is stuck as 'running' and never syncs again."""
        self.channel.write({'auto_sync': True, 'sync_orders': True})
        with patch.object(type(self.channel), '_import_shopify_orders',
                          side_effect=Exception('boom')):
            self.channel._run_scheduled_sync()
        self.assertFalse(self.channel.sync_running)
        self.assertIn('Failed', self.channel.last_sync_message or '')


class TestFulfilmentTriggers(SyncCase):

    def setUp(self):
        super().setUp()
        self.channel.write({'export_fulfilments': True})
        self.partner = self.env['res.partner'].create({'name': 'Buyer'})
        self.order = self.env['sale.order'].create({'partner_id': self.partner.id})
        self.env['miko.ecommerce.mapping'].link(
            self.channel, self.order, 'gid://shopify/Order/55', '#55')

    def test_the_delivery_trigger_does_not_fire_on_an_invoice_store(self):
        self.channel.fulfil_trigger = 'invoice'
        self.assertFalse(self.channel._fulfilment_wanted('delivery'))
        self.assertTrue(self.channel._fulfilment_wanted('invoice'))

    def test_both_means_whichever_happens_first(self):
        self.channel.fulfil_trigger = 'both'
        self.assertTrue(self.channel._fulfilment_wanted('delivery'))
        self.assertTrue(self.channel._fulfilment_wanted('invoice'))

    def test_nothing_is_sent_while_the_setting_is_off(self):
        self.channel.export_fulfilments = False
        self.assertFalse(self.channel._fulfilment_wanted('delivery'))

    def test_posting_a_fulfilment_returns_the_shopify_id(self):
        with self._with_client():
            created = self.channel.post_shopify_fulfilment(self.order, '1Z999', 'UPS')
        self.assertEqual(created, 'gid://shopify/Fulfillment/9')
        sent = [v for q, v in self.client.calls if 'fulfillmentCreate' in q][0]
        self.assertEqual(sent['fulfillment']['trackingInfo']['number'], '1Z999')

    def test_the_customer_is_not_emailed_unless_asked(self):
        """Turning this on over a backlog emails real people. Not recoverable."""
        with self._with_client():
            self.channel.post_shopify_fulfilment(self.order)
        sent = [v for q, v in self.client.calls if 'fulfillmentCreate' in q][0]
        self.assertFalse(sent['fulfillment']['notifyCustomer'])

    def test_an_order_with_nothing_open_is_not_posted_against(self):
        client = FakeClient(open_fulfilment=False)
        with self._with_client(client):
            self.assertFalse(self.channel.post_shopify_fulfilment(self.order))

    def test_a_refused_fulfilment_becomes_a_blocked_job_with_advice(self):
        client = FakeClient(fulfilment_errors=[{'message': 'Already fulfilled'}])
        with self._with_client(client):
            self.assertFalse(self.channel.post_shopify_fulfilment(self.order))
        job = self.env['miko.ecommerce.job'].search(
            [('channel_id', '=', self.channel.id),
             ('operation', '=', 'export_fulfilment')], limit=1)
        self.assertEqual(job.state, 'blocked')
        self.assertTrue(job.guidance)

    def test_an_order_from_another_store_is_not_this_channels_business(self):
        other = self.env['sale.order'].create({'partner_id': self.partner.id})
        with self._with_client():
            self.assertFalse(self.channel.post_shopify_fulfilment(other))


class TestRetryPath(SyncCase):

    def test_retrying_an_order_import_cannot_create_a_second_order(self):
        """The most expensive thing Retry could possibly get wrong."""
        node = {
            'id': 'gid://shopify/Order/77', 'name': '#77',
            'createdAt': '2026-02-01T00:00:00Z', 'cancelledAt': None,
            'currentTotalPriceSet': {'shopMoney': {'amount': '10.00',
                                                   'currencyCode': 'NZD'}},
            'customer': None, 'shippingLines': {'edges': []},
            'lineItems': {'edges': [{'node': {
                'id': 'li', 'title': 'Thing', 'quantity': 1, 'sku': 'T-1',
                'variant': {'id': 'gid://shopify/ProductVariant/8'},
                'originalUnitPriceSet': {'shopMoney': {'amount': '10.00',
                                                       'currencyCode': 'NZD'}},
                'discountedUnitPriceSet': {'shopMoney': {'amount': '10.00',
                                                         'currencyCode': 'NZD'}},
                'taxLines': [],
            }}]},
        }
        self.channel.default_customer_id = self.env['res.partner'].create(
            {'name': 'Guest'})
        first = self.channel._job_import_order(node)
        again = self.channel._job_import_order(node)
        self.assertEqual(first, again, 'a retry must return the same order')
        self.assertEqual(self.env['sale.order'].search_count(
            [('shopify_order_name', '=', '#77')]), 1)


class TestStockActivation(SyncCase):
    """Shopify refuses a quantity for an item not stocked at the location.

    Found against a live store, not by the suite: the mutation returns HTTP 200
    with "The specified inventory item is not stocked at the location" in
    userErrors. Every product Shopify created, and every product stocked
    somewhere else, hits it.
    """

    def setUp(self):
        super().setUp()
        self.channel.write({'export_stock': True,
                            'shopify_location_id': 'gid://shopify/Location/1'})

    def test_an_unstocked_item_is_activated_before_the_quantity_is_set(self):
        client = FakeClient(stocked=False)
        self.channel._ensure_stocked(client, ['gid://shopify/InventoryItem/1'])
        self.assertEqual(client.activated, ['gid://shopify/InventoryItem/1'])

    def test_an_item_already_stocked_costs_no_extra_call(self):
        client = FakeClient(stocked=True)
        self.channel._ensure_stocked(client, ['gid://shopify/InventoryItem/1'])
        self.assertEqual(client.activated, [],
                         'activating something already stocked is a wasted call')

    def test_nothing_to_stock_makes_no_call_at_all(self):
        client = FakeClient()
        self.assertEqual(self.channel._ensure_stocked(client, []), [])
        self.assertEqual(client.calls, [])

    def test_untracked_items_are_reported_rather_than_changed(self):
        """Turning tracking on decides whether the shop can oversell.

        That is the owner's merchandising decision, not a side effect of
        publishing a number, so it is logged and left alone.
        """
        client = FakeClient(stocked=True, tracked=False)
        self.channel._ensure_stocked(client, ['gid://shopify/InventoryItem/1'])
        self.assertEqual(client.activated, [])


class TestCustomerFieldShape(SyncCase):
    """Customer.email and Customer.phone were REMOVED from the Admin API.

    2025-07 exposes them as defaultEmailAddress { emailAddress } and
    defaultPhoneNumber { phoneNumber }. Confirmed by introspecting a live store.
    CustomerInput still takes flat email and phone, so only reading changed.
    """

    def test_the_nested_shape_is_flattened(self):
        from ..models.shopify_customer import flatten_customer
        node = flatten_customer({
            'id': 'gid://shopify/Customer/1',
            'defaultEmailAddress': {'emailAddress': 'ada@example.com'},
            'defaultPhoneNumber': {'phoneNumber': '+6421000000'},
        })
        self.assertEqual(node['email'], 'ada@example.com')
        self.assertEqual(node['phone'], '+6421000000')

    def test_a_flat_payload_still_works(self):
        """Jobs queued before this change must still replay correctly."""
        from ..models.shopify_customer import flatten_customer
        node = flatten_customer({'id': 'x', 'email': 'old@example.com'})
        self.assertEqual(node['email'], 'old@example.com')

    def test_a_customer_with_neither_does_not_explode(self):
        from ..models.shopify_customer import flatten_customer
        node = flatten_customer({'id': 'x'})
        self.assertEqual(node['email'], '')
        self.assertEqual(node['phone'], '')

    def test_the_query_asks_for_fields_that_exist(self):
        """Guards the regression directly: the old names must not come back."""
        from ..models.shopify_customer import CUSTOMER_QUERY
        self.assertIn('defaultEmailAddress', CUSTOMER_QUERY)
        self.assertNotIn('lastName email', CUSTOMER_QUERY)
