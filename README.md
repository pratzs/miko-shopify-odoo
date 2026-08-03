# Shopify Connector: Orders, Products, Customers (Miko)

Connects a Shopify store to Odoo and keeps the two in step. Built on the **GraphQL
Admin API**, which Shopify has required for new apps since 1 October 2024 and is
replacing REST with.

Requires **E-Commerce Connector Engine (Miko)** (`miko_ecommerce_core`), which is
free, and provides the identity mapping and job queue underneath this.

| | |
|---|---|
| Module | `miko_shopify` |
| Series | 16.0, 17.0, 18.0, 19.0 |
| Licence | OPL-1 |
| Price | USD 449 |
| Tests | 78, all four series |

## What it does

**In:** orders, products, customers, taxes.
**Out:** products, customers, stock levels, fulfilment with tracking.
**When:** on a schedule, with a manual pull and push for every task when something
is urgent.

Direction is per entity and enforced. Products and customers can each be set to
Shopify to Odoo, Odoo to Shopify, or both, and a push against a pull-only store is
refused rather than done quietly. Orders are always inbound, because that is where
they are placed.

Fulfilment fires when the delivery is validated, or when the invoice is posted, or
whichever comes first. Businesses that never run Inventory need the invoice
trigger, or their customers are never told anything.

Which Shopify field lands in which Odoo field is configurable. Identity fields are
deliberately not: repointing those would break duplicate prevention, and everything
else rests on it.

## When something fails

Failures are kept, never dropped, and split into the two kinds that need different
things from a person:

- **Failed** is transient. The scheduler retries with exponential backoff.
- **Needs attention** means somebody has to change something first: a tax with no
  mapping, a product not in Odoo, an expired token. These are never retried on a
  timer, because a timer cannot fix them, and each one carries the sentence saying
  what to do. There is a Retry button for the moment it is fixed.

Every job keeps the exact payload that caused it, so a retry replays the failure
without going back to Shopify for the data.

## What it refuses to do

The interesting part of a connector is not what it syncs, it is what it declines to
guess. Every one of these is a failure we have seen cost real money:

- **It never imports the same order twice.** Identity goes through the mapping
  table before anything is written, so a re-run, a crashed sync or two overlapping
  schedules all converge on one order. Without this, a dropped connection halfway
  through produces two sales orders, two deliveries, two invoices and a customer
  charged twice, and unpicking that takes days.
- **It never drops a tax.** Anything Shopify sends with no Odoo equivalent stops
  and asks. The alternative is an invoice quietly short by exactly the tax amount,
  which nobody notices until a return is filed. Switchable, but never by default.
- **It never attaches a sale to a nearly-matching variant.** Odoo generates the
  full cross product of attributes; Shopify stores a flat list that is often not a
  complete matrix. A Shopify variant with no exact option match is reported and
  left unlinked rather than attached to the closest one, because the wrong size is
  a real parcel going to a real customer.
- **It never imports a total that is not what the customer paid.** Every order is
  checked against Shopify's own figure. Anything that disagrees beyond currency
  rounding stays a quotation, flagged, with the difference spelled out.
- **It never overwrites what somebody typed in Odoo.** A sync fills empty fields
  and leaves the rest, so a corrected address stays corrected.
- **It never merges contacts on a weak signal.** Exact email only. Name matching
  merges the two unrelated people called John Smith, and that does not come apart
  again once orders are attached.
- **It never confirms or invoices anything on day one.** Both are off by default,
  as are stock export, fulfilment export and the scheduler itself. Installing this
  moves no data until somebody says so.
- **It never emails your customers by surprise.** Shopify's shipping confirmation
  is off by default: switching it on over a backlog emails real people about orders
  that shipped weeks ago, and that cannot be taken back.

## Setup

1. In Shopify: **Settings > Apps and sales channels > Develop apps**, create an
   app, and grant these Admin API scopes:

   | Scope | Needed for |
   |---|---|
   | `read_products`, `write_products` | import / export catalogue |
   | `read_customers`, `write_customers` | import / export contacts |
   | `read_orders` | import orders |
   | `read_inventory`, `write_inventory` | publish stock |
   | `read_locations` | choosing where stock is written |
   | `write_merchant_managed_fulfillment_orders` | sending fulfilments |

   **`read_customers` is required even if you only import orders.** An order
   carries its customer, so without it Shopify refuses the whole order query with
   "Access denied for customer field". Found the hard way against a live store.
2. Install it, and copy the Admin API access token. Shopify shows it once.
3. In Odoo: **Store Sync > Stores**, create a store, set the platform to Shopify,
   paste the `myshopify.com` domain and the token, and press **Test Connection**.
4. Import products, then customers, then orders.

Test Connection also verifies the token belongs to the store you configured. A
token from a different store would otherwise import another company's orders.

## Design notes

**Why GraphQL.** REST product endpoints stopped supporting more than 100 variants
per product in February 2025 and no longer receive new product features. A REST
connector is a connector with a rewrite in it.

**Throttling is cost-based, not request-based.** Shopify limits query cost from a
leaky bucket and reports what is left on every response. The client reads that
figure and waits for the refill rather than firing and earning a 429, because
ignoring it works perfectly on a small store and falls over on a large one.

**GraphQL returns errors with HTTP 200.** A client that only checks the status code
treats a failure as a success and writes nothing, silently. Every response body is
inspected.

**Retries are selective.** A 429 or a 5xx is retried with backoff. A 401 is a wrong
token and will still be wrong in eight seconds, so it is reported immediately with
what to do about it.

**Tokens never reach a log.** Redacted out of every exception, message and
traceback before it can be written anywhere.

## Testing

```bash
python3 _dev/build_versions.py && _dev/certify.sh
```

Runs the suite against real `odoo:16` through `odoo:19` images. Nothing in the
suite touches the network: the HTTP client is driven against a stubbed transport
and the import code against recorded payloads, so the result is the same offline.

## Verified against a live store

Every GraphQL document in this module was run against a real Shopify development
store on API 2025-07, not just against the test stubs. What that found:

| Call | Result |
|---|---|
| product query, with variants and inventory items | executed OK |
| order query (all fields except the customer block) | executed OK |
| locations query | executed OK |
| `productCreate`, `productUpdate` | executed OK |
| `inventoryActivate`, `inventorySetQuantities` | executed OK, quantity read back |
| `customerCreate` / `customerUpdate` | schema-valid; blocked by that token's scopes |
| `fulfillmentCreate` | schema-valid; blocked by that token's scopes |

Two real bugs came out of it, neither of which a mocked test could ever have
caught:

- **`Customer.email` and `Customer.phone` no longer exist.** The Admin API moved
  them to `defaultEmailAddress { emailAddress }` and
  `defaultPhoneNumber { phoneNumber }`. Customer import would have failed outright
  against every real store. `CustomerInput` still takes flat `email` and `phone`,
  so only reading changed - an asymmetry that is very easy to get wrong.
- **Stock cannot be written to an item that is not stocked at the location.**
  Shopify returns HTTP 200 with "The specified inventory item is not stocked at
  the location" in `userErrors`. Every product Shopify created, and every product
  stocked elsewhere, hits it. The module now checks levels in bulk and calls
  `inventoryActivate` for the ones that need it before setting quantities.

## Support

support@tripsterdevelopers.com
