import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import telegram_owner_alert as alerts
from property_bot import Listing


class OwnerFilterTests(unittest.TestCase):
    def details(self, **values):
        listing = Listing("Fanar", "Apartment for rent", 600, 120, "Fanar, Metn", "today", "https://olx.com.lb/ad/test-ID1.html")
        defaults = {"distance_km": 2, "seller_name": "Charbel", "seller_type": "private"}
        defaults.update(values)
        return alerts.SellerDetails(listing, **defaults)

    def report(self, folder, accepted=(), rejected=(), days=0, dry_run=False):
        stamp = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d_%H%M%S")
        path = folder / f"owner_alert_decisions_{stamp}.json"
        path.write_text(json.dumps({"summary": {"dry_run": dry_run}, "accepted": list(accepted), "rejected": list(rejected)}), encoding="utf-8")
        return path

    def test_total_ads_are_only_a_soft_signal(self):
        for count in (5, 6, 9, 16, 43):
            with self.subTest(count=count):
                details = self.details(account_ads_count=count)
                self.assertTrue(alerts.likely_owner(details, 1, 4)[0])
                self.assertFalse(alerts.is_agency_like(details, 1))
                self.assertGreater(details.owner_score, 0)

    def test_agency_evidence_cannot_be_offset_by_owner_wording(self):
        for values in ({"agency_id": "123"}, {"agency_name": "Example Realty"}, {"seller_type": "2"}, {"seller_name": "Example Real Estate"}):
            with self.subTest(values=values):
                details = self.details(description="direct owner no commission", **values)
                self.assertFalse(alerts.likely_owner(details, 1, 4)[0])

    def test_multiple_property_posts_still_reject(self):
        details = self.details(description="direct owner", account_ads_count=6)
        self.assertFalse(alerts.likely_owner(details, 4, 4)[0])
        self.assertFalse(alerts.likely_owner(details, 1, 4, phone_post_count=5)[0])

    def test_missing_location_and_non_apartments_still_reject(self):
        self.assertFalse(alerts.likely_owner(self.details(distance_km=None, account_ads_count=9), 1, 4)[0])
        self.assertFalse(alerts.likely_owner(self.details(distance_km=100), 1, 4)[0])

    def test_learning_never_blacklists_a_personal_name(self):
        broker_id = "12345678-1234-1234-1234-123456789abc"
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.report(folder, rejected=[{"seller_name": "Charbel", "seller_id": broker_id, "agency_name": "Example Agency", "phone_number": "+96171234567"}])
            with patch.object(alerts, "LOG_DIR", folder):
                terms = alerts.learned_broker_terms()
            self.assertNotIn("Charbel", terms)
            self.assertIn(broker_id, terms)
            self.assertIn("+96171234567", terms)
            self.assertEqual(alerts.blacklist_reasons(self.details(), terms), ())
            self.assertTrue(alerts.blacklist_reasons(self.details(seller_id=broker_id), terms))

    def test_total_ads_do_not_create_learned_broker_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.report(folder, rejected=[{"seller_name": "Owner", "account_ads_count": 43, "seller_id": "12345678-1234-1234-1234-123456789abc", "exclusion_reasons": ["account has 43 OLX ads"]}])
            with patch.object(alerts, "LOG_DIR", folder):
                self.assertEqual(alerts.learned_broker_terms(), [])

    def test_explicit_user_blacklist_is_preserved(self):
        self.assertTrue(alerts.blacklist_reasons(self.details(), ["Charbel"]))

    def test_recheck_preserves_accepted_and_reviewed_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.report(folder, accepted=[{"url": "accepted"}], days=20)
            self.report(folder, rejected=[{"url": "accepted"}, {"url": "reviewed"}, {"url": "missed"}, {"url": "broker", "agency_id": "123"}, {"url": "business", "seller_type": "2"}])
            self.report(folder, rejected=[{"url": "too-old"}], days=11)
            state = {"seen_urls": ["missed"], "owner_filter_rechecks": {alerts.OWNER_FILTER_REVISION: ["reviewed"]}}
            with patch.object(alerts, "LOG_DIR", folder):
                self.assertEqual(alerts.owner_filter_recheck_urls(state), {"missed"})
            self.assertEqual(state["seen_urls"], ["missed"])

    def test_dry_run_reports_do_not_hide_future_leads(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.report(folder, accepted=[{"url": "missed"}], dry_run=True)
            self.report(folder, rejected=[{"url": "missed"}], days=1)
            with patch.object(alerts, "LOG_DIR", folder):
                self.assertEqual(alerts.owner_filter_recheck_urls({}), {"missed"})
            for path in folder.glob("*.json"):
                path.unlink()
            self.report(folder, accepted=[{"url": "new"}], rejected=[{"url": "other"}], dry_run=True)
            with patch.object(alerts, "LOG_DIR", folder):
                self.assertEqual(alerts.report_seen_urls(), set())

    def test_full_scan_rechecks_once_without_resending_accepted(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            folder = Path(temporary)
            details = self.details(account_ads_count=9, phone_number="+96171234567")
            state_path = folder / "state.json"
            state_path.write_text(json.dumps({"seen_urls": [details.listing.url, "old-accepted"]}))
            self.report(folder, accepted=[{"url": "old-accepted"}], rejected=[alerts.decision_row(details, 1)], days=1)
            for name, value in {"LOG_DIR": folder, "SCAN_HEALTH_PATH": folder / "health.json", "OUTBOX_PATH": folder / "outbox.json"}.items():
                stack.enter_context(patch.object(alerts, name, value))
            for name in ("load_env_files", "install_run_lock", "flush_telegram_outbox", "send_telegram_message"):
                stack.enter_context(patch.object(alerts, name))
            stack.enter_context(patch.object(alerts, "collect_fanar_radius_seed_details", return_value=[details]))
            enrich = stack.enter_context(patch.object(alerts, "enrich_listings", return_value=[details]))
            args = alerts.build_arg_parser().parse_args(["--state", str(state_path), "--telegram-token", "test-only", "--telegram-chat-id", "123"])
            self.assertEqual(alerts.run(args), 0)
            report = json.loads(sorted(folder.glob("owner_alert_decisions_*.json"))[-1].read_text())
            self.assertEqual(report["summary"]["accepted"], 1)
            self.assertEqual(report["summary"]["rechecked_listings"], 1)
            self.assertEqual(report["summary"]["new_listings"], 0)
            state = json.loads(state_path.read_text())
            self.assertIn("old-accepted", state["seen_urls"])
            self.assertIn(details.listing.url, state["owner_filter_rechecks"][alerts.OWNER_FILTER_REVISION])
            self.assertEqual(alerts.run(args), 0)
            enrich.assert_called_once()
            report = json.loads(sorted(folder.glob("owner_alert_decisions_*.json"))[-1].read_text())
            self.assertEqual(report["summary"]["accepted"], 0)
            self.assertEqual(report["summary"]["new_listings"], 0)


if __name__ == "__main__":
    unittest.main()
