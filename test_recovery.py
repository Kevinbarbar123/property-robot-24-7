import io
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import browser_recovery
import challenge_detection
import property_bot
import recovery
import recovery_portal
import scan_progress
import source_client
import telegram_owner_alert as alerts
import telegram_command_bot as commands
from reliability_config import load_reliability
from source_errors import SourceAccessError, SourceResponseError


def empty_search():
    return '<script type="application/json" id="__NEXT_DATA__">' + json.dumps({"props": {"pageProps": {"initialState": {"search": {"ads": {"hits": [], "totalHits": 0}}}}}}) + '</script>'


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"OLX_RECOVERY_DB": str(self.folder / "recovery.sqlite3"), "RECOVERY_PUBLIC_URL": "https://bot.example"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.progress = scan_progress.ProgressStore()
        self.config = replace(load_reliability(), request_interval_min=0.001, request_interval_max=0.001)
        self.configuration = patch.object(source_client, "load_reliability", return_value=self.config)
        self.configuration.start()
        self.addCleanup(self.configuration.stop)
        self.addCleanup(self.clear_context)

    def clear_context(self):
        scan_progress.CURRENT_JOB = ""
        scan_progress.CURRENT_STORE = None

    def response(self, content="ok", status=200, headers=None):
        response = MagicMock()
        response.status = status
        response.headers = headers or {}
        response.read.return_value = content.encode()
        response.__enter__.return_value = response
        return response

    def incident(self, kind="captcha", url="https://olx.com.lb/properties/apartments-villas-for-sale/fanar/", retry_at=0):
        return self.progress.incident(SourceAccessError("simulated", kind=kind, url=url, retry_at=retry_at))

    def test_provider_detection_and_false_positive_protection(self):
        for provider, widget in [("recaptcha", '<div class="g-recaptcha">'), ("hcaptcha", '<iframe src="https://hcaptcha.com/challenge">'), ("turnstile", '<div class="cf-turnstile">')]:
            with self.subTest(provider=provider):
                result = challenge_detection.detect_challenge(200, {}, widget + "Verify you are human")
                self.assertEqual(result.kind, "captcha")
                self.assertEqual(result.provider, provider)
                self.assertIsNone(challenge_detection.detect_challenge(200, {}, empty_search() + widget))
        self.assertIsNone(challenge_detection.detect_challenge(200, {}, '<script src="https://google.com/recaptcha/api.js"></script><h1>Apartment</h1>'))
        self.assertIsNone(challenge_detection.detect_challenge(200, {}, '<script>const example="<div class=\'h-captcha\'>";</script><h1>Apartment</h1>'))

    def test_configured_challenge_selector(self):
        config = replace(self.config, challenge_selectors=["#custom-check"])
        self.assertEqual(challenge_detection.detect_challenge(200, {}, '<div id="custom-check">Security check</div>', config).kind, "captcha")

    def test_restrictions_are_not_assumed_to_be_captcha(self):
        self.assertEqual(challenge_detection.detect_challenge(403, {}, "Forbidden").kind, "access_restricted")
        self.assertEqual(challenge_detection.detect_challenge(429, {}, "Too many requests").kind, "rate_limited")
        self.assertIsNone(challenge_detection.detect_challenge(500, {}, "Server error"))

    def test_retry_after_seconds_and_http_date(self):
        self.assertEqual(challenge_detection.retry_deadline("120", 1000), 1120)
        date = format_datetime(datetime.fromtimestamp(5000, timezone.utc), usegmt=True)
        self.assertEqual(challenge_detection.retry_deadline(date, 1000), 5000)
        self.assertEqual(challenge_detection.retry_deadline("invalid", 1000), 0)

    def test_429_deadline_is_persisted_and_no_second_request_is_sent(self):
        future = format_datetime(datetime.fromtimestamp(time.time() + 7200, timezone.utc), usegmt=True)
        error = HTTPError("https://olx.com.lb/test", 429, "limited", {"Retry-After": future}, io.BytesIO(b"limited"))
        with patch.object(property_bot, "urlopen", side_effect=error) as request:
            with self.assertRaises(SourceAccessError):
                property_bot.fetch_url("https://olx.com.lb/test")
            with self.assertRaises(SourceAccessError):
                property_bot.fetch_url("https://olx.com.lb/test")
            self.assertEqual(request.call_count, 1)
        self.assertGreater(self.progress.current_incident()["retry_at"], time.time() + 7100)

    def test_html_captcha_pauses_the_shared_session(self):
        with patch.object(property_bot, "urlopen", return_value=self.response('<div class="h-captcha">Verify you are human')) as request:
            for url in ("https://olx.com.lb/one", "https://olx.com.lb/two"):
                with self.assertRaises(SourceAccessError):
                    property_bot.fetch_url(url)
            self.assertEqual(request.call_count, 1)
        self.assertEqual(self.progress.current_incident()["provider"], "hcaptcha")

    def test_temporary_failure_retries_with_exponential_jitter(self):
        with patch.object(property_bot, "urlopen", side_effect=[URLError("offline"), URLError("offline"), self.response()]) as request, patch.object(source_client.random, "uniform", side_effect=lambda low, high: high), patch.object(source_client.time, "sleep") as sleep:
            self.assertEqual(property_bot.fetch_url("https://olx.com.lb/test"), "ok")
        self.assertEqual(request.call_count, 3)
        self.assertIn(2, [call.args[0] for call in sleep.call_args_list])
        self.assertIn(4, [call.args[0] for call in sleep.call_args_list])

    def test_exhausted_temporary_failures_are_distinct(self):
        with patch.object(property_bot, "urlopen", side_effect=URLError("offline")), patch.object(source_client.time, "sleep"):
            with self.assertRaises(SourceResponseError) as caught:
                property_bot.fetch_url("https://olx.com.lb/test")
        self.assertNotIsInstance(caught.exception, SourceAccessError)
        self.assertEqual(caught.exception.kind, "temporary")

    def test_expired_rate_limit_resumes_after_success(self):
        incident = self.incident(kind="rate_limited", retry_at=time.time() - 1)
        with patch.object(property_bot, "urlopen", return_value=self.response()):
            property_bot.fetch_url("https://olx.com.lb/test")
        self.assertFalse(self.progress.current_incident())
        self.assertEqual(self.progress.get("resumed_incident")["id"], incident["id"])

    def test_concurrent_request_limit_releases_slots(self):
        lease = self.progress.gate("olx.com.lb", self.config)
        self.progress.release(lease)
        with self.progress.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM leases").fetchone()[0], 0)

    def test_recovery_worker_lock_prevents_duplicate_resume_workers(self):
        with scan_progress.recovery_worker_lock() as acquired:
            self.assertTrue(acquired)
            with scan_progress.recovery_worker_lock() as duplicate:
                self.assertFalse(duplicate)
        with scan_progress.recovery_worker_lock() as released:
            self.assertTrue(released)

    def test_checkpoint_survives_process_restart(self):
        job = self.progress.begin({"max_pages": 3})
        self.progress.save_work(job, "page-one", {"listings": ["one", "two"]})
        restored = scan_progress.ProgressStore()
        self.assertEqual(restored.begin({"max_pages": 3}), job)
        self.assertEqual(restored.work(job, "page-one"), {"listings": ["one", "two"]})

    def test_search_resumption_does_not_refetch_completed_page(self):
        scan_progress.CURRENT_JOB = self.progress.begin({"test": True})
        scan_progress.CURRENT_STORE = self.progress
        first = alerts.SellerDetails(property_bot.Listing("Fanar", "Apartment", 100000, 100, "Fanar", "today", "https://olx.com.lb/ad/one.html"))
        second = alerts.SellerDetails(property_bot.Listing("Fanar", "Apartment", 100000, 100, "Fanar", "today", "https://olx.com.lb/ad/two.html"))
        with patch.object(alerts, "OLX_PURPOSES", {"sale": "apartments-villas-for-sale"}), patch.object(alerts, "OLX_TARGET_SEARCHES", {"Fanar": {"query": "fanar", "location": ""}}), patch.object(alerts, "parse_page_count", return_value=2), patch.object(alerts, "search_page_details", return_value=[first]), patch.object(alerts.time, "sleep"), patch.object(alerts, "fetch_url", side_effect=["first", SourceAccessError("simulated challenge")]):
            with self.assertRaises(SourceAccessError):
                alerts.collect_fanar_radius_seed_details(2, 0)
        with patch.object(alerts, "OLX_PURPOSES", {"sale": "apartments-villas-for-sale"}), patch.object(alerts, "OLX_TARGET_SEARCHES", {"Fanar": {"query": "fanar", "location": ""}}), patch.object(alerts, "search_page_details", return_value=[second]), patch.object(alerts.time, "sleep"), patch.object(alerts, "fetch_url", return_value="second") as request:
            rows = alerts.collect_fanar_radius_seed_details(2, 0)
        self.assertEqual(len(rows), 2)
        request.assert_called_once()
        self.assertIn("page=2", request.call_args.args[0])

    def test_links_are_single_use_and_expire(self):
        incident = self.incident()
        token = self.progress.link(incident["id"])
        session, identifier = self.progress.redeem(token, 3600)
        self.assertEqual(identifier, incident["id"])
        self.assertEqual(self.progress.authorize(session, 900), identifier)
        with self.assertRaises(PermissionError):
            self.progress.redeem(token, 3600)
        self.progress.resolve(identifier)
        with self.assertRaises(PermissionError):
            self.progress.authorize(session, 900)

    def test_idle_and_absolute_session_expiry(self):
        incident = self.incident()
        token = self.progress.link(incident["id"])
        session, _ = self.progress.redeem(token, 3600)
        with self.progress.connect() as connection:
            connection.execute("UPDATE portal_sessions SET last_active=?", (time.time() - 901,))
        with self.assertRaises(PermissionError):
            self.progress.authorize(session, 900)

    def test_frame_polling_does_not_extend_idle_timeout(self):
        incident = self.incident()
        token = self.progress.link(incident["id"])
        session, _ = self.progress.redeem(token, 3600)
        before = time.time() - 899
        with self.progress.connect() as connection:
            connection.execute("UPDATE portal_sessions SET last_active=?", (before,))
        self.progress.authorize(session, 900, touch=False)
        with self.progress.connect() as connection:
            after = connection.execute("SELECT last_active FROM portal_sessions").fetchone()[0]
        self.assertEqual(before, after)

    def test_three_failed_recovery_cycles_stop_automatic_retries(self):
        from types import SimpleNamespace
        job = self.progress.begin({"max_pages": 3})
        scan_progress.CURRENT_JOB = job
        scan_progress.CURRENT_STORE = self.progress
        args = SimpleNamespace(dry_run=True, telegram_token="", telegram_chat_id="")
        for _ in range(3):
            recovery.handle_failure(args, SourceResponseError("connection error", kind="temporary"), {})
        self.assertEqual(self.progress.job(job)["status"], "stopped")
        self.assertEqual(self.progress.get("stopped_incident"), self.progress.current_incident()["id"])

    def test_portal_rejects_cross_origin_actions_and_unclaimed_frames(self):
        handler = MagicMock()
        handler.headers = {"Content-Length": "2", "Origin": "https://evil.example"}
        handler.rfile = io.BytesIO(b"{}")
        with patch.object(recovery_portal, "send") as send:
            self.assertTrue(recovery_portal.dispatch(handler, "/recovery/claim", "POST"))
            self.assertEqual(send.call_args.args[1], 401)
            recovery_portal.dispatch(handler, "/recovery/frame", "GET")
            self.assertEqual(send.call_args.args[1], 401)

    def test_internal_browser_endpoint_requires_secret(self):
        handler = MagicMock()
        handler.headers = {"Content-Length": "2"}
        handler.rfile = io.BytesIO(b"{}")
        with patch.object(recovery_portal, "send") as send, patch.object(recovery_portal, "service") as browser:
            recovery_portal.dispatch(handler, "/recovery/internal", "POST")
            browser.assert_not_called()
            self.assertEqual(send.call_args.args[1], 401)

    def test_normal_pages_never_launch_browser(self):
        with patch.object(property_bot, "urlopen", return_value=self.response(empty_search())), patch.object(browser_recovery, "service") as browser:
            content = property_bot.fetch_url("https://olx.com.lb/test")
        self.assertEqual(alerts.search_page_details(content, "Fanar", "sale"), [])
        browser.assert_not_called()

    def test_delivery_receipts_prevent_repeat_chunks(self):
        scan_progress.CURRENT_STORE = self.progress
        with patch.object(alerts, "telegram_api", return_value={"result": {"message_id": 123}}) as send, patch.object(alerts.time, "sleep"):
            alerts.send_telegram_message("test", "123,456", "lead", delivery_key="lead:one")
            alerts.send_telegram_message("test", "123,456", "lead", delivery_key="lead:one")
        self.assertEqual(send.call_count, 2)

    def test_unknown_send_outcome_is_not_automatically_resent(self):
        scan_progress.CURRENT_STORE = self.progress
        with patch.object(alerts, "telegram_api", side_effect=TimeoutError("lost response")) as send:
            with self.assertRaises(TimeoutError):
                alerts.send_telegram_message("test", "123", "lead", delivery_key="lead:one")
            alerts.send_telegram_message("test", "123", "lead", delivery_key="lead:one")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.progress.delivery_status("lead:one:123:0"), "unknown")

    def test_send_does_not_try_other_hosts_after_an_unknown_outcome(self):
        with patch.object(alerts, "telegram_api_request", side_effect=TimeoutError("lost reply")) as request:
            with self.assertRaises(TimeoutError):
                alerts.telegram_api("test", "sendMessage", {"chat_id": "123", "text": "lead"}, retries=0)
        self.assertEqual(request.call_count, 1)

    def test_incident_notice_is_sent_once_and_contains_link(self):
        incident = self.incident()
        with patch.object(alerts, "send_telegram_message") as send:
            recovery.notify_event("test", "123", incident, "captcha", "yesterday")
            recovery.notify_event("test", "123", incident, "captcha", "yesterday")
        send.assert_called_once()
        self.assertIn("https://bot.example/verify#", send.call_args.args[2])
        self.assertIn("Last successful scan: yesterday", send.call_args.args[2])

    def test_browser_close_cleans_owned_context(self):
        browser = object.__new__(browser_recovery.BrowserService)
        browser.context = MagicMock()
        browser.page = MagicMock()
        browser.playwright = MagicMock()
        context, playwright = browser.context, browser.playwright
        browser._close()
        context.close.assert_called_once()
        playwright.stop.assert_called_once()
        self.assertIsNone(browser.context)

    def test_access_restriction_with_listing_markup_is_not_verified(self):
        incident = self.incident()
        browser = object.__new__(browser_recovery.BrowserService)
        browser.page = MagicMock()
        browser.page.url = incident["url"]
        browser.page.content.return_value = empty_search()
        browser.last_status = 403
        browser.verified = False
        browser._check_verified(incident)
        self.assertFalse(browser.verified)

    def test_browser_verification_requires_valid_listing_data(self):
        incident = self.incident()
        browser = object.__new__(browser_recovery.BrowserService)
        browser.page = MagicMock()
        browser.page.url = incident["url"]
        browser.page.locator.return_value.count.return_value = 0
        browser.verified = False
        browser.page.content.return_value = "<h1>Thanks</h1>"
        browser._check_verified(incident)
        self.assertFalse(browser.verified)
        browser.page.content.return_value = empty_search()
        browser._check_verified(incident)
        self.assertTrue(browser.verified)
        self.assertTrue(self.progress.get("browser_mode"))
        self.assertTrue(self.progress.get("resume_requested"))
        self.assertFalse(self.progress.current_incident())

    def test_resume_command_does_not_override_captcha(self):
        job = self.progress.begin({"max_pages": 3})
        self.progress.status(job, "paused")
        self.incident()
        with self.assertRaises(SourceResponseError):
            recovery.resume_arguments()

    def test_unauthorized_chat_cannot_access_verification(self):
        with patch.object(commands, "safe_send_message"), patch.object(commands, "log_event"), patch.object(recovery, "verification_link") as link:
            commands.handle_message({"chat": {"id": 999}, "text": "/verify"}, "test", "123")
        link.assert_not_called()


if __name__ == "__main__":
    unittest.main()
