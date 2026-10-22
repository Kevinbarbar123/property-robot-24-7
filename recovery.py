from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path

import scan_progress
from reliability_config import load_reliability, public_url, recovery_path
from source_errors import SourceResponseError


def current_store():
    return scan_progress.CURRENT_STORE or scan_progress.ProgressStore()


def begin_scan(args):
    options = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items() if key not in {"telegram_token", "telegram_chat_id", "show_chat_id", "lock", "resume_job"}}
    path = Path(os.getenv("OLX_RECOVERY_DB", str(args.state.parent / "recovery.sqlite3")))
    scan_progress.CURRENT_STORE = scan_progress.ProgressStore(path)
    current_store().maintain()
    pending = current_store().latest_job()
    if pending and pending["status"] != "complete" and json.loads(pending["options"]) != options:
        raise SourceResponseError("An unfinished scan is saved. Send /resume to continue it before starting different scan options.", kind="pending_job")
    if getattr(args, "resume_job", ""):
        job = current_store().job(args.resume_job)
        if not job or json.loads(job["options"]) != options:
            raise ValueError("Resume options do not match the saved job")
        job_id = args.resume_job
    else:
        job_id = current_store().begin(options)
    scan_progress.CURRENT_JOB = job_id
    current_store().set("job_owner:" + job_id, os.getpid())
    current_store().status(job_id, "running")
    return job_id


def serialize_details(details):
    return asdict(details)


def restore_details(payload):
    from property_bot import Listing
    from telegram_owner_alert import SellerDetails
    values = dict(payload)
    listing = Listing(**values.pop("listing"))
    values["exclusion_reasons"] = tuple(values.get("exclusion_reasons", []))
    return SellerDetails(listing=listing, **values)


def page_work(url):
    if not scan_progress.CURRENT_JOB:
        return None
    return current_store().work(scan_progress.CURRENT_JOB, "search:" + url)


def save_page(url, details, pages):
    if scan_progress.CURRENT_JOB:
        current_store().save_work(scan_progress.CURRENT_JOB, "search:" + url, {"details": [serialize_details(item) for item in details], "pages": pages})


def detail_work(listing):
    if not scan_progress.CURRENT_JOB:
        return None
    payload = current_store().work(scan_progress.CURRENT_JOB, "detail:" + listing.url)
    return restore_details(payload) if payload else None


def save_detail(details):
    if scan_progress.CURRENT_JOB and not any(reason.startswith("detail fetch failed:") for reason in details.exclusion_reasons):
        current_store().save_work(scan_progress.CURRENT_JOB, "detail:" + details.listing.url, serialize_details(details))


def save_position(**values):
    if scan_progress.CURRENT_JOB:
        current_store().position(scan_progress.CURRENT_JOB, **values)


def verification_link():
    progress = current_store()
    incident = progress.current_incident()
    if not incident:
        return "No verification is currently required."
    if incident["kind"] != "captcha":
        return "This is an access restriction or temporary failure. Check /status; verification cannot override its retry deadline."
    base = public_url()
    if not base:
        return "Verification is required. The protected verification website is not configured yet. Set RECOVERY_PUBLIC_URL on the same host as the scanner."
    token = progress.link(incident["id"], load_reliability().verification_idle_seconds)
    return f"{base}/verify#{token}"


def recovery_status():
    progress = current_store()
    job = progress.latest_job()
    if not job:
        job = progress.job(progress.get("last_completed_job", ""))
    incident = progress.current_incident()
    position = json.loads(job.get("position", "{}"))
    with progress.connect() as connection:
        uncertain = connection.execute("SELECT count(*) FROM deliveries WHERE status IN ('sending','unknown')").fetchone()[0]
    return {
        "status": incident.get("kind", job.get("status", "ready")),
        "completed_work": progress.completed(job["id"]) if job else 0,
        "completed_listings": progress.completed_listings(job["id"]) if job else 0,
        "area": position.get("area", ""),
        "page": position.get("page", ""),
        "stage": position.get("stage", ""),
        "retry_at": incident.get("retry_at", 0),
        "provider": incident.get("provider", ""),
        "uncertain_deliveries": uncertain,
    }


def notify_event(token, chat_id, incident, event, last_success="unknown"):
    from telegram_owner_alert import send_telegram_message
    progress = current_store()
    key = f"incident:{incident['id']}:{event}"
    if progress.get(key + ":complete", False):
        return
    saved_message = progress.get(key + ":message")
    if saved_message:
        send_telegram_message(token, chat_id, saved_message, delivery_key=key)
        progress.set(key + ":complete", True)
        return
    status = recovery_status()
    titles = {"captcha": "Manual OLX verification required", "access_restricted": "OLX access restriction detected", "rate_limited": "OLX rate limit detected", "temporary": "OLX scraping stopped after repeated failures", "schema_changed": "OLX page format changed; scraping paused", "resumed": "OLX scraping resumed successfully"}
    message = f"{titles.get(event, event)}\n\nCollected listings: {status['completed_listings']}\nCompleted pages/details: {status['completed_work']}\nArea: {status['area'] or 'unknown'}\nPage: {status['page'] or 'n/a'}\nError type: {incident['kind']}\nLast successful scan: {last_success or 'unknown'}"
    if event == "captcha":
        message += "\n\nOpen this link and complete verification manually:\n" + verification_link()
    elif event != "resumed":
        message += "\n\nProgress is saved. " + ("Send /status for the next retry time." if event != "schema_changed" else "Parser review is required before /resume.")
    progress.set(key + ":message", message)
    send_telegram_message(token, chat_id, message, delivery_key=key)
    progress.set(key + ":complete", True)


