from __future__ import annotations

import argparse
import traceback
import json
import os
import scan_progress
from pathlib import Path
from types import SimpleNamespace
import recovery
from source_errors import SourceResponseError
from telegram_owner_alert import install_run_lock, LOCK_PATH

from config import load_config
from database import LeadDatabase
from notifier import send_alerts
from scraper import scrape_olx


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Professional OLX owner lead pipeline.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml.")
    parser.add_argument("--dry-run", action="store_true", help="Do not send Telegram alerts.")
    parser.add_argument("--no-alerts", action="store_true", help="Store/export only.")
    parser.add_argument("--resume-job", default="", help=argparse.SUPPRESS)
    return parser


def run() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)
    try:
        install_run_lock(LOCK_PATH)
    except RuntimeError as error:
        print(str(error))
        return 4
    progress = scan_progress.ProgressStore()
    options = {"pipeline": "pro", "config": str(Path(args.config).resolve()), "dry_run": args.dry_run, "no_alerts": args.no_alerts}
    pending = progress.latest_job()
    if pending and json.loads(pending["options"]) != options:
        print("An unfinished scan is saved. Send /resume before starting a different pipeline.")
        return 4
    job_id = args.resume_job or progress.begin(options)
    if args.resume_job and json.loads(progress.job(job_id).get("options", "{}")) != options:
        raise ValueError("Resume options do not match the saved pipeline")
    scan_progress.CURRENT_JOB = job_id
    scan_progress.CURRENT_STORE = progress
    progress.set("job_owner:" + job_id, os.getpid())
    db = LeadDatabase(config.database_path)
    run_id = db.start_run()
    new_listings = []
    try:
        listings = scrape_olx(config)
        for listing in listings:
            key = "pro_listing:" + listing.url
            pending_listing = progress.work(job_id, key)
            if pending_listing is not None:
                db.save_listing(listing)
                new_listings.append(listing)
                continue
            if db.find_duplicate(listing) is None:
                progress.save_work(job_id, key, listing.as_dict())
            if db.save_listing(listing):
                new_listings.append(listing)
        db.export_csv(config.csv_export_path)
        db.export_json(config.json_export_path)
        if not args.no_alerts:
            send_alerts(
                config.telegram_bot_token,
                config.telegram_chat_id,
                new_listings,
                config.owner_score_threshold,
                dry_run=args.dry_run,
                delivery_key="pro:" + job_id,
            )
        db.finish_run(run_id, "ok", len(listings), len(new_listings))
        progress.status(job_id, "complete")
        print(f"Scraped {len(listings)} listings; saved {len(new_listings)} new leads.")
        print(f"SQLite: {config.database_path}")
        print(f"CSV: {config.csv_export_path}")
        print(f"JSON: {config.json_export_path}")
        return 0
    except Exception as exc:
        if isinstance(exc, SourceResponseError):
            recovery.handle_failure(SimpleNamespace(dry_run=args.dry_run or args.no_alerts, telegram_token=config.telegram_bot_token, telegram_chat_id=config.telegram_chat_id), exc, {})
        db.log_error(run_id, "main", f"{type(exc).__name__}: {exc}")
        db.finish_run(run_id, "error", 0, 0, str(exc))
        traceback.print_exc()
        return 1
    finally:
        db.close()
        scan_progress.CURRENT_JOB = ""
        scan_progress.CURRENT_STORE = None


if __name__ == "__main__":
    raise SystemExit(run())
