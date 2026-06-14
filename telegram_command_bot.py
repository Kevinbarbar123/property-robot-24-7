#!/usr/bin/env python3
"""
Crash-resistant Telegram command listener for on-demand property scans.

Commands:
  /latest  Scan OLX and Facebook Marketplace now and send new likely-owner listings.
  /last7   Scan listings from the last 7 days.
  /last14  Scan listings from the last 14 days.
  /last21  Scan listings from the last 21 days.
  /status  Show bot and scan state.
  /help    Show command help.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.error import HTTPError, URLError

from telegram_owner_alert import (
    STATE_PATH,
    TelegramAPIError,
    load_env_files,
    send_telegram_message,
    telegram_api,
)


POLL_STATE_PATH = Path("data/telegram_command_bot_state.json")
SCAN_STATE_PATH = Path("data/telegram_command_bot_scan_state.json")
HEALTH_PATH = Path("data/telegram_command_bot_health.json")
ALERT_SCRIPT = Path(__file__).with_name("telegram_owner_alert.py")
LOG_PATH = Path("logs/telegram_command_bot.events.log")
SCAN_LOG_PATH = Path("logs/telegram_command_bot.scan.log")
CRASH_LOG_PATH = Path("logs/telegram_command_bot.crash.log")
SCAN_COOLDOWN_SECONDS = 10 * 60
SCAN_TIMEOUT_SECONDS = 30 * 60
POLL_TIMEOUT_SECONDS = 20
NETWORK_BACKOFF_MIN_SECONDS = 5.0
NETWORK_BACKOFF_MAX_SECONDS = 120.0
SCAN_LOCK = threading.Lock()


HELP_TEXT = """Property bot commands:

/latest - scan OLX and Facebook Marketplace now and send new likely-owner listings
/last7 - scan the last 7 days and send new likely-owner listings
/last14 - scan the last 14 days and send new likely-owner listings
/last21 - scan the last 21 days and send new likely-owner listings
/status - show bot state
/help - show this help

If Telegram is flaky, use the web app too: http://192.168.1.6:8787/"""


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


def allowed_chat(chat_id: int | str, configured_chat_id: str) -> bool:
    return bool(configured_chat_id) and str(chat_id) == str(configured_chat_id)


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


def recover_stale_scan_state(reason: str = "Recovered stale scan state after bot restart.") -> None:
    scan_state = load_json(SCAN_STATE_PATH, {})
    started_at = float(scan_state.get("last_scan_started_at") or 0)
    finished_at = float(scan_state.get("last_scan_finished_at") or 0)
    if started_at and finished_at < started_at:
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
    state = load_json(STATE_PATH, {"seen_urls": []})
    scan_state = load_json(SCAN_STATE_PATH, {})
    health = load_json(HEALTH_PATH, {})
    started_at = float(scan_state.get("last_scan_started_at") or 0)
    finished_at = float(scan_state.get("last_scan_finished_at") or 0)
    current_scan = "none"
    if started_at and finished_at < started_at:
        current_scan = scan_running_message(scan_state)
    return "\n".join(
        [
            "Bot is alive.",
            f"Seen listings: {len(state.get('seen_urls', []))}",
            f"Last owner scan: {state.get('last_run_at', 'never')}",
            f"Current scan: {current_scan}",
            f"Last command scan: {scan_state.get('last_scan_command', 'none')}",
            f"Last command result: {scan_state.get('last_scan_returncode', 'none')}",
            f"Last command error: {scan_state.get('last_scan_error', '') or 'none'}",
            f"Last poll OK: {health.get('last_poll_ok_at', 'not yet')}",
            "Web app: http://192.168.1.6:8787/",
        ]
    )


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
                "140" if created_within_days >= 21 else "80",
            ]
        )
    return command


def run_latest_scan(chat_id: int | str, token: str, created_within_days: int = 0) -> int:
    if not SCAN_LOCK.acquire(blocking=False):
        scan_state = load_json(SCAN_STATE_PATH, {})
        safe_send_message(token, chat_id, scan_running_message(scan_state))
        return 0
    completed: subprocess.CompletedProcess[str] | None = None
    returncode = 1
    scan_state = load_json(SCAN_STATE_PATH, {})
    try:
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
            f"Scanning OLX and Facebook Marketplace for the last {created_within_days} days. This can take up to 30 minutes. You can still send /status while I work."
            if created_within_days
            else "Scanning OLX and Facebook Marketplace now. This can take up to 30 minutes. You can still send /status while I work."
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
    if command in {"start", "help"}:
        safe_send_message(token, chat_id, HELP_TEXT)
    elif command in {"latest", "scan", "today"}:
        start_scan_thread(chat_id, token)
    elif command in {"last7", "week"}:
        start_scan_thread(chat_id, token, 7)
    elif command in {"last14", "twoweeks"}:
        start_scan_thread(chat_id, token, 14)
    elif command in {"last21", "threeweeks"}:
        start_scan_thread(chat_id, token, 21)
    elif command == "status":
        safe_send_message(token, chat_id, status_message())
    else:
        safe_send_message(token, chat_id, "Unknown command. Send /help.")


def main_loop(token: str, configured_chat_id: str) -> int:
    poll_state = load_json(POLL_STATE_PATH, {"offset": None})
    offset = poll_state.get("offset")
    backoff_seconds = NETWORK_BACKOFF_MIN_SECONDS
    print("Telegram command bot is listening. Press Ctrl+C to stop.")
    log_event("listener started")
    save_health(pid=os.getpid(), started_at=now_text(), status="running")
    recover_stale_scan_state()

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
            backoff_seconds = NETWORK_BACKOFF_MIN_SECONDS
        except KeyboardInterrupt:
            print("Stopped.")
            save_health(status="stopped")
            return 0
        except TelegramAPIError as exc:
            save_health(last_poll_error=str(exc), last_poll_error_at=now_text())
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
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    configured_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token:
        print("Set TELEGRAM_BOT_TOKEN in .env.private first.", file=sys.stderr)
        return 2
    return main_loop(token, configured_chat_id)


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
