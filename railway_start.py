#!/usr/bin/env python3
"""
Railway entrypoint for 24/7 hosting.

Runs the iPhone web app as the public Railway web process and keeps the
Telegram command bot alive in a background supervisor.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import web_robot_app
from cloud_runtime import active, enabled, prepare_runtime


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
TELEGRAM_SCRIPT = ROOT / "telegram_command_bot.py"
STOP_EVENT = threading.Event()
CHILDREN: list[subprocess.Popen[str]] = []


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{now_text()} {message}"
    print(line, flush=True)
    with (LOG_DIR / "railway_start.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def terminate_children() -> None:
    STOP_EVENT.set()
    for child in list(CHILDREN):
        if child.poll() is None:
            child.terminate()
    deadline = time.time() + 10
    for child in list(CHILDREN):
        while child.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        if child.poll() is None:
            child.kill()


def handle_shutdown(signum: int, _frame) -> None:
    log(f"shutdown signal received signal={signum}")
    terminate_children()
    raise SystemExit(0)


def telegram_supervisor() -> None:
    if not active():
        log("telegram disabled; cloud service is in staging mode")
        return
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        log("telegram supervisor disabled; TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
        return

    restart_delay_seconds = 15
    while not STOP_EVENT.is_set():
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        log("starting telegram command bot")
        try:
            child = subprocess.Popen(
                [sys.executable, "-u", str(TELEGRAM_SCRIPT)],
                cwd=str(ROOT), env=env, text=True,
            )
        except OSError as exc:
            log(f"telegram could not start: {type(exc).__name__}; retrying")
            STOP_EVENT.wait(restart_delay_seconds)
            continue
        CHILDREN.append(child)
        while child.poll() is None and not STOP_EVENT.is_set():
            time.sleep(2)
        if child in CHILDREN:
            CHILDREN.remove(child)
        if STOP_EVENT.is_set():
            break
        if child.returncode == 3:
            log("telegram delivery conflict; waiting 15 minutes before checking again")
            STOP_EVENT.wait(15 * 60)
            continue
        log(f"telegram command bot exited code={child.returncode}; restarting in {restart_delay_seconds}s")
        STOP_EVENT.wait(restart_delay_seconds)


def runtime_watchdog(workers: list[threading.Thread], display) -> None:
    from urllib.request import urlopen
    failures = 0
    port = os.getenv("WEB_ROBOT_PORT") or os.getenv("PORT") or "8787"
    while not STOP_EVENT.wait(30):
        try:
            with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
                if response.status != 200:
                    raise OSError("web service is not ready")
            if any(not worker.is_alive() for worker in workers):
                raise OSError("a required supervisor stopped")
            if display is not None and display.poll() is not None:
                raise OSError("verification display stopped")
            failures = 0
        except OSError as exc:
            failures += 1
            log(f"runtime health check failed {failures}/3: {type(exc).__name__}")
            if failures >= 3:
                log("runtime unhealthy; requesting a complete service restart")
                os.kill(os.getpid(), signal.SIGTERM)
                return


def main() -> int:
    os.chdir(ROOT)
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    web_robot_app.load_env_files()
    prepare_runtime()
    if active() and (not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID")):
        raise RuntimeError("Active hosting requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    display = None
    if os.name != "nt" and os.getenv("DISPLAY"):
        display = subprocess.Popen(["Xvfb", os.getenv("DISPLAY", ":99"), "-screen", "0", "1024x768x24", "-nolisten", "tcp"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        CHILDREN.append(display)
    workers = []
    if active():
        workers.append(threading.Thread(target=telegram_supervisor, daemon=True))
        if enabled("OWNER_SCAN_ENABLED"):
            workers.append(threading.Thread(target=owner_scan_supervisor, daemon=True))
    for worker in workers:
        worker.start()
    threading.Thread(target=runtime_watchdog, args=(workers, display), daemon=True).start()
    log(f"starting web app on Railway port={os.getenv('PORT', '8787')}")
    try:
        return web_robot_app.main()
    finally:
        terminate_children()


def owner_scan_supervisor() -> None:
    if not active() or not enabled("OWNER_SCAN_ENABLED"):
        return
    while not STOP_EVENT.is_set():
        child = None
        try:
            child = subprocess.Popen([sys.executable, "-u", str(ROOT / "owner_scheduler.py"), "--once"], cwd=ROOT)
            CHILDREN.append(child)
            while child.poll() is None and not STOP_EVENT.wait(2):
                pass
            if STOP_EVENT.is_set() and child.poll() is None:
                child.terminate()
            elif child.returncode:
                log(f"owner scheduler exit={child.returncode}; cooldown saved in data/owner_schedule.json")
        except OSError as exc:
            log(f"owner scheduler could not start: {type(exc).__name__}")
        finally:
            if child in CHILDREN:
                CHILDREN.remove(child)
        STOP_EVENT.wait(300)


if __name__ == "__main__":
    raise SystemExit(main())
