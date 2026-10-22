import io
import json
import os
import tarfile
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cloud_runtime
import migrate_bot
import owner_scheduler
import recovery
import scan_progress
import telegram_command_bot
import web_robot_app


class CloudRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_local_default_remains_active(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(cloud_runtime.active())

    def test_railway_defaults_to_staging(self):
        with patch.dict(os.environ, {"RAILWAY_ENVIRONMENT_ID": "fixture"}, clear=True):
            self.assertFalse(cloud_runtime.active())

    def test_explicit_staging_stops_commands_and_schedules(self):
        with patch.dict(os.environ, {"BOT_ACTIVE": "false"}), patch.object(owner_scheduler, "ROOT", self.root), patch.object(owner_scheduler.os, "chdir"), patch.object(owner_scheduler, "load_env_files"), patch.object(telegram_command_bot, "load_env_files"), patch.object(telegram_command_bot, "telegram_api") as api, patch.object(owner_scheduler.subprocess, "run") as run:
            self.assertEqual(telegram_command_bot.main(), 0)
            self.assertEqual(owner_scheduler.run_due(), 0)
            self.assertFalse(web_robot_app.start_scan(7))
            api.assert_not_called()
            run.assert_not_called()

    def test_staging_recovery_never_ticks(self):
        stop = unittest.mock.Mock()
        stop.wait.side_effect = [False, True]
        with patch.dict(os.environ, {"BOT_ACTIVE": "false"}), patch.object(recovery, "recovery_tick") as tick:
            recovery.recovery_loop(stop)
            tick.assert_not_called()

    def test_missing_volume_is_refused(self):
        with patch.dict(os.environ, {"REQUIRE_PERSISTENT_DATA": "true", "RAILWAY_VOLUME_MOUNT_PATH": ""}):
            with self.assertRaisesRegex(RuntimeError, "volume"):
                cloud_runtime.prepare_runtime(self.root)

    def test_directory_without_real_mount_is_refused(self):
        with patch.dict(os.environ, {"REQUIRE_PERSISTENT_DATA": "true", "RAILWAY_VOLUME_MOUNT_PATH": str(self.root / "data")}), patch.object(os.path, "ismount", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "volume"):
                cloud_runtime.prepare_runtime(self.root)

    def test_missing_migration_prevents_activation(self):
        with patch.dict(os.environ, {"BOT_ACTIVE": "true", "REQUIRE_MIGRATED_STATE": "true", "REQUIRE_PERSISTENT_DATA": "false"}):
            with self.assertRaisesRegex(RuntimeError, "history"):
                cloud_runtime.prepare_runtime(self.root)

    @unittest.skipIf(os.name == "nt", "Container symlinks are checked on Linux CI")
    def test_reports_and_logs_persist_across_startups(self):
        with patch.dict(os.environ, {"BOT_ACTIVE": "false", "PERSIST_RUNTIME_DIRS": "true", "REQUIRE_PERSISTENT_DATA": "false"}):
            cloud_runtime.prepare_runtime(self.root)
            (self.root / "reports/history.json").write_text("{}")
            cloud_runtime.prepare_runtime(self.root)
            self.assertTrue((self.root / "data/reports/history.json").exists())

    def test_status_uses_public_cloud_url(self):
        with patch.dict(os.environ, {"RECOVERY_PUBLIC_URL": "https://property.example"}):
            self.assertEqual(cloud_runtime.website_url(), "https://property.example")

    def test_staging_health_and_scan_routes(self):
        server = web_robot_app.RobotServer(("127.0.0.1", 0), web_robot_app.RobotHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        with patch.dict(os.environ, {"BOT_ACTIVE": "false"}), patch.object(web_robot_app.RobotHandler, "log_message"), patch.object(web_robot_app, "start_scan") as scan:
            worker.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urlopen(base + "/healthz", timeout=3) as response:
                    self.assertEqual(json.load(response), {"status": "ok", "mode": "staging"})
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(base + "/scan", data=b"days=7"), timeout=3)
                self.assertEqual(caught.exception.code, 503)
                caught.exception.close()
                scan.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        (self.root / "data").mkdir(parents=True)
        (self.root / "reports").mkdir()
        (self.root / "data/telegram_owner_alert_state.json").write_text('{"seen_urls":["fixture"]}')
        (self.root / "data/telegram_command_bot_state.json").write_text('{"offset":123}')
        (self.root / "data/owner_schedule.json").write_text('{"next_run_at":1234567890}')
        (self.root / "reports/owner_alert_decisions_fixture.json").write_text('{"accepted":["fixture"]}')
        (self.root / ".env.private").write_text("TELEGRAM_BOT_TOKEN=do-not-export")
        (self.root / "data/web_app_session_secret.txt").write_text("do-not-export")
        self.progress = scan_progress.ProgressStore(self.root / "data/recovery.sqlite3")
        job = self.progress.begin({"state": "data/telegram_owner_alert_state.json"})
        self.progress.status(job, "complete")
        with self.progress.connect() as connection:
            connection.execute("INSERT INTO deliveries VALUES ('fixture','sent','123',0)")
            connection.execute("INSERT INTO cache VALUES ('fixture','large expired page',0)")
        self.progress.set("browser_mode", True)
        self.progress.set("browser_service_secret", "do-not-export")
        self.archive = Path(self.temporary.name) / "private.tar.gz"
        self.destination = Path(self.temporary.name) / "destination"

    def final_export(self):
        with patch.object(telegram_command_bot, "listener_lock", return_value=nullcontext()), patch("telegram_owner_alert.install_run_lock"):
            return migrate_bot.export_snapshot(self.root, self.archive, final=True)

    def test_export_excludes_credentials_and_browser_secrets(self):
        manifest = migrate_bot.export_snapshot(self.root, self.archive)
        self.assertFalse(manifest["cutover_ready"])
        self.assertFalse(any(".env" in name or "secret" in name for name in manifest["files"]))
        self.assertTrue(self.progress.get("browser_mode"))

    def test_live_backup_cannot_activate_cloud(self):
        migrate_bot.export_snapshot(self.root, self.archive)
        with self.assertRaisesRegex(ValueError, "live backup"):
            migrate_bot.restore_snapshot(self.archive, self.destination)
        self.assertFalse((self.destination / "migration-ready.json").exists())

    def test_final_restore_preserves_history_offsets_schedule_and_receipts(self):
        self.final_export()
        migrate_bot.restore_snapshot(self.archive, self.destination)
        self.assertEqual(json.loads((self.destination / "telegram_owner_alert_state.json").read_text())["seen_urls"], ["fixture"])
        self.assertEqual(json.loads((self.destination / "telegram_command_bot_state.json").read_text())["offset"], 123)
        self.assertEqual(json.loads((self.destination / "owner_schedule.json").read_text())["next_run_at"], 1234567890)
        restored = scan_progress.ProgressStore(self.destination / "recovery.sqlite3")
        self.assertFalse(restored.get("browser_mode"))
        with restored.connect() as connection:
            self.assertEqual(connection.execute("SELECT status FROM deliveries WHERE key='fixture'").fetchone()[0], "sent")
            self.assertEqual(connection.execute("SELECT count(*) FROM cache").fetchone()[0], 0)
        self.assertTrue((self.destination / "reports/owner_alert_decisions_fixture.json").exists())
        self.assertTrue((self.destination / "migration-ready.json").exists())

    def test_existing_history_is_never_overwritten(self):
        self.final_export()
        self.destination.mkdir()
        (self.destination / "telegram_owner_alert_state.json").write_text("original")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            migrate_bot.restore_snapshot(self.archive, self.destination)
        self.assertEqual((self.destination / "telegram_owner_alert_state.json").read_text(), "original")

    def test_path_traversal_is_rejected(self):
        with tarfile.open(self.archive, "w:gz") as archive:
            member = tarfile.TarInfo("../escape.json")
            member.size = 2
            archive.addfile(member, io.BytesIO(b"{}"))
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            migrate_bot.restore_snapshot(self.archive, self.destination)

    def test_pending_scan_blocks_cross_host_export(self):
        self.progress.begin({"unfinished": True})
        with self.assertRaisesRegex(ValueError, "pending scan"):
            migrate_bot.export_snapshot(self.root, self.archive)

    def test_final_export_requires_stopped_local_listener(self):
        with patch.object(telegram_command_bot, "listener_lock", side_effect=RuntimeError("listener still running")):
            with self.assertRaisesRegex(RuntimeError, "still running"):
                migrate_bot.export_snapshot(self.root, self.archive, final=True)
        self.assertFalse(self.archive.exists())


if __name__ == "__main__":
    unittest.main()
