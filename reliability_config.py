from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class ReliabilityConfig:
    enabled: bool = True
    request_interval_min: float = 1.0
    request_interval_max: float = 2.0
    max_in_flight: int = 2
    max_attempts: int = 3
    backoff_base: float = 2.0
    backoff_cap: float = 60.0
    restriction_cooldown: int = 21600
    rate_limit_cooldown: int = 900
    temporary_cooldown: int = 300
    detail_cache_seconds: int = 21600
    verification_idle_seconds: int = 900
    verification_max_seconds: int = 3600
    browser_channel: str = "chrome"
    next_data_selector: str = "script#__NEXT_DATA__"
    search_state_paths: list[str] = field(default_factory=lambda: ["props.pageProps.initialState", "props.initialState"])
    challenge_selectors: list[str] = field(default_factory=list)
    challenge_phrases: list[str] = field(default_factory=lambda: ["verify you are human", "checking your browser", "complete the captcha"])


def load_reliability() -> ReliabilityConfig:
    path = Path(os.getenv("OLX_RELIABILITY_CONFIG", str(ROOT / "reliability.json")))
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    known = {item.name for item in fields(ReliabilityConfig)}
    config = ReliabilityConfig(**{key: value for key, value in raw.items() if key in known})
    if not 0 < config.request_interval_min <= config.request_interval_max:
        raise ValueError("Invalid conservative request interval")
    if not 1 <= config.max_in_flight <= 2 or not 1 <= config.max_attempts <= 3:
        raise ValueError("Request concurrency and attempts exceed conservative limits")
    if min(config.backoff_base, config.backoff_cap, config.restriction_cooldown, config.rate_limit_cooldown, config.temporary_cooldown, config.verification_idle_seconds) <= 0:
        raise ValueError("Recovery timings must be positive")
    return config


def recovery_path() -> Path:
    return Path(os.getenv("OLX_RECOVERY_DB", str(ROOT / "data/recovery.sqlite3")))


def public_url() -> str:
    explicit = os.getenv("RECOVERY_PUBLIC_URL", "").rstrip("/")
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    value = explicit or (f"https://{domain}" if domain else "")
    parsed = urlparse(value)
    if value and (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("RECOVERY_PUBLIC_URL must use HTTPS")
    return value
