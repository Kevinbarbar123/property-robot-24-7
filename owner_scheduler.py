from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from telegram_owner_alert import install_run_lock, load_env_files
from cloud_runtime import active


ROOT = Path(__file__).resolve().parent
SCHEDULE_PATH = ROOT / "data/owner_schedule.json"


def read_schedule() -> dict:
    try:
        value = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_schedule(state: dict) -> None:
    SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SCHEDULE_PATH.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, SCHEDULE_PATH)


def run_due() -> int:
    os.chdir(ROOT)
    load_env_files()
    if not active():
        return 0
    try:
        install_run_lock(ROOT / "data/owner_schedule.lock")
    except RuntimeError:
        return 0
    state = read_schedule()
    if time.time() < float(state.get("next_run_at", 0)):
        return 0
    interval_hours = max(6.0, float(os.getenv("OWNER_SCAN_INTERVAL_HOURS", "24")))
    state.update(last_started_at=time.time(), status="running", next_run_at=time.time() + 3600)
    write_schedule(state)
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    with (log_dir / "owner_scheduler.log").open("a", encoding="utf-8") as output:
        output.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} independent scan started\n")
        output.flush()
        try:
            completed = subprocess.run(
                [sys.executable, "-u", str(ROOT / "telegram_owner_alert.py")],
                cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT,
                timeout=45 * 60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            code = completed.returncode
        except (subprocess.TimeoutExpired, OSError) as exc:
            output.write(f"Scheduled scan failed: {type(exc).__name__}\n")
            code = 1
    delay = interval_hours * 3600 if code == 0 else (3600 if code == 4 else 6 * 3600)
    state.update(last_finished_at=time.time(), last_exit_code=code,
                 status="ok" if code == 0 else "waiting_retry", next_run_at=time.time() + delay)
    if code == 0:
        state["last_success_at"] = time.time()
    write_schedule(state)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run due owner scans without Codex or an AI API.")
    parser.add_argument("--once", action="store_true", help="Check the persisted schedule once and exit.")
    parser.parse_args()
    raise SystemExit(run_due())
