from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_TARGET_AREAS = [
    "Fanar",
    "Mar Roukoz",
    "Ain Saadeh",
    "Jdeideh",
    "Bsalim",
    "Beit Mery",
    "Beit Merry",
    "Mansourieh",
    "Nabay",
    "Antelias",
    "Qornet Chehwen",
    "Kornet Chehwan",
    "Dlebta",
    "Metn",
]


@dataclass
class BotConfig:
    target_areas: list[str] = field(default_factory=lambda: list(DEFAULT_TARGET_AREAS))
    property_types: list[str] = field(default_factory=lambda: ["apartment_sale", "apartment_rent"])
    min_price: int = 1
    max_price: int = 10_000_000
    owner_score_threshold: int = 70
    scrape_interval_minutes: int = 60
    max_pages_per_run: int = 3
    max_detail_pages_per_run: int = 160
    request_delay_min_seconds: float = 0.8
    request_delay_max_seconds: float = 2.2
    cache_ttl_hours: int = 6
    database_path: Path = Path("data/pro_owner_leads.sqlite")
    csv_export_path: Path = Path("reports/pro_owner_leads.csv")
    json_export_path: Path = Path("reports/pro_owner_leads.json")
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


def load_env_files() -> None:
    for path, override in ((Path(".env"), False), (Path(".env.private"), True)):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if override:
                os.environ[key] = value
            else:
                os.environ.setdefault(key, value)


def expand_env(value: str) -> str:
    match = re.fullmatch(r"\$\{([A-Z0-9_]+)\}", value.strip())
    if match:
        return os.getenv(match.group(1), "")
    return value


def parse_scalar(value: str) -> Any:
    value = expand_env(value.strip())
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value.strip('"').strip("'")


def load_simple_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data: dict[str, Any] = {}
    current_key = ""
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("  - ") and current_key:
            data.setdefault(current_key, []).append(parse_scalar(line[4:]))
            continue
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            current_key = key.strip()
            if value.strip():
                data[current_key] = parse_scalar(value.strip())
            else:
                data[current_key] = []
    return data


def load_config(path: str | Path = "config.yaml") -> BotConfig:
    load_env_files()
    raw = load_simple_yaml(Path(path))
    config = BotConfig()
    for key, value in raw.items():
        if not hasattr(config, key):
            continue
        if key.endswith("_path"):
            setattr(config, key, Path(str(value)))
        else:
            setattr(config, key, value)
    config.telegram_bot_token = config.telegram_bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
    config.telegram_chat_id = config.telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
    return config