def handle_failure(args, error, health):
    progress = current_store()
    config = load_reliability()
    deadline = error.retry_at or time.time() + config.temporary_cooldown
    error.retry_at = deadline
    incident = progress.incident(error, scan_progress.CURRENT_JOB)
    if scan_progress.CURRENT_JOB:
        progress.status(scan_progress.CURRENT_JOB, "paused")
    count_key = "incident_failures:" + incident["id"]
    failures = progress.get(count_key, 0) + 1
    progress.set(count_key, failures)
    if failures >= 3 and incident["kind"] == "temporary":
        progress.set("stopped_incident", incident["id"])
        if scan_progress.CURRENT_JOB:
            progress.status(scan_progress.CURRENT_JOB, "stopped")
    health.update(status=incident["kind"], error=str(error), last_attempt_at=time.strftime("%Y-%m-%dT%H:%M:%S"), retry_after=incident["retry_at"], recovery_job=scan_progress.CURRENT_JOB)
    token = args.telegram_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = args.telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
    if not args.dry_run and token and chat:
        try:
            notify_event(token, chat, incident, incident["kind"], health.get("last_success_at"))
        except Exception:
            progress.set("pending_incident_notice", incident)
    return incident


def resume_arguments():
    progress = current_store()
    job = progress.latest_job()
    if not job:
        return []
    incident = progress.current_incident()
    if incident and (incident["kind"] == "captcha" or incident["retry_at"] > time.time()):
        raise SourceResponseError("Complete verification or wait for the retry deadline before resuming.")
    if incident and incident["kind"] == "schema_changed":
        progress.resolve(incident["id"])
    if incident and progress.get("stopped_incident") == incident["id"]:
        progress.set("stopped_incident", None)
        progress.set("incident_failures:" + incident["id"], 0)
    result = ["--resume-job", job["id"]]
    for key, value in json.loads(job["options"]).items():
        if key == "pipeline":
            continue
        option = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                result.append(option)
        elif value is not None:
            result.extend([option, str(value)])
    return result


def _recovery_tick():
    import subprocess
    import sys
    from telegram_owner_alert import load_env_files
    load_env_files()
    progress = current_store()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    pending = progress.get("pending_incident_notice")
    if pending and token and chat:
        notify_event(token, chat, pending, pending["kind"])
        progress.set("pending_incident_notice", None)
    resumed = progress.get("resumed_incident")
    if resumed and token and chat:
        notify_event(token, chat, resumed, "resumed")
        progress.set("resumed_incident", None)
    incident = progress.current_incident()
    job = progress.latest_job()
    if not job:
        return
    if job["status"] == "running":
        from telegram_owner_alert import process_is_running
        owner = progress.get("job_owner:" + job["id"])
        if owner and not process_is_running(owner):
            failures = progress.get("crash_restarts:" + job["id"], 0) + 1
            progress.set("crash_restarts:" + job["id"], failures)
            if failures > 3:
                progress.status(job["id"], "stopped")
                return
            progress.set("resume_requested", True)
    due_retry = job["status"] == "paused" and incident and incident["kind"] in {"temporary", "rate_limited", "access_restricted"} and incident["retry_at"] <= time.time()
    if not progress.get("resume_requested", False) and not due_retry:
        return
    arguments = resume_arguments()
    if not arguments:
        return
    progress.set("resume_requested", False)
    root = Path(__file__).resolve().parent
    script = "main.py" if json.loads(job["options"]).get("pipeline") == "pro" else "telegram_owner_alert.py"
    try:
        completed = subprocess.run([sys.executable, "-u", str(root / script), *arguments], cwd=root, timeout=45 * 60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        progress.set("resume_requested", True)
        raise
    if completed.returncode == 4:
        progress.set("resume_requested", True)


def recovery_tick():
    with scan_progress.recovery_worker_lock() as acquired:
        if acquired:
            _recovery_tick()


def recovery_loop(stop_event):
    import logging
    from cloud_runtime import active
    while not stop_event.wait(15):
        if not active():
            continue
        try:
            recovery_tick()
        except Exception as error:
            logging.getLogger(__name__).warning("Recovery worker waiting after %s", type(error).__name__)
