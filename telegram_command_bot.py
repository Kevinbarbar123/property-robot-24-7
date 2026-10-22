#!/usr/bin/env python3
"""
Crash-resistant Telegram command listener for on-demand property scans.

Commands:
  /latest  Scan OLX now and send new likely-owner listings.
  /last7   Scan listings from the last 7 days.
  /last14  Scan listings from the last 14 days.
  /last21  Scan listings from the last 21 days.
  /status  Show bot and scan state.
  /help    Show command help.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import recovery
from cloud_runtime import active, website_url
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from telegram_owner_alert import (
    OLX_TARGET_SEARCHES,
    STATE_PATH,
    TelegramAPIError,
    load_env_files,
    send_telegram_message,
    telegram_api,
)


POLL_STATE_PATH = Path("data/telegram_command_bot_state.json")
SCAN_STATE_PATH = Path("data/telegram_command_bot_scan_state.json")
HEALTH_PATH = Path("data/telegram_command_bot_health.json")
REPORTS_DIR = Path("reports")
ALERT_SCRIPT = Path(__file__).with_name("telegram_owner_alert.py")
LOG_PATH = Path("logs/telegram_command_bot.events.log")
SCAN_LOG_PATH = Path("logs/telegram_command_bot.scan.log")
CRASH_LOG_PATH = Path("logs/telegram_command_bot.crash.log")
BROKER_BLACKLIST_PATH = Path("data/broker_blacklist.json")
LEAD_STATUS_PATH = Path("data/lead_status.json")
DAILY_SCHEDULE_PATH = Path("data/daily_schedule.json")
REMINDERS_PATH = Path("data/followup_reminders.json")
OWNER_PHRASES_PATH = Path("data/owner_phrases.json")
OWNER_ALERT_LOCK_PATH = Path("data/telegram_owner_alert.lock")
SCAN_COOLDOWN_SECONDS = 10 * 60
SCAN_TIMEOUT_SECONDS = 30 * 60
POLL_TIMEOUT_SECONDS = 20
NETWORK_BACKOFF_MIN_SECONDS = 5.0
NETWORK_BACKOFF_MAX_SECONDS = 120.0
SCAN_LOCK = threading.Lock()
LISTENER_LOCK_PATH = Path(__file__).resolve().parent / "data/telegram_listener.lock"
CONFLICT_RETRY_SECONDS = 15 * 60


@contextmanager
def listener_lock(path: Path = LISTENER_LOCK_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("A Telegram listener already owns this workspace.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def pause_after_conflict() -> None:
    save_health(status="conflict", retry_after=time.time() + CONFLICT_RETRY_SECONDS)
    log_event("Telegram delivery conflict; listener paused for 15 minutes before checking again")

HELP_TEXT = """Property bot commands:

/latest - scan OLX now and send new likely-owner listings
/last7 - scan the last 7 days and send new likely-owner listings
/last14 - scan the last 14 days and send new likely-owner listings
/last21 - scan the last 21 days and send new likely-owner listings
/maybe - show borderline owner leads to check manually
/areas - show areas producing owner leads
/why <olx link or text> - explain why a listing was sent or rejected
/blacklist <seller/phone/url> - stop sending this broker/seller again
/schedule HH:MM - run /latest every day at that local time
/hot - show best hot owner leads
/export - save latest owner leads to CSV
/prices - show below-market owner leads
/agencies - show agency/broker heatmap
/reposts - show repeated/suspicious repost patterns
/learnowner <phrase> - teach owner wording
/contacted <olx link> - mark lead contacted
/interested <olx link> - mark lead interested
/notinterested <olx link> - mark lead not interested
/wrongnumber <olx link> - mark lead wrong number
/leads - show saved lead statuses
/remind <olx link> <note> - save follow-up reminder
/reminders - show follow-up reminders
/summary - quick spoken-style scan summary
/status - show bot state
/verify - open the protected manual verification session
/resume - continue saved work when eligible
/ask - ask the AI helper how to use the robot or what to do next
/help - show this help

Shared mode is enabled: commands and bot replies are mirrored to every approved phone.

If Telegram is flaky, use the web app link shown in /status."""

BOT_COMMANDS = [
    {"command": "verify", "description": "Manual OLX verification link"},
    {"command": "resume", "description": "Resume saved scan"},
    {"command": "latest", "description": "Scan OLX"},
    {"command": "last7", "description": "Scan last 7 days"},
    {"command": "hot", "description": "Best owner leads"},
    {"command": "maybe", "description": "Borderline owner leads"},
    {"command": "areas", "description": "Area performance"},
    {"command": "why", "description": "Explain a listing"},
    {"command": "prices", "description": "Below-market owner leads"},
    {"command": "agencies", "description": "Agency/broker heatmap"},
    {"command": "reposts", "description": "Repeated post patterns"},
    {"command": "export", "description": "Export owner leads CSV"},
    {"command": "schedule", "description": "Daily scan schedule"},
    {"command": "leads", "description": "Saved lead statuses"},
    {"command": "contacted", "description": "Mark contacted"},
    {"command": "interested", "description": "Mark interested"},
    {"command": "remind", "description": "Save follow-up reminder"},
    {"command": "reminders", "description": "Show reminders"},
    {"command": "learnowner", "description": "Teach owner wording"},
    {"command": "summary", "description": "Quick scan summary"},
    {"command": "ask", "description": "Ask AI helper"},
    {"command": "status", "description": "Bot health"},
    {"command": "help", "description": "Show help"},
]

ASK_HELP_TEXT = """AI helper:

Ask me like this:
/ask what should I do next?
/ask how do I check if this looks like an agency?
/ask explain the latest scan result

