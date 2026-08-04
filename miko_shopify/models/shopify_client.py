# -*- coding: utf-8 -*-
"""The Shopify Admin API client.

**GraphQL, not REST, and that is a deliberate decision rather than a preference.**
Shopify made the GraphQL Admin API mandatory for all new public apps from
1 October 2024, and began removing REST product endpoints in February 2025: the
REST product API stopped supporting more than 100 variants per product and no
longer receives new product features. A connector written against REST today is a
connector that has to be rewritten, and its users are the ones who find out.

Everything below exists because of something that actually goes wrong against a
live store:

* **Cost-based throttling.** Shopify does not limit requests, it limits query
  COST, from a leaky bucket that refills at a fixed rate. Every response reports
  how much is left. Ignoring that figure works perfectly on a small store and
  falls over on a large one, which is the worst possible way for a bug to behave.
* **Retries have to be selective.** A 429 or a 5xx is worth retrying. A 401 is a
  wrong token and will still be wrong in eight seconds' time, so retrying it just
  makes the user wait longer before seeing the real message.
* **GraphQL returns errors with HTTP 200.** A client that only checks the status
  code treats a failure as a success and writes nothing, silently.
* **The token must never reach a log.** Not in an exception, not in a traceback,
  not in a debug line.
"""
import json
import logging
import re
import time

import requests

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# The version pinned here is a stable release, not the release candidate.
# Shopify supports each quarterly version for a year; pinning means Shopify
# cannot change the shape of a response underneath a running integration.
DEFAULT_API_VERSION = '2025-07'

TIMEOUT = 30            # seconds; a hung socket must not hold an Odoo worker
MAX_ATTEMPTS = 5
RETRY_STATUS = {429, 500, 502, 503, 504}

# Below this many cost points left in the bucket, wait for it to refill rather
# than firing the next call and being rejected.
COST_FLOOR = 100


class ShopifyError(UserError):
    """A Shopify problem stated in terms the user can act on."""


# Shopify access tokens all carry one of these prefixes followed by an
# alphanumeric body. Matched as a whole in one pass: an earlier version of this
# walked the string replacing matches in a while loop and re-inserted the prefix
# it had just matched, so the loop never terminated and the string grew forever.
# It ran on every error message, so any Shopify failure would have hung a worker.
TOKEN_RE = re.compile(r'(shpat_|shpca_|shppa_|shpss_)[A-Za-z0-9_]+')


def _redact(text):
    """Never let a token reach a log or an error message."""
    if not text:
        return text
    return TOKEN_RE.sub(lambda m: m.group(1) + '***', str(text))


