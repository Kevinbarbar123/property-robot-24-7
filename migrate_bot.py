from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from contextlib import ExitStack, closing
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parent
STATE_NAMES = {
    "telegram_owner_alert_state.json", "telegram_command_bot_state.json",
    "telegram_outbox.json", "owner_schedule.json", "web_app_state.json",
    "broker_blacklist.json", "lead_status.json", "daily_schedule.json",
    "followup_reminders.json", "owner_phrases.json", "owner_scan_health.json",
}
DATABASE_NAMES = {"recovery.sqlite3", "pro_owner_leads.sqlite"}


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def export_snapshot(root: Path, output: Path, final: bool = False) -> dict:
    with ExitStack() as stack:
        if final:
            from telegram_command_bot import listener_lock
            from telegram_owner_alert import install_run_lock
            stack.enter_context(listener_lock(root / "data/telegram_listener.lock"))
            install_run_lock(root / "data/telegram_owner_alert.lock")
            install_run_lock(root / "data/owner_schedule.lock")
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            for name in STATE_NAMES:
                source = root / "data" / name
                if source.exists():
                    json.loads(source.read_text(encoding="utf-8-sig"))
                    shutil.copy2(source, stage / name)
            if not (stage / "telegram_owner_alert_state.json").exists():
                raise ValueError("Seen-listing history is missing; refusing an empty migration.")
            for name in DATABASE_NAMES:
                source = root / "data" / name
                if not source.exists():
                    continue
                with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original:
                    with closing(sqlite3.connect(stage / name)) as target:
                        original.backup(target)
                        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                            raise ValueError("Database integrity check failed.")
                        if name == "recovery.sqlite3":
                            if target.execute("SELECT count(*) FROM jobs WHERE status!='complete'").fetchone()[0]:
                                raise ValueError("Finish or resolve the pending scan before moving hosts.")
                            target.execute("DELETE FROM cache")
                            target.execute("DELETE FROM leases")
                            target.execute("DELETE FROM hosts")
                            target.execute("DELETE FROM links")
                            target.execute("DELETE FROM portal_sessions")
                            target.execute("DELETE FROM meta WHERE key LIKE 'browser_%' OR key LIKE 'job_owner:%' OR key='resume_requested'")
                            target.commit()
                            target.execute("VACUUM")
            if not (stage / "recovery.sqlite3").exists():
                raise ValueError("Recovery receipts database is missing.")
            reports = stage / "reports"
            reports.mkdir()
            for source in (root / "reports").glob("owner_alert_decisions_*.json"):
                json.loads(source.read_text(encoding="utf-8-sig"))
                shutil.copy2(source, reports / source.name)
            manifest = {
                "version": 1, "created_at": time.time(), "cutover_ready": final,
                "files": {path.relative_to(stage).as_posix(): digest(path)
                          for path in stage.rglob("*") if path.is_file()},
            }
            (stage / "migration-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("xb") as archive_file:
                if os.name != "nt":
                    os.chmod(output, 0o600)
                with tarfile.open(fileobj=archive_file, mode="w:gz") as archive:
                    for path in stage.rglob("*"):
                        if path.is_file():
                            archive.add(path, arcname=path.relative_to(stage).as_posix(), recursive=False)
            return manifest


def restore_snapshot(archive_path: Path, data: Path) -> dict:
    data.mkdir(parents=True, exist_ok=True)
    occupied = [path for path in data.iterdir() if path.name != "logs" and path.resolve() != archive_path.resolve()]
    if occupied:
        raise ValueError("Destination already contains bot data; refusing to overwrite it.")
    with tempfile.TemporaryDirectory(dir=data.parent) as temporary:
        stage = Path(temporary)
        with tarfile.open(archive_path, "r:gz") as archive:
            total_size = 0
            seen = set()
            for member in archive:
                path = PurePosixPath(member.name)
                allowed = (member.name in STATE_NAMES | DATABASE_NAMES | {"migration-manifest.json"}
                           or len(path.parts) == 2 and path.parts[0] == "reports"
                           and path.name.startswith("owner_alert_decisions_") and path.suffix == ".json")
                total_size += member.size
                if (not member.isfile() or not allowed or ".." in path.parts or "\\" in member.name
                        or path.is_absolute() or member.name in seen or total_size > 2 * 1024**3):
                    raise ValueError("Unsafe or oversized migration archive.")
                seen.add(member.name)
                target = stage.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as incoming, target.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                os.chmod(target, 0o600)
                os.utime(target, (member.mtime, member.mtime))
        manifest = json.loads((stage / "migration-manifest.json").read_text(encoding="utf-8"))
        if manifest.get("version") != 1 or manifest.get("cutover_ready") is not True:
            raise ValueError("This is only a live backup. Stop the local services and export --final for cutover.")
        files = manifest.get("files", {})
        if set(files) != seen - {"migration-manifest.json"}:
            raise ValueError("Migration file inventory does not match.")
        for name, expected in files.items():
            path = stage / name
            if digest(path) != expected:
                raise ValueError("Migration checksum does not match.")
            if path.suffix == ".json":
                json.loads(path.read_text(encoding="utf-8-sig"))
        for required in ("telegram_owner_alert_state.json", "recovery.sqlite3"):
            if required not in files:
                raise ValueError("Migration is missing essential history.")
        for name in DATABASE_NAMES & set(files):
            with closing(sqlite3.connect(stage / name)) as connection:
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Imported database integrity check failed.")
        for path in stage.iterdir():
            shutil.move(str(path), str(data / path.name))
        marker = data / "migration-ready.json"
        marker.write_text(json.dumps({"version": 1, "imported_at": time.time(), "source_created_at": manifest["created_at"]}), encoding="utf-8")
        os.chmod(marker, 0o600)
        return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Private bot history backup and safe cloud migration; never upload archives to GitHub.")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--final", action="store_true", help="Require stopped bot/scanner and mark the snapshot ready for cutover.")
    restore = commands.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--data", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    if args.command == "export":
        manifest = export_snapshot(ROOT, args.output, args.final)
    else:
        from cloud_runtime import active
        if active():
            parser.error("Set BOT_ACTIVE=false before restoring cloud state.")
        manifest = restore_snapshot(args.archive, args.data)
    print(f"Completed: {len(manifest['files'])} private files; cutover_ready={manifest['cutover_ready']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