I always answer. With OPENAI_API_KEY I use the OpenAI API; without it I use the built-in local helper."""


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else dict(default)
    except json.JSONDecodeError:
        backup_path = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d_%H%M%S')}.bak")
        try:
            os.replace(path, backup_path)
            log_event(f"state file corrupt path={path}; backed up to {backup_path}")
        except OSError as exc:
            log_event(f"state file corrupt path={path}; backup failed: {exc}")
        return dict(default)
    except OSError as exc:
        log_event(f"state file unreadable path={path}: {exc}")
        return dict(default)


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    last_error: OSError | None = None
    for _ in range(3):
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
            os.replace(temp_path, path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.2)
    if last_error is not None:
        log_event(f"state file save failed path={path}: {last_error}")
    try:
        temp_path.unlink(missing_ok=True)
    except OSError:
        pass


def log_event(message: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{now_text()} {message}\n")
    except OSError:
        pass


def log_crash(exc: BaseException) -> None:
    try:
        CRASH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CRASH_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"\n--- {now_text()} {type(exc).__name__}: {exc} ---\n")
            handle.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except OSError:
        pass


def save_health(**updates: object) -> None:
    health = load_json(HEALTH_PATH, {})
    health.update(updates)
    health["updated_at"] = now_text()
    save_json(HEALTH_PATH, health)


def append_scan_output(command: str, completed: subprocess.CompletedProcess[str]) -> None:
    try:
        SCAN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SCAN_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"\n--- {now_text()} command={command} returncode={completed.returncode} ---\n")
            if completed.stdout:
                handle.write("[stdout]\n")
                handle.write(completed.stdout.rstrip() + "\n")
            if completed.stderr:
                handle.write("[stderr]\n")
                handle.write(completed.stderr.rstrip() + "\n")
    except OSError as exc:
        log_event(f"scan log write failed: {exc}")


def safe_send_message(token: str, chat_id: int | str, message: str) -> bool:
    try:
        send_telegram_message(token, str(chat_id), message)
        return True
    except (TelegramAPIError, HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        log_event(f"send failed chat={chat_id}: {type(exc).__name__}: {exc}")
        save_health(last_send_error=str(exc), last_send_error_at=now_text())
        print(f"Telegram send error: {exc}", file=sys.stderr)
        return False


def get_updates(token: str, offset: int | None) -> list[dict]:
    params = {"timeout": str(POLL_TIMEOUT_SECONDS)}
    if offset is not None:
        params["offset"] = str(offset)
    result = telegram_api(token, "getUpdates", params, timeout=POLL_TIMEOUT_SECONDS + 12, retries=1)
    updates = result.get("result", [])
    return updates if isinstance(updates, list) else []


def sync_bot_commands(token: str) -> None:
    try:
        telegram_api(token, "setMyCommands", {"commands": json.dumps(BOT_COMMANDS)}, timeout=15, retries=1)
        log_event("telegram bot command menu synced")
        save_health(last_command_menu_error="", last_command_menu_error_at="")
    except (TelegramAPIError, HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        log_event(f"telegram bot command menu sync failed: {type(exc).__name__}: {exc}")
        save_health(last_command_menu_error=str(exc), last_command_menu_error_at=now_text())


def configured_chat_ids(configured_chat_id: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[,;\s]+", configured_chat_id or "")
        if item.strip()
    ]


def allowed_chat(chat_id: int | str, configured_chat_id: str) -> bool:
    return str(chat_id) in set(configured_chat_ids(configured_chat_id))


def shared_reply_chat_id(chat_id: int | str, configured_chat_id: str) -> str:
    chat_ids = configured_chat_ids(configured_chat_id)
    return ",".join(chat_ids) if len(chat_ids) > 1 else str(chat_id)


def shared_command_notice(chat_id: int | str, command: str) -> str:
    return f"Shared conversation: chat {chat_id} sent /{command}."


def normalize_command(text: str) -> str:
    words = text.split()
    if not words:
        return ""
    command_index = 1 if words[0].strip().lower().lstrip("/") in {"command", "commend"} and len(words) > 1 else 0
    return words[command_index].split("@")[0].strip().lower().lstrip("/")


def scan_label(created_within_days: int) -> str:
    return f"last{created_within_days}" if created_within_days else "latest"


def recent_scan_message(created_within_days: int, started_at: float) -> str:
    elapsed = max(0, int(time.time() - started_at))
    minutes = max(1, elapsed // 60)
    return f"I already started a {scan_label(created_within_days)} scan about {minutes} minute(s) ago. Send /status to check the latest completed run."


def scan_running_message(scan_state: dict) -> str:
    started_at = float(scan_state.get("last_scan_started_at") or 0)
    elapsed_minutes = max(1, int((time.time() - started_at) // 60)) if started_at else 1
    command = scan_state.get("last_scan_command", "scan")
    return f"A {command} scan is already running for about {elapsed_minutes} minute(s). I will send the result when it finishes."


def owner_alert_lock_active() -> bool:
    if not OWNER_ALERT_LOCK_PATH.exists():
        return False
    data = load_json(OWNER_ALERT_LOCK_PATH, {})
    try:
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        return True
    if not pid:
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def recover_stale_scan_state(reason: str = "Recovered stale scan state after bot restart.") -> None:
    scan_state = load_json(SCAN_STATE_PATH, {})
    started_at = float(scan_state.get("last_scan_started_at") or 0)
    finished_at = float(scan_state.get("last_scan_finished_at") or 0)
    if started_at and finished_at < started_at and (time.time() - started_at > SCAN_TIMEOUT_SECONDS or not owner_alert_lock_active()):
        scan_state.update(
            {
                "last_scan_finished_at": time.time(),
                "last_scan_returncode": 130,
                "last_scan_error": reason,
                "last_scan_recovered_at": time.time(),
            }
        )
        save_json(SCAN_STATE_PATH, scan_state)
        log_event(f"stale scan state recovered: {reason}")


def status_message() -> str:
    recovery_state = recovery.recovery_status()
    state = load_json(STATE_PATH, {"seen_urls": []})
    scan_state = load_json(SCAN_STATE_PATH, {})
    health = load_json(HEALTH_PATH, {})
    source_health = load_json(Path("data/owner_scan_health.json"), {})
    schedule = load_json(Path("data/owner_schedule.json"), {})
    started_at = float(scan_state.get("last_scan_started_at") or 0)
    finished_at = float(scan_state.get("last_scan_finished_at") or 0)
    current_scan = "none"
    if started_at and finished_at < started_at:
        current_scan = scan_running_message(scan_state)
    return "\n".join(
        [
            "Bot is alive.",
            f"Recovery: {recovery_state['status']}; completed pages/details: {recovery_state['completed_work']}; area: {recovery_state['area'] or 'n/a'}; stage: {recovery_state['stage'] or 'n/a'}",
            f"Next eligible retry: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(recovery_state['retry_at'])) if recovery_state['retry_at'] else 'none'}",
            f"Telegram deliveries needing review: {recovery_state['uncertain_deliveries']}",
            f"OLX search: {source_health.get('status', 'not checked')}",
            f"Source message: {source_health.get('error', '') if source_health.get('status') != 'ok' else 'none'}",
            f"Independent schedule: {schedule.get('status', 'not installed')}",
            f"Seen listings: {len(state.get('seen_urls', []))}",
            f"Last owner scan: {state.get('last_run_at', 'never')}",
            f"Current scan: {current_scan}",
            f"Last command scan: {scan_state.get('last_scan_command', 'none')}",
            f"Last command result: {scan_state.get('last_scan_returncode', 'none')}",
            f"Last command error: {scan_state.get('last_scan_error', '') or 'none'}",
            f"Last poll OK: {health.get('last_poll_ok_at', 'not yet')}",
            f"Web app: {website_url()}",
        ]
    )


def newest_report_summary() -> str:
    reports = sorted(REPORTS_DIR.glob("owner_alert_decisions_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not reports:
        return "No scan reports saved yet."
    data = load_json(reports[0], {})
    summary = data.get("summary") or {}
    accepted = data.get("accepted") or []
    rejected = data.get("rejected") or []
    areas = sorted({str(item.get("city") or "") for item in accepted if item.get("city")})[:12]
    phones = sum(1 for item in accepted if str(item.get("phone_number") or "").strip())
    return "\n".join(
        [
            f"Latest report file: {reports[0].name}",
            f"Accepted likely owners: {summary.get('accepted', len(accepted))}",
            f"Rejected/risky rows: {summary.get('rejected', len(rejected))}",
            f"Accepted phone numbers: {phones}",
            f"Areas with accepted matches: {', '.join(areas) if areas else 'none'}",
        ]
    )


def report_files(limit: int = 30) -> list[Path]:
    return sorted(REPORTS_DIR.glob("owner_alert_decisions_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


def latest_report() -> tuple[Path | None, dict]:
    reports = report_files(1)
    if not reports:
        return None, {}
    return reports[0], load_json(reports[0], {})


def item_identity(item: dict) -> str:
    parts = [
        str(item.get("url") or ""),
        str(item.get("title") or ""),
        str(item.get("seller_name") or ""),
        str(item.get("phone_number") or ""),
        str(item.get("agency_name") or ""),
        str(item.get("agent_code") or ""),
    ]
    return " ".join(parts).lower()


def format_report_item(item: dict, index: int) -> str:
    confidence = str(item.get("confidence") or "UNKNOWN")
    city = str(item.get("city") or "Unknown")
    purpose = str(item.get("purpose") or "listing")
    title = str(item.get("title") or "Untitled")
    seller = str(item.get("seller_name") or "unknown seller")
    phone = str(item.get("phone_number") or "no phone")
    score = item.get("owner_score", "n/a")
    url = str(item.get("url") or "")
    why = item.get("why_sent") or item.get("exclusion_reasons") or []
    why_text = "; ".join(str(reason) for reason in why[:3]) if isinstance(why, list) else str(why)
    return "\n".join(
        [
            f"{index}. {confidence} | {city} {purpose}: {title}",
            f"Seller: {seller} | Phone: {phone} | Score: {score}",
            f"Why: {why_text or 'no warning signs saved'}",
            f"Link: {url}",
        ]
    )


def maybe_message(limit: int = 10) -> str:
    report, data = latest_report()
    if not report:
        return "No report found yet. Run /latest first."
    candidates = []
    current_areas = set(OLX_TARGET_SEARCHES)
    for item in data.get("rejected") or []:
        if str(item.get("city") or "") not in current_areas:
            continue
        score = item.get("owner_score")
        reasons = " ".join(str(reason).lower() for reason in item.get("exclusion_reasons") or [])
        seller_posts = int(item.get("seller_target_post_count") or 0)
        if isinstance(score, int) and score <= 8 and seller_posts <= 3:
            candidates.append(item)
        elif "owner wording found" in reasons:
            candidates.append(item)
    candidates = sorted(candidates, key=lambda item: (item.get("owner_score", 99), str(item.get("city") or "")))[:limit]
    if not candidates:
        return f"No maybe-owner rows in latest report: {report.name}"
    lines = [f"Maybe-owner rows from {report.name}:", "These are not confirmed owners. Check manually before contacting."]
    for index, item in enumerate(candidates, start=1):
        lines.extend(["", format_report_item(item, index)])
    return "\n".join(lines)[:3900]


def area_report_message(report_count: int = 12) -> str:
    accepted_counter: Counter[str] = Counter()
    phone_counter: Counter[str] = Counter()
    maybe_counter: Counter[str] = Counter()
    scanned_reports = 0
    current_areas = set(OLX_TARGET_SEARCHES)
    for report in report_files(report_count):
        data = load_json(report, {})
        scanned_reports += 1
        for item in data.get("accepted") or []:
            city = str(item.get("city") or "Unknown")
            if city not in current_areas:
                continue
            accepted_counter[city] += 1
            if str(item.get("phone_number") or "").strip():
                phone_counter[city] += 1
        for item in data.get("rejected") or []:
            city = str(item.get("city") or "Unknown")
            if city not in current_areas:
                continue
            score = item.get("owner_score")
            if isinstance(score, int) and score <= 8:
                maybe_counter[city] += 1
    if not scanned_reports:
        return "No reports yet."
    areas = sorted(set(accepted_counter) | set(phone_counter) | set(maybe_counter))
    if not areas:
        return f"No owner leads found in the last {scanned_reports} reports."
    lines = [f"Area performance from last {scanned_reports} reports:"]
    for area in sorted(areas, key=lambda name: (-accepted_counter[name], -phone_counter[name], name))[:25]:
        lines.append(f"- {area}: {accepted_counter[area]} owners, {phone_counter[area]} phones, {maybe_counter[area]} maybe")
    return "\n".join(lines)


def why_message(text: str) -> str:
    query = re.sub(r"^/\w+(?:@\w+)?\s*", "", text, count=1, flags=re.IGNORECASE).strip().lower()
    if not query:
        return "Send /why followed by an OLX link, seller name, phone, or title words."
    for report in report_files(40):
        data = load_json(report, {})
        for section in ("accepted", "rejected"):
            for item in data.get(section) or []:
                if query in item_identity(item):
                    status = "SENT as owner" if section == "accepted" else "REJECTED / maybe"
                    return "\n".join(
                        [
                            f"{status} in {report.name}",
                            format_report_item(item, 1),
                            f"Rejected reasons: {'; '.join(str(r) for r in item.get('exclusion_reasons') or []) or 'none'}",
                        ]
                    )[:3900]
    return "I could not find that listing in saved reports. Run /last7 or send a more exact OLX link."


def list_json_items(path: Path) -> list[str]:
    data = load_json(path, {"items": []})
    items = data.get("items") if isinstance(data, dict) else []
    return [str(item).strip() for item in items if str(item).strip()] if isinstance(items, list) else []


def save_json_items(path: Path, items: list[str]) -> None:
    save_json(path, {"items": sorted(set(items), key=str.lower), "updated_at": now_text()})


def blacklist_message(text: str) -> str:
    term = re.sub(r"^/\w+(?:@\w+)?\s*", "", text, count=1, flags=re.IGNORECASE).strip()
    items = list_json_items(BROKER_BLACKLIST_PATH)
    if not term or term.lower() in {"list", "show"}:
        return "Broker blacklist:\n" + ("\n".join(f"- {item}" for item in items[:50]) if items else "empty")
    if term.lower().startswith("remove "):
        target = term[7:].strip().lower()
        kept = [item for item in items if item.lower() != target]
        save_json_items(BROKER_BLACKLIST_PATH, kept)
        return f"Removed from blacklist: {term[7:].strip()}"
    digits = re.sub(r"\D", "", term)
    if term.isdigit() and len(digits) < 7:
        return "Not blacklisted: short numeric IDs can match unrelated listings. Send a seller name, full phone, UUID, or OLX URL."
    normalized = re.sub(r"[^a-z0-9]", "", term.lower())
    if not term.lower().startswith(("http://", "https://")) and len(normalized) < 4:
        return "Not blacklisted: the value is too short. Send a seller name, full phone, UUID, or OLX URL."
    items.append(term)
    save_json_items(BROKER_BLACKLIST_PATH, items)
    return f"Blacklisted broker/seller term: {term}\nFuture scans will reject listings matching it."


def extract_lead_url(text: str) -> str:
    match = re.search(r"https?://\S+", text)
    if match:
        return match.group(0).rstrip(").,")
    return re.sub(r"^/\w+(?:@\w+)?\s*", "", text, count=1, flags=re.IGNORECASE).strip()


def lead_status_message(text: str, status: str) -> str:
    lead = extract_lead_url(text)
    if not lead:
        return f"Send /{status} followed by the OLX link."
    data = load_json(LEAD_STATUS_PATH, {"leads": {}})
    leads = data.get("leads") if isinstance(data.get("leads"), dict) else {}
    leads[lead] = {"status": status, "updated_at": now_text()}
    data["leads"] = leads
    save_json(LEAD_STATUS_PATH, data)
    return f"Marked lead as {status}: {lead}"


def leads_message(limit: int = 20) -> str:
    data = load_json(LEAD_STATUS_PATH, {"leads": {}})
    leads = data.get("leads") if isinstance(data.get("leads"), dict) else {}
    if not leads:
        return "No lead statuses saved yet."
    rows = sorted(leads.items(), key=lambda item: str(item[1].get("updated_at", "")), reverse=True)[:limit]
    return "\n".join(["Saved lead statuses:", *[f"- {value.get('status')} | {value.get('updated_at')} | {url}" for url, value in rows]])[:3900]


def latest_items(section: str = "accepted", limit: int = 80) -> list[dict]:
    _, data = latest_report()
    items = data.get(section) or []
    return [item for item in items if isinstance(item, dict)][:limit]


def item_price_per_sqm(item: dict) -> float | None:
    value = item.get("price_per_sqm")
    try:
        return float(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None


def hot_message(limit: int = 10) -> str:
    items = latest_items("accepted", 120)
    if not items:
        return "No accepted owner leads in the latest report."

    def hot_score(item: dict) -> tuple[int, float, float]:
        has_phone = 0 if str(item.get("phone_number") or "").strip() else 1
        owner_score = int(item.get("owner_score") or 99)
        published = item.get("published_timestamp") or 0
        try:
            published_value = -float(published)
        except (TypeError, ValueError):
            published_value = 0.0
        ratio = item_price_per_sqm(item) or 10**9
        return (has_phone, owner_score, published_value + ratio / 10**9)

    top = sorted(items, key=hot_score)[:limit]
    lines = ["Hot owner leads:"]
    for index, item in enumerate(top, start=1):
        lines.extend(["", format_report_item(item, index)])
    return "\n".join(lines)[:3900]


def export_message() -> str:
    report, data = latest_report()
    if not report:
        return "No report found yet."
    rows = data.get("accepted") or []
    if not rows:
        return "Latest report has no accepted owner leads to export."
    path = REPORTS_DIR / "latest_owner_leads.csv"
    fields = [
        "confidence",
        "city",
        "purpose",
        "title",
        "seller_name",
        "phone_number",
        "price_usd",
        "sqm",
        "price_per_sqm",
        "created",
        "url",
        "owner_score",
        "why_sent",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            output_row = {}
            for field in fields:
                value = row.get(field, "")
                if field == "why_sent":
                    if isinstance(value, list):
                        value = "; ".join(str(reason) for reason in value)
                    else:
                        value = str(value or "")
                output_row[field] = value
            writer.writerow(output_row)
    return f"Exported {len(rows)} owner leads from {report.name}:\n{path.resolve()}"


def prices_message(limit: int = 12) -> str:
    history: dict[tuple[str, str], list[float]] = {}
    for report in report_files(25):
        data = load_json(report, {})
        for section in ("accepted", "rejected"):
            for item in data.get(section) or []:
                ratio = item_price_per_sqm(item)
                if ratio is None or ratio <= 0:
                    continue
                key = (str(item.get("city") or "Unknown"), str(item.get("purpose") or ""))
                history.setdefault(key, []).append(ratio)
    deals = []
    for item in latest_items("accepted", 100):
        ratio = item_price_per_sqm(item)
        key = (str(item.get("city") or "Unknown"), str(item.get("purpose") or ""))
        values = sorted(history.get(key) or [])
        if ratio is None or len(values) < 4:
            continue
        median = values[len(values) // 2]
        if median and ratio <= median * 0.85:
            deals.append((ratio / median, median, item))
    if not deals:
        return "No clear below-market owner leads in the latest report."
    lines = ["Below-market owner leads:"]
    for index, (discount, median, item) in enumerate(sorted(deals, key=lambda row: row[0])[:limit], start=1):
        lines.extend(
            [
                "",
                format_report_item(item, index),
                f"Market flag: ${item_price_per_sqm(item):,.0f}/sqm vs area median about ${median:,.0f}/sqm",
            ]
        )
    return "\n".join(lines)[:3900]


def agencies_message(limit: int = 18) -> str:
    city_counter: Counter[str] = Counter()
    seller_counter: Counter[str] = Counter()
    for report in report_files(20):
        data = load_json(report, {})
        for item in data.get("rejected") or []:
            reasons = " ".join(str(reason).lower() for reason in item.get("exclusion_reasons") or [])
            if "agency" not in reasons and "seller has" not in reasons and "phone appears" not in reasons:
                continue
            city_counter[str(item.get("city") or "Unknown")] += 1
            seller = str(item.get("agency_name") or item.get("seller_name") or "Unknown").strip()
            seller_counter[seller] += 1
    if not city_counter:
        return "No agency heatmap data yet."
    lines = ["Agency/broker heatmap:", "", "Areas:"]
    lines.extend(f"- {city}: {count}" for city, count in city_counter.most_common(limit))
    lines.extend(["", "Top agency/seller names:"])
    lines.extend(f"- {seller}: {count}" for seller, count in seller_counter.most_common(limit) if seller)
    return "\n".join(lines)[:3900]


def reposts_message(limit: int = 15) -> str:
    groups: dict[str, list[dict]] = {}
    for report in report_files(30):
        data = load_json(report, {})
        for section in ("accepted", "rejected"):
            for item in data.get(section) or []:
                title = re.sub(r"\W+", " ", str(item.get("title") or "").lower()).strip()
                if len(title) < 10:
                    continue
                seller = re.sub(r"\W+", "", str(item.get("seller_name") or item.get("agency_name") or "").lower())
                phone = re.sub(r"\D+", "", str(item.get("phone_number") or ""))
                city = re.sub(r"\W+", "", str(item.get("city") or "").lower())
                signature = "|".join(part for part in (city, title[:70], seller[:25] or phone[:10]) if part)
                if signature:
                    groups.setdefault(signature, []).append(item)

    repeats = []
    for items in groups.values():
        urls = {str(item.get("url") or "") for item in items if item.get("url")}
        if len(items) >= 2 and len(urls) >= 2:
            newest = max(items, key=lambda item: str(item.get("created") or item.get("published_timestamp") or ""))
            repeats.append((len(items), newest, sorted(urls)))

    if not repeats:
        return "No repeated repost patterns found in recent reports."
    lines = ["Repeated/reposted patterns:", "Use these carefully: reposting can be owner or broker behavior."]
    for index, (count, item, urls) in enumerate(sorted(repeats, key=lambda row: -row[0])[:limit], start=1):
        lines.extend(
            [
                "",
                f"{index}. {count} similar posts | {item.get('city', 'Unknown')} | {item.get('seller_name') or 'unknown seller'}",
                str(item.get("title") or "Untitled"),
                f"Newest: {urls[-1]}",
            ]
        )
    return "\n".join(lines)[:3900]


def schedule_message(text: str) -> str:
    value = re.sub(r"^/\w+(?:@\w+)?\s*", "", text, count=1, flags=re.IGNORECASE).strip().lower()
    data = load_json(DAILY_SCHEDULE_PATH, {})
    if not value or value in {"status", "show"}:
        if not data.get("enabled"):
            return "Daily schedule is off. Use /schedule HH:MM to enable it."
        return f"Daily /latest schedule: {data.get('time')} local time. Last run date: {data.get('last_run_date', 'never')}"
    if value in {"off", "stop", "disable"}:
        data["enabled"] = False
        save_json(DAILY_SCHEDULE_PATH, data)
        return "Daily schedule disabled."
    if not re.fullmatch(r"\d{1,2}:\d{2}", value):
        return "Use /schedule HH:MM, for example /schedule 09:00, or /schedule off."
    hour, minute = [int(part) for part in value.split(":")]
    if hour > 23 or minute > 59:
        return "Invalid time. Use 00:00 to 23:59."
    save_json(DAILY_SCHEDULE_PATH, {"enabled": True, "time": f"{hour:02d}:{minute:02d}", "last_run_date": data.get("last_run_date", "")})
    return f"Daily /latest schedule enabled at {hour:02d}:{minute:02d} local time."


def reminder_message(text: str) -> str:
    body = re.sub(r"^/\w+(?:@\w+)?\s*", "", text, count=1, flags=re.IGNORECASE).strip()
    data = load_json(REMINDERS_PATH, {"items": []})
    items = data.get("items") if isinstance(data.get("items"), list) else []
    if not body or body.lower() in {"list", "show"}:
        if not items:
            return "No follow-up reminders saved."
        return "\n".join(["Follow-up reminders:", *[f"- {item.get('created_at')} | {item.get('note')} | {item.get('url')}" for item in items[-20:]]])[:3900]
    url = extract_lead_url(body)
    note = body.replace(url, "").strip() if url else body
    items.append({"url": url, "note": note or "follow up", "created_at": now_text()})
    save_json(REMINDERS_PATH, {"items": items})
    return f"Saved reminder: {note or 'follow up'}\n{url}"


def learn_owner_message(text: str) -> str:
    phrase = re.sub(r"^/\w+(?:@\w+)?\s*", "", text, count=1, flags=re.IGNORECASE).strip()
    items = list_json_items(OWNER_PHRASES_PATH)
    if not phrase or phrase.lower() in {"list", "show"}:
        return "Owner phrases:\n" + ("\n".join(f"- {item}" for item in items[:50]) if items else "empty")
    items.append(phrase)
    save_json_items(OWNER_PHRASES_PATH, items)
    return f"Learned owner phrase: {phrase}\nFuture scans can use this as positive owner wording."


def voice_summary_message() -> str:
    report, data = latest_report()
    if not report:
        return "No report yet."
    accepted = data.get("accepted") or []
    phones = sum(1 for item in accepted if str(item.get("phone_number") or "").strip())
    areas = Counter(str(item.get("city") or "Unknown") for item in accepted)
    top_areas = ", ".join(area for area, _ in areas.most_common(5)) or "none"
    return (
        "Voice-style summary: "
        f"Latest scan has {len(accepted)} owner leads, {phones} with phone numbers. "
        f"Best areas are {top_areas}. "
        "Open /hot for the best leads, /maybe for borderline leads, and /areas for area performance."
    )


def bot_context_summary() -> str:
    state = load_json(STATE_PATH, {"seen_urls": []})
    scan_state = load_json(SCAN_STATE_PATH, {})
    health = load_json(HEALTH_PATH, {})
    return "\n".join(
        [
            "Robot context:",
            f"Seen OLX listings: {len(state.get('seen_urls', []))}",
            f"Last owner scan: {state.get('last_run_at', 'never')}",
            f"Last command scan: {scan_state.get('last_scan_command', 'none')}",
            f"Last command result: {scan_state.get('last_scan_returncode', 'none')}",
            f"Last poll OK: {health.get('last_poll_ok_at', 'not yet')}",
            newest_report_summary(),
        ]
    )


def extract_response_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"].strip()
    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts).strip()


def local_ai_helper(question: str) -> str:
    lowered = question.lower()
    context = bot_context_summary()
    lines = ["AI helper is active in local mode. I can still guide you using the bot's saved context.", ""]

    if any(word in lowered for word in ["scan", "latest", "olx", "listing", "agency", "owner"]):
        lines.extend(
            [
                "For OLX, the clean daily flow is:",
                "1. Send /latest for new unseen listings only.",
                "2. Use /last7 only if you want to catch missed older posts.",
                "3. Open owner-phone rows first because they already have WhatsApp links.",
                "4. Leave old results in history so you do not repeat work.",
            ]
        )
    elif any(word in lowered for word in ["message", "whatsapp", "contact", "broker"]):
        lines.extend(
            [
                "Use a short professional WhatsApp message:",
                "Hello, I saw your apartment listing. Are you interested in working with a broker regarding your apartment?",
                "",
                "Best order: likely owner + phone number first, maybe-owner second, agency/risky rows last or skip.",
            ]
        )
    else:
        lines.extend(
            [
                "Best next move:",
                "1. Run /latest for fresh OLX owner leads.",
                "2. Open owner-phone rows first because they already include WhatsApp links.",
                "3. Ask me specific questions like: /ask which leads should I contact first?",
            ]
        )

    lines.extend(["", context])
    return "\n".join(lines)[:3500]


def ask_ai_helper(question: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("AI_API_KEY")
    if not api_key:
        return local_ai_helper(question)
    model = os.getenv("OPENAI_MODEL") or os.getenv("AI_MODEL") or "gpt-5.4-mini"
    payload = {
        "model": model,
        "max_output_tokens": 700,
        "input": [
            {
                "role": "system",
                "content": (
                    "You are the Property Robot assistant inside a private Telegram bot. "
                    "Help the user decide what to do next with OLX apartment owner-finding workflows. "
                    "Be concise, practical, and honest. Do not claim you scanned OLX unless the provided context says so. "
                    "Do not give legal guarantees. Do not open trades or give financial advice. "
                    "If asked about contacting owners, suggest polite broker outreach and WhatsApp workflow."
                ),
            },
            {
                "role": "user",
                "content": f"{bot_context_summary()}\n\nUser question:\n{question[:4000]}",
            },
        ],
    }
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8"))
        answer = extract_response_text(data)
        return answer or local_ai_helper(question)
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:500]
        except Exception:
            detail = str(exc)
        log_event(f"openai api error: {exc} {detail}")
        save_health(last_ai_error=f"{exc}", last_ai_error_at=now_text())
        return "AI service is unavailable; using the built-in helper.\n\n" + local_ai_helper(question)
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        log_event(f"openai transient error: {type(exc).__name__}: {exc}")
        save_health(last_ai_error=str(exc), last_ai_error_at=now_text())
        return "AI service is unavailable; using the built-in helper.\n\n" + local_ai_helper(question)


def ask_ai_message(text: str) -> str:
    question = re.sub(r"^/\w+(?:@\w+)?\s*", "", text, count=1, flags=re.IGNORECASE).strip()
    if not question:
        return ASK_HELP_TEXT
    return ask_ai_helper(question)


def looks_like_question(text: str) -> bool:
    lowered = text.lower().strip()
    if lowered.startswith("/"):
        return False
    if "?" in lowered:
        return True
    starters = ("how ", "what ", "why ", "when ", "where ", "can ", "should ", "do ", "does ", "is ", "are ")
    return len(lowered.split()) >= 4 and lowered.startswith(starters)


def scan_command_args(chat_id: int | str, created_within_days: int) -> list[str]:
    command = [sys.executable, str(ALERT_SCRIPT), "--telegram-chat-id", str(chat_id)]
    if created_within_days:
        command.extend(
            [
                "--created-within-days",
                str(created_within_days),
                "--max-pages",
                "4" if created_within_days >= 21 else "3",
                "--max-candidates",
                "260" if created_within_days >= 21 else "180",
            ]
        )
    return command


def scan_state_is_active(scan_state: dict | None = None) -> bool:
    scan_state = scan_state or load_json(SCAN_STATE_PATH, {})
    try:
        started_at = float(scan_state.get("last_scan_started_at") or 0)
        finished_at = float(scan_state.get("last_scan_finished_at") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        started_at
        and finished_at < started_at
        and time.time() - started_at < SCAN_TIMEOUT_SECONDS
        and owner_alert_lock_active()
    )


def run_latest_scan(chat_id: int | str, token: str, created_within_days: int = 0) -> int:
    if not SCAN_LOCK.acquire(blocking=False):
        scan_state = load_json(SCAN_STATE_PATH, {})
        safe_send_message(token, chat_id, scan_running_message(scan_state))
        return 0
    completed: subprocess.CompletedProcess[str] | None = None
    returncode = 1
    scan_state = load_json(SCAN_STATE_PATH, {})
    try:
        recover_stale_scan_state("Recovered scan state because no active scanner process was found.")
        scan_state = load_json(SCAN_STATE_PATH, {})
        if scan_state_is_active(scan_state):
            safe_send_message(token, chat_id, scan_running_message(scan_state))
            return 0
        scan_command = scan_label(created_within_days)
        last_scan_command = scan_state.get("last_scan_command")
        last_scan_started_at = float(scan_state.get("last_scan_started_at") or 0)
        last_scan_finished_at = float(scan_state.get("last_scan_finished_at") or 0)
        last_scan_returncode = scan_state.get("last_scan_returncode")
        if (
            last_scan_command == scan_command
            and last_scan_returncode == 0
            and last_scan_started_at
            and last_scan_finished_at >= last_scan_started_at
            and time.time() - last_scan_started_at < SCAN_COOLDOWN_SECONDS
        ):
            log_event(f"skipped duplicate scan command={scan_command}")
            safe_send_message(token, chat_id, recent_scan_message(created_within_days, last_scan_started_at))
            return 0

        scan_state.update(
            {
                "last_scan_command": scan_command,
                "last_scan_started_at": time.time(),
                "last_scan_finished_at": 0,
                "last_scan_returncode": None,
                "last_scan_error": "",
            }
        )
        save_json(SCAN_STATE_PATH, scan_state)

        intro = (
            f"Scanning OLX for the last {created_within_days} days. This can take up to 30 minutes. You can still send /status while I work."
            if created_within_days
            else "Scanning OLX now. This can take up to 30 minutes. You can still send /status while I work."
        )
        safe_send_message(token, chat_id, intro)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        log_event(f"scan started command={scan_command}")
        try:
            completed = subprocess.run(
                scan_command_args(chat_id, created_within_days),
                cwd=str(ALERT_SCRIPT.parent),
                env=env,
                text=True,
                capture_output=True,
                timeout=SCAN_TIMEOUT_SECONDS,
            )
            returncode = completed.returncode
            append_scan_output(scan_command, completed)
        except subprocess.TimeoutExpired:
            log_event(f"scan timed out command={scan_command}")
            safe_send_message(token, chat_id, f"The {scan_command} scan timed out after 30 minutes. I stopped it so Telegram will not stay stuck.")
            returncode = 124

        scan_state.update(
            {
                "last_scan_finished_at": time.time(),
                "last_scan_returncode": returncode,
                "last_scan_error": "" if returncode == 0 else f"Exit code: {returncode}",
            }
        )
        save_json(SCAN_STATE_PATH, scan_state)
        log_event(f"scan finished command={scan_command} returncode={returncode}")

        if returncode == 0:
            safe_send_message(token, chat_id, f"The {scan_command} scan finished.")
        elif completed is not None:
            output = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
            safe_send_message(token, chat_id, "The scan failed.\n\n" + (output[-3000:] if output else f"Exit code: {returncode}"))
        return returncode
    except Exception as exc:
        log_event(f"scan crashed: {type(exc).__name__}: {exc}")
        log_crash(exc)
        scan_state.update(
            {
                "last_scan_finished_at": time.time(),
                "last_scan_returncode": 125,
                "last_scan_error": f"{type(exc).__name__}: {exc}",
            }
        )
        save_json(SCAN_STATE_PATH, scan_state)
        safe_send_message(token, chat_id, f"The scan stopped because of an error, but Telegram is still alive: {exc}")
        return 125
    finally:
        SCAN_LOCK.release()


def start_scan_thread(chat_id: int | str, token: str, created_within_days: int = 0) -> None:
    thread = threading.Thread(
        target=run_latest_scan,
        args=(chat_id, token, created_within_days),
        daemon=True,
    )
    thread.start()


def maybe_run_daily_schedule(token: str, configured_chat_id: str) -> None:
    data = load_json(DAILY_SCHEDULE_PATH, {})
    if not data.get("enabled"):
        return
    scheduled_time = str(data.get("time") or "")
    today = time.strftime("%Y-%m-%d")
    if scheduled_time != time.strftime("%H:%M") or data.get("last_run_date") == today:
        return
    chat_id = shared_reply_chat_id(configured_chat_ids(configured_chat_id)[0], configured_chat_id) if configured_chat_ids(configured_chat_id) else configured_chat_id
    if not chat_id:
        log_event("daily schedule skipped: no configured chat id")
        return
    if SCAN_LOCK.locked() or scan_state_is_active():
        log_event("daily schedule skipped: scan already running")
        return
    data["last_run_date"] = today
    save_json(DAILY_SCHEDULE_PATH, data)
    safe_send_message(token, chat_id, f"Daily scheduled scan starting now ({scheduled_time}).")
    log_event(f"daily schedule started time={scheduled_time}")
    start_scan_thread(chat_id, token)


def handle_message(message: dict, token: str, configured_chat_id: str) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return

    log_event(f"received chat={chat_id} text={text!r}")
    if not allowed_chat(chat_id, configured_chat_id):
        safe_send_message(
            token,
            chat_id,
            f"This bot is not authorized for this chat yet.\n\nYour chat id is: {chat_id}\nPut it in TELEGRAM_CHAT_ID inside .env.private.",
        )
        return

    command = normalize_command(text)
    log_event(f"handling chat={chat_id} command={command!r}")
    reply_chat_id = shared_reply_chat_id(chat_id, configured_chat_id)
    if command in {"start", "help"}:
        safe_send_message(token, reply_chat_id, HELP_TEXT)
    elif command == "verify":
        safe_send_message(token, reply_chat_id, recovery.verification_link())
    elif command == "resume":
        try:
            arguments = recovery.resume_arguments()
            if arguments:
                recovery.current_store().set("resume_requested", True)
                threading.Thread(target=recovery.recovery_tick, daemon=True).start()
                safe_send_message(token, reply_chat_id, "Resuming your saved scan. Check /status for progress.")
            else:
                safe_send_message(token, reply_chat_id, "No unfinished scan is saved. Send /latest to start a scan.")
        except Exception as error:
            safe_send_message(token, reply_chat_id, str(error))
    elif command in {"ask", "ai"}:
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, ask_ai_message(text))
    elif command == "maybe":
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, maybe_message())
    elif command in {"areas", "report"}:
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, area_report_message())
    elif command == "why":
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, why_message(text))
    elif command == "blacklist":
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, blacklist_message(text))
    elif command in {"contacted", "interested", "notinterested", "wrongnumber"}:
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, lead_status_message(text, command))
    elif command == "leads":
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, leads_message())
    elif command == "schedule":
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, schedule_message(text))
    elif command == "hot":
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, hot_message())
    elif command == "export":
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, export_message())
    elif command == "prices":
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, prices_message())
    elif command == "agencies":
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, agencies_message())
    elif command == "reposts":
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, reposts_message())
    elif command == "learnowner":
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, learn_owner_message(text))
    elif command in {"remind", "reminders"}:
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, reminder_message(text))
    elif command in {"summary", "voice"}:
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, voice_summary_message())
    elif command in {"latest", "search", "scan", "today"}:
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        start_scan_thread(reply_chat_id, token)
    elif command in {"last7", "week"}:
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        start_scan_thread(reply_chat_id, token, 7)
    elif command in {"last14", "twoweeks"}:
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        start_scan_thread(reply_chat_id, token, 14)
    elif command in {"last21", "threeweeks"}:
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        start_scan_thread(reply_chat_id, token, 21)
    elif command == "status":
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, command))
        safe_send_message(token, reply_chat_id, status_message())
    elif looks_like_question(text):
        safe_send_message(token, reply_chat_id, shared_command_notice(chat_id, "ask"))
        safe_send_message(token, reply_chat_id, ask_ai_helper(text))
    else:
        safe_send_message(token, reply_chat_id, f"Shared conversation: chat {chat_id} sent an unknown command. Send /help.")


def main_loop(token: str, configured_chat_id: str) -> int:
    poll_state = load_json(POLL_STATE_PATH, {"offset": None})
    offset = poll_state.get("offset")
    backoff_seconds = NETWORK_BACKOFF_MIN_SECONDS
    print("Telegram command bot is listening. Press Ctrl+C to stop.")
    log_event("listener started")
    save_health(pid=os.getpid(), started_at=now_text(), status="running", retry_after=0)
    recover_stale_scan_state()
    sync_bot_commands(token)

    while True:
        try:
            updates = get_updates(token, offset)
            save_health(last_poll_ok_at=now_text(), last_poll_error="")
            for update in updates:
                update_id = update.get("update_id")
                if update_id is not None:
                    offset = int(update_id) + 1
                    poll_state["offset"] = offset
                    save_json(POLL_STATE_PATH, poll_state)
                message = update.get("message") or update.get("channel_post")
                if message:
                    try:
                        handle_message(message, token, configured_chat_id)
                    except Exception as exc:
                        log_event(f"message handling failed update={update_id}: {type(exc).__name__}: {exc}")
                        log_crash(exc)
            maybe_run_daily_schedule(token, configured_chat_id)
            backoff_seconds = NETWORK_BACKOFF_MIN_SECONDS
        except KeyboardInterrupt:
            print("Stopped.")
            save_health(status="stopped")
            return 0
        except TelegramAPIError as exc:
            save_health(last_poll_error=str(exc), last_poll_error_at=now_text())
            if exc.error_code == 409:
                pause_after_conflict()
                return 3
            if exc.error_code in {401, 404}:
                log_event("telegram auth failed; token must be fixed")
                print("Telegram polling error: unauthorized (check TELEGRAM_BOT_TOKEN).", file=sys.stderr)
                return 2
            log_event(f"telegram api polling error: {exc}")
            print(f"Telegram polling error: {exc}", file=sys.stderr)
            time.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, NETWORK_BACKOFF_MAX_SECONDS)
        except HTTPError as exc:
            save_health(last_poll_error=str(exc), last_poll_error_at=now_text())
            if exc.code == 409:
                pause_after_conflict()
                return 3
            if getattr(exc, "code", None) in {401, 404}:
                log_event("telegram http auth failed; token must be fixed")
                print("Telegram polling error: unauthorized (check TELEGRAM_BOT_TOKEN).", file=sys.stderr)
                return 2
            log_event(f"http polling error: {exc}")
            print(f"Telegram polling error: {exc}", file=sys.stderr)
            time.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, NETWORK_BACKOFF_MAX_SECONDS)
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            save_health(last_poll_error=str(exc), last_poll_error_at=now_text())
            log_event(f"polling transient error: {type(exc).__name__}: {exc}")
            print(f"Telegram polling transient error: {exc}", file=sys.stderr)
            time.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, NETWORK_BACKOFF_MAX_SECONDS)
        except Exception as exc:
            save_health(last_poll_error=str(exc), last_poll_error_at=now_text())
            log_event(f"polling unexpected error: {type(exc).__name__}: {exc}")
            log_crash(exc)
            print(f"Telegram polling unexpected error: {exc}", file=sys.stderr)
            time.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, NETWORK_BACKOFF_MAX_SECONDS)


def main() -> int:
    load_env_files()
    if not active():
        return 0
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    configured_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token:
        print("Set TELEGRAM_BOT_TOKEN in .env.private first.", file=sys.stderr)
        return 2
    try:
        with listener_lock():
            webhook = telegram_api(token, "getWebhookInfo", {}).get("result", {})
            if webhook.get("url"):
                save_health(status="conflict", retry_after=time.time() + CONFLICT_RETRY_SECONDS,
                            last_poll_error="Telegram webhook is configured; polling cannot receive commands.",
                            last_poll_error_at=now_text())
                log_event("Webhook is configured; listener waiting for delivery configuration to be corrected")
                return 3
            recovery_stop = threading.Event()
            threading.Thread(target=recovery.recovery_loop, args=(recovery_stop,), daemon=True).start()
            try:
                return main_loop(token, configured_chat_id)
            finally:
                recovery_stop.set()
    except RuntimeError as exc:
        log_event(str(exc))
        return 0


if __name__ == "__main__":
    while True:
        try:
            raise SystemExit(main())
        except SystemExit:
            raise
        except BaseException as exc:
            log_crash(exc)
            log_event(f"top-level crash recovered: {type(exc).__name__}: {exc}")
            time.sleep(NETWORK_BACKOFF_MAX_SECONDS)

