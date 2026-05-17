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
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        log("telegram supervisor disabled; TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
        return

    restart_delay_seconds = 15
    while not STOP_EVENT.is_set():
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        log("starting telegram command bot")
        child = subprocess.Popen(
            [sys.executable, "-u", str(TELEGRAM_SCRIPT)],
            cwd=str(ROOT),
            env=env,
            text=True,
        )
        CHILDREN.append(child)
        while child.poll() is None and not STOP_EVENT.is_set():
            time.sleep(2)
        if child in CHILDREN:
            CHILDREN.remove(child)
        if STOP_EVENT.is_set():
            break
        log(f"telegram command bot exited code={child.returncode}; restarting in {restart_delay_seconds}s")
        time.sleep(restart_delay_seconds)


def main() -> int:
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=telegram_supervisor, daemon=True).start()
    log(f"starting web app on Railway port={os.getenv('PORT', '8787')}")
    return web_robot_app.main()


if __name__ == "__main__":
    raise SystemExit(main())
