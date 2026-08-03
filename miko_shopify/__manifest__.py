# -*- coding: utf-8 -*-
{
    'name': 'Shopify Odoo Connector: Orders, Stock, Fulfilment Sync (Miko)',
    'version': '19.0.1.0.0',
    'summary': 'Two-way Shopify Odoo sync on a schedule: import orders, products '
               'and customers, publish stock and fulfilment tracking',
    'description': """
Connect a Shopify store to Odoo and keep the two in step.

Built on the GraphQL Admin API, which Shopify has required for new apps since
October 2024 and is replacing REST with, so this does not need rewriting when the
REST endpoints finish being withdrawn.

What it will not do is as important as what it will. It never imports the same
order twice, whatever happens mid-sync. It never invents a tax: anything Shopify
sends without an Odoo equivalent stops and asks rather than quietly producing an
invoice short by the tax. It never attaches a variant to a nearly-matching one. And
it checks every imported order against the total Shopify actually charged, leaving
anything that disagrees as a quotation with the difference spelled out.
""",
    'author': 'Tripster Developers',
    'website': 'https://tripsterdevelopers.com/odoo/',
    'category': 'eCommerce',
    'license': 'OPL-1',
    'depends': ['miko_ecommerce_core', 'sale_management', 'stock', 'account'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/miko_shopify_views.xml',
    ],
    'images': ['images/banner.png'],
    'price': 449.00,
    'currency': 'USD',
    'application': False,
    'installable': True,
    'support': 'support@tripsterdevelopers.com',
}
