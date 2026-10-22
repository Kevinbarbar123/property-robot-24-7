from __future__ import annotations

import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def enabled(name: str, default: bool = True) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes"}


def active() -> bool:
    return enabled("BOT_ACTIVE", default=not bool(os.getenv("RAILWAY_ENVIRONMENT_ID")))


def website_url() -> str:
    from reliability_config import public_url
    return public_url() or "Open the web app URL printed in the laptop's web service log."


def prepare_runtime(root: Path = ROOT) -> None:
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    if enabled("REQUIRE_PERSISTENT_DATA", False):
        mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
        if not mount or Path(mount).resolve() != data.resolve() or not os.path.ismount(data):
            raise RuntimeError("Attach a persistent Railway volume at /app/data before starting this service.")
    if active() and enabled("REQUIRE_MIGRATED_STATE", False):
        marker = data / "migration-ready.json"
        if not marker.exists():
            raise RuntimeError("Import the existing bot history before setting BOT_ACTIVE=true.")
        migrated = json.loads(marker.read_text(encoding="utf-8"))
        if migrated.get("version") != 1 or not (data / "telegram_owner_alert_state.json").is_file() or not (data / "recovery.sqlite3").is_file():
            raise RuntimeError("Migrated bot history is incomplete; activation refused.")
    if enabled("PERSIST_RUNTIME_DIRS", False):
        for name in ("reports", "logs"):
            source = root / name
            destination = data / name
            destination.mkdir(exist_ok=True)
            if source.is_symlink():
                if source.resolve() != destination.resolve():
                    raise RuntimeError(f"Unexpected {name} link; refusing to replace it.")
                continue
            if source.exists():
                if any(source.iterdir()):
                    raise RuntimeError(f"Move existing {name} into data/{name} before cloud startup.")
                source.rmdir()
            source.symlink_to(destination, target_is_directory=True)

