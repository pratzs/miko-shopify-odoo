# -*- coding: utf-8 -*-
"""The scheduled sync.

A connector nobody has to remember to press. Everything else in this module is
useless without it: an import that only runs when somebody clicks a button is a
migration tool, not an integration.

Three rules shape what is here.

**Installing must change nothing.** The cron ships enabled, but it only touches
channels whose owner turned `auto_sync` on, and that is off by default. A module
that starts pulling a live store into a live database the moment it is installed
is a module that makes a mess before anyone has agreed to it.

**One store's failure must not stop the others.** Each channel is synced in its
own try/except and committed on its own, so a store with an expired token cannot
prevent every other store from syncing.

**A slow store must not be started twice.** Odoo's scheduler will happily fire
again while the previous run is still going. A channel that is already syncing is
skipped, and the flag is cleared even when the run fails.
"""
import logging

from odoo import _, api, fields, models
from odoo.tools import config

_logger = logging.getLogger(__name__)


class MikoEcommerceChannel(models.Model):
    _inherit = 'miko.ecommerce.channel'

    auto_sync = fields.Boolean(
        string='Sync on a schedule', default=False,
        help="Off until you turn it on, deliberately. Installing a connector "
             "should never start moving data on its own.")
    sync_interval_minutes = fields.Integer(
        string='Every (minutes)', default=60,
        help="How long to leave between runs for this store. The scheduler wakes "
             "more often than this and skips stores that are not due yet.")
    sync_orders = fields.Boolean(string='Sync orders', default=True)
    sync_products = fields.Boolean(string='Sync products', default=False)
    sync_customers = fields.Boolean(string='Sync customers', default=False)
    sync_running = fields.Boolean(
        readonly=True, copy=False,
        help="Set while a scheduled run is in progress, so a long sync is never "
             "started a second time on top of itself.")
    last_sync_message = fields.Text(readonly=True, copy=False)

    # ------------------------------------------------------------------
    @api.model
    def _cron_shopify_sync(self):
        """Entry point for the scheduler. Never raises."""
        channels = self.search([
            ('platform', '=', 'shopify'),
            ('active', '=', True),
            ('auto_sync', '=', True),
        ])
        for channel in channels:
            if not channel._sync_is_due():
                continue
            channel._run_scheduled_sync()
        return True

    def _sync_is_due(self):
        """True when enough time has passed, and nothing is already running."""
        self.ensure_one()
        if self.sync_running:
            _logger.info("miko_shopify: %s is still syncing, skipping this tick",
                         self.name)
            return False
        if not self.last_sync:
            return True
        minutes = max(self.sync_interval_minutes or 0, 5)
        elapsed = (fields.Datetime.now() - self.last_sync).total_seconds() / 60.0
        return elapsed >= minutes

    def _checkpoint(self, rollback=False):
        """Commit progress as the run goes, except under test.

        A scheduled sync has to commit as it goes. Without it, a failure late in
        the run rolls back the orders that already imported successfully, and the
        next run fetches every one of them again.

        Odoo refuses commit and rollback inside a test, because they would break
        the test's own transaction, so this does nothing there. The behaviour
        being skipped is transaction bookkeeping, not logic, and everything the
        tests actually assert still runs.
        """
        # config['test_enable'], NOT Registry.in_test_mode(). The registry method
        # answers "is there a test cursor on the registry", which is False for an
        # ordinary at-install TransactionCase, so trusting it meant this really
        # did commit during the suite on 16 to 18 - which aborts the transaction
        # and takes every later test in the class down with it. The config flag
        # answers the question actually being asked, and exists on every series
        # (19 removed the registry method altogether).
        if config['test_enable']:
            return False
        if rollback:
            self.env.cr.rollback()
        else:
            self.env.cr.commit()
        return True

    def _run_scheduled_sync(self):
        """Sync one channel, commit it, and never let it break the loop.

        The commit is deliberate. Without it a failure late in the run would roll
        back the orders that had already imported successfully, and the next run
        would fetch them all again.
        """
        self.ensure_one()
        self.sync_running = True
        self._checkpoint()            # claim the slot before the slow part

        done = []
        try:
            if self.sync_products:
                done.append(_("%s products") % self._import_shopify_products())
            if self.sync_customers:
                done.append(_("%s customers") % self._import_shopify_customers())
            if self.sync_orders:
                done.append(_("%s orders") % self._import_shopify_orders())
            message = _("Synced %s.") % (", ".join(done) or _("nothing enabled"))
        except Exception as err:      # noqa: BLE001 - recorded, never raised on
            _logger.exception("miko_shopify: scheduled sync failed for %s", self.name)
            self._checkpoint(rollback=True)
            message = _("Failed: %s") % err
        finally:
            # Always clears, including after a rollback, or the channel would be
            # stuck as "running" for ever and never sync again.
            self.sync_running = False
            self.last_sync_message = message
            self._checkpoint()
        return message

    def action_sync_now(self):
        """Run the scheduled sync immediately, for the button on the form."""
        for channel in self:
            channel._run_scheduled_sync()
        return True
