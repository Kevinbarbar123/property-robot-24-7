import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import owner_scheduler
import property_bot
import telegram_command_bot
import telegram_owner_alert as alerts


class ScanReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.recovery_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.recovery_temporary.cleanup)
        self.recovery_environment = patch.dict(os.environ, {"OLX_RECOVERY_DB": str(Path(self.recovery_temporary.name) / "recovery.sqlite3")})
        self.recovery_environment.start()
        self.addCleanup(self.recovery_environment.stop)

    def response(self, status=200, content=b"", headers=None):
        response = MagicMock()
        response.status = status
        response.headers = headers or {}
        response.read.return_value = content
        response.__enter__.return_value = response
        return response

    def test_challenge_stops_without_retry(self):
        response = self.response(202, headers={"x-amzn-waf-action": "challenge"})
        with patch.object(property_bot, "urlopen", return_value=response) as fetch:
            with self.assertRaises(property_bot.SourceAccessError):
                property_bot.fetch_url("https://www.olx.com.lb/test", retries=5)
        self.assertEqual(fetch.call_count, 1)

    def test_rate_limit_stops_without_retry(self):
        with patch.object(property_bot, "urlopen", side_effect=HTTPError("test", 429, "limited", {}, None)) as fetch:
            with self.assertRaises(property_bot.SourceAccessError):
                property_bot.fetch_url("https://www.olx.com.lb/test", retries=5)
        self.assertEqual(fetch.call_count, 1)

    def test_empty_response_is_failure(self):
        with patch.object(property_bot, "urlopen", return_value=self.response()):
            with self.assertRaises(property_bot.SourceResponseError):
                property_bot.fetch_url("https://www.olx.com.lb/test")

    def test_unrecognized_html_is_not_no_listings(self):
        with self.assertRaises(property_bot.SourceResponseError):
            alerts.search_page_details("<html>changed source</html>", "Fanar", "sale")

    def test_explicit_empty_search_is_valid(self):
        state = {"props": {"pageProps": {"initialState": {"search": {"ads": {"hits": [], "totalHits": 0}}}}}}
        raw = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(state) + '</script>'
        self.assertEqual(alerts.search_page_details(raw, "Fanar", "sale"), [])

    def test_failure_preserves_seen_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            state_path = folder / "seen.json"
            state_path.write_text('{"seen_urls": ["existing"]}', encoding="utf-8")
            before = state_path.read_bytes()
            args = alerts.build_arg_parser().parse_args(["--dry-run", "--state", str(state_path)])
            with patch.object(alerts, "SCAN_HEALTH_PATH", folder / "health.json"), patch.object(alerts, "LOG_DIR", folder), patch.object(alerts, "_run", side_effect=property_bot.SourceAccessError("access check")), patch.object(alerts, "send_telegram_message") as send:
                self.assertEqual(alerts.run(args), 3)
            self.assertEqual(state_path.read_bytes(), before)
            report = json.loads(next(folder.glob("owner_alert_decisions_*.json")).read_text())
            self.assertEqual(report["summary"]["status"], "blocked")
            self.assertFalse(report["summary"]["coverage_complete"])
            send.assert_not_called()

    def test_cooldown_does_not_fetch(self):
        with tempfile.TemporaryDirectory() as temporary:
            health = Path(temporary) / "health.json"
            health.write_text(json.dumps({"retry_after": time.time() + 3600}))
            with patch.object(alerts, "SCAN_HEALTH_PATH", health), patch.object(alerts, "install_run_lock"), patch.object(alerts, "load_env_files"), patch.object(alerts, "collect_fanar_radius_seed_details") as collect:
                self.assertEqual(alerts.run(alerts.build_arg_parser().parse_args(["--dry-run"])), 3)
            collect.assert_not_called()

    def test_scheduler_runs_without_ai_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(owner_scheduler, "ROOT", root), patch.object(owner_scheduler, "SCHEDULE_PATH", root / "data/schedule.json"), patch.object(owner_scheduler, "install_run_lock"), patch.object(owner_scheduler, "load_env_files"), patch.object(owner_scheduler.os, "chdir"), patch.dict(os.environ, {}, clear=True), patch.object(owner_scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as child:
                self.assertEqual(owner_scheduler.run_due(), 0)
                self.assertEqual(owner_scheduler.run_due(), 0)
                child.assert_called_once()
                self.assertIn("telegram_owner_alert.py", child.call_args.args[0][2])

    def test_scheduler_failure_waits_six_hours(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(owner_scheduler, "ROOT", root), patch.object(owner_scheduler, "SCHEDULE_PATH", root / "data/schedule.json"), patch.object(owner_scheduler, "install_run_lock"), patch.object(owner_scheduler, "load_env_files"), patch.object(owner_scheduler.os, "chdir"), patch.object(owner_scheduler.subprocess, "run", side_effect=subprocess.TimeoutExpired("scan", 2700)):
                self.assertEqual(owner_scheduler.run_due(), 1)
                state = owner_scheduler.read_schedule()
                self.assertGreater(state["next_run_at"], time.time() + 21500)
                self.assertNotIn("last_success_at", state)

    def test_ai_quota_failure_keeps_helper_available(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}), patch.object(telegram_command_bot, "bot_context_summary", return_value=""), patch.object(telegram_command_bot, "urlopen", side_effect=HTTPError("test", 429, "quota", {}, None)), patch.object(telegram_command_bot, "local_ai_helper", return_value="local answer"), patch.object(telegram_command_bot, "log_event"), patch.object(telegram_command_bot, "save_health"):
            self.assertIn("local answer", telegram_command_bot.ask_ai_helper("help"))

    def test_conflicting_poller_stops(self):
        with patch.object(telegram_command_bot, "load_json", return_value={}), patch.object(telegram_command_bot, "save_health") as health, patch.object(telegram_command_bot, "log_event"), patch.object(telegram_command_bot, "recover_stale_scan_state"), patch.object(telegram_command_bot, "sync_bot_commands"), patch.object(telegram_command_bot, "get_updates", side_effect=HTTPError("test", 409, "conflict", {}, None)) as updates:
            self.assertEqual(telegram_command_bot.main_loop("test", "test"), 3)
            updates.assert_called_once()
            conflict = [call.kwargs for call in health.call_args_list if call.kwargs.get("status") == "conflict"]
            self.assertEqual(len(conflict), 1)
            self.assertGreater(conflict[0]["retry_after"], time.time() + 890)

    def test_listener_lock_rejects_duplicate_and_releases(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "listener.lock"
            with telegram_command_bot.listener_lock(path):
                with self.assertRaises(RuntimeError):
                    with telegram_command_bot.listener_lock(path):
                        self.fail("Duplicate listener acquired lock")
            with telegram_command_bot.listener_lock(path):
                pass

    def test_existing_webhook_is_reported_without_polling_or_deleting(self):
        with patch.object(telegram_command_bot, "load_env_files"), patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-only"}), patch.object(telegram_command_bot, "listener_lock"), patch.object(telegram_command_bot, "telegram_api", return_value={"result": {"url": "https://example.test/webhook"}}) as api, patch.object(telegram_command_bot, "save_health") as health, patch.object(telegram_command_bot, "log_event"), patch.object(telegram_command_bot, "main_loop") as listener:
            self.assertEqual(telegram_command_bot.main(), 3)
            listener.assert_not_called()
            api.assert_called_once_with("test-only", "getWebhookInfo", {})
            self.assertEqual(health.call_args.kwargs["status"], "conflict")

    def test_manual_search_commands_route_for_both_chats(self):
        for chat_id in (100001, 100002):
            for command in ("/search", "Search now", "/latest@Propertieskimi_bot", "/scan"):
                with self.subTest(chat_id=chat_id, command=command), patch.object(telegram_command_bot, "safe_send_message"), patch.object(telegram_command_bot, "log_event"), patch.object(telegram_command_bot, "start_scan_thread") as start:
                    telegram_command_bot.handle_message({"chat": {"id": chat_id}, "text": command}, "test", "100001,100002")
                    start.assert_called_once_with("100001,100002", "test")

    def test_unauthorized_chat_cannot_scan(self):
        with patch.object(telegram_command_bot, "safe_send_message"), patch.object(telegram_command_bot, "log_event"), patch.object(telegram_command_bot, "start_scan_thread") as start:
            telegram_command_bot.handle_message({"chat": {"id": 999}, "text": "/search"}, "test", "100001,100002")
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