class ShopifyClient(object):
    """One authenticated conversation with one store.

    Deliberately a plain object rather than an Odoo model: it holds a token and
    a session, neither of which belongs in the database or in a recordset.
    """

    def __init__(self, domain, token, api_version=None):
        self.domain = (domain or '').strip()
        self.token = (token or '').strip()
        self.api_version = (api_version or DEFAULT_API_VERSION).strip()
        if not self.domain or not self.token:
            raise ShopifyError(_(
                "This store has no domain or no access token yet. Both are on "
                "the Shopify tab of the store record."))
        self._session = requests.Session()
        self._available_cost = None

    # ------------------------------------------------------------------
    @property
    def endpoint(self):
        return 'https://%s/admin/api/%s/graphql.json' % (self.domain, self.api_version)

    def _headers(self):
        return {
            'X-Shopify-Access-Token': self.token,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    def _respect_cost(self):
        """Wait rather than be rejected.

        Shopify's bucket refills at a known rate, so when the last response said
        the bucket is nearly empty the right move is to pause for the refill.
        Firing anyway earns a 429, and a 429 costs the same wait plus a wasted
        round trip.
        """
        if self._available_cost is not None and self._available_cost < COST_FLOOR:
            time.sleep(1.0)
            self._available_cost = None

    def _note_cost(self, body):
        cost = ((body or {}).get('extensions') or {}).get('cost') or {}
        status = cost.get('throttleStatus') or {}
        if 'currentlyAvailable' in status:
            self._available_cost = status['currentlyAvailable']

    # ------------------------------------------------------------------
    def call(self, query, variables=None):
        """Run one GraphQL document and return its `data`.

        Raises ShopifyError with something readable on every failure path.
        """
        payload = json.dumps({'query': query, 'variables': variables or {}})
        last_error = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._respect_cost()
            try:
                response = self._session.post(
                    self.endpoint, data=payload, headers=self._headers(),
                    timeout=TIMEOUT)
            except requests.exceptions.Timeout:
                last_error = _("Shopify did not respond within %s seconds.") % TIMEOUT
            except requests.exceptions.RequestException as err:
                last_error = _("Could not reach Shopify: %s") % _redact(err)
            else:
                fatal = self._check_status(response)
                if fatal:
                    raise ShopifyError(fatal)
                if response.status_code in RETRY_STATUS:
                    last_error = _("Shopify returned HTTP %s.") % response.status_code
                    self._sleep_for(response, attempt)
                    continue
                try:
                    body = response.json()
                except ValueError:
                    raise ShopifyError(_(
                        "Shopify returned something that is not JSON (HTTP %s). "
                        "Check that the store domain is the myshopify.com one and "
                        "not a custom domain behind a redirect.") % response.status_code)

                self._note_cost(body)

                # GraphQL reports failures with HTTP 200. Not looking here is how
                # a sync silently does nothing and reports success.
                errors = body.get('errors')
                if errors:
                    if self._is_throttled(errors):
                        last_error = _("Shopify throttled the request.")
                        self._available_cost = 0
                        time.sleep(min(2 ** attempt, 16))
                        continue
                    raise ShopifyError(self._explain(errors))

                if body.get('data') is None:
                    raise ShopifyError(_("Shopify returned an empty response."))
                return body['data']

            time.sleep(min(2 ** attempt, 16))

        raise ShopifyError(_(
            "Shopify could not be reached after %(n)s attempts. Last problem: "
            "%(err)s") % {'n': MAX_ATTEMPTS, 'err': last_error})

    def _check_status(self, response):
        """Return a message for statuses that retrying cannot fix."""
        if response.status_code in (401, 403):
            return _(
                "Shopify rejected the access token for %s. Reinstall the custom "
                "app in the Shopify admin and paste the new Admin API access "
                "token, then test the connection again.") % self.domain
        if response.status_code == 404:
            return _(
                "Shopify has no store at %(domain)s on API version %(ver)s. The "
                "domain must be the myshopify.com one, such as "
                "example.myshopify.com.") % {'domain': self.domain, 'ver': self.api_version}
        if response.status_code == 402:
            return _("The Shopify store %s is frozen or unpaid.") % self.domain
        if response.status_code == 423:
            return _("The Shopify store %s is locked.") % self.domain
        return None

    @staticmethod
    def _is_throttled(errors):
        for err in errors or []:
            code = ((err.get('extensions') or {}).get('code') or '').upper()
            if code == 'THROTTLED' or 'throttl' in (err.get('message') or '').lower():
                return True
        return False

    @staticmethod
    def _explain(errors):
        parts = []
        for err in errors or []:
            message = _redact(err.get('message') or '')
            code = (err.get('extensions') or {}).get('code')
            if code == 'ACCESS_DENIED':
                field = (err.get('extensions') or {}).get('requiredAccess') or ''
                message = _(
                    "The access token is missing a permission%s. Add it to the "
                    "custom app's Admin API scopes in Shopify, then reinstall the "
                    "app to issue a new token.") % (' (%s)' % field if field else '')
            parts.append(message)
        return _("Shopify refused the request. %s") % ' '.join(parts)

    @staticmethod
    def _sleep_for(response, attempt):
        retry_after = response.headers.get('Retry-After')
        try:
            time.sleep(min(float(retry_after), 30.0))
        except (TypeError, ValueError):
            time.sleep(min(2 ** attempt, 16))

    # ------------------------------------------------------------------
    def paginate(self, query, variables, path, page_size=50, max_pages=1000):
        """Walk a cursor-paginated connection and yield every node.

        `path` is the key of the connection in the response, such as 'orders'.

        Cursors, not offsets: Shopify's connections have no page numbers, and a
        sync that re-requests "page 2" after records changed silently skips rows.
        max_pages is a runaway guard, not a limit anyone should hit; reaching it
        is logged rather than passed over, because a truncated sync that looks
        complete is worse than one that admits it stopped.
        """
        cursor = None
        pages = 0
        while pages < max_pages:
            page_vars = dict(variables or {}, first=page_size, after=cursor)
            data = self.call(query, page_vars)
            connection = data.get(path) or {}
            for edge in connection.get('edges') or []:
                node = edge.get('node')
                if node:
                    yield node
            page_info = connection.get('pageInfo') or {}
            if not page_info.get('hasNextPage'):
                return
            cursor = page_info.get('endCursor')
            if not cursor:
                return
            pages += 1
        _logger.warning(
            "miko_shopify: stopped paginating %s after %s pages; the sync is "
            "incomplete", path, max_pages)

    # ------------------------------------------------------------------
    def shop(self):
        """Identify the store. The cheapest possible proof the token works."""
        data = self.call("""
            query { shop {
                name
                myshopifyDomain
                email
                currencyCode
                ianaTimezone
                billingAddress { countryCodeV2 }
            } }
        """)
        return data.get('shop') or {}
