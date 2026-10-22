from __future__ import annotations

import hashlib
import random
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError

import telegram_owner_alert as legacy

from config import BotConfig
from parser import ProListing, from_seller_details
from scorer import score_listing
from challenge_detection import detect_challenge
from reliability_config import load_reliability
from source_client import store, restrict
from source_errors import SourceAccessError


def area_aliases(area: str) -> set[str]:
    normalized = legacy.normalize_for_match(area)
    aliases = {normalized}
    for city, meta in legacy.OLX_TARGET_SEARCHES.items():
        names = [city, *(meta.get("aliases") or [])]
        if normalized in {legacy.normalize_for_match(name) for name in names}:
            aliases.update(legacy.normalize_for_match(name) for name in names)
    if normalized == "metn":
        aliases.add("metn")
    return {alias for alias in aliases if alias}


def is_target_area(details: Any, config: BotConfig) -> bool:
    area_text = legacy.normalize_for_match(
        " ".join([details.listing.city, details.listing.location, details.listing.title, details.description])
    )
    allowed = set()
    for area in config.target_areas:
        allowed.update(area_aliases(area))
    return any(alias and alias in area_text for alias in allowed)


def price_allowed(details: Any, config: BotConfig) -> bool:
    price = details.listing.price_usd or 0
    return config.min_price <= price <= config.max_price


def property_type_allowed(listing: ProListing, config: BotConfig) -> bool:
    return not config.property_types or listing.property_type in set(config.property_types)


def cache_path_for(url: str, cache_dir: Path) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.html"


@contextmanager
def cached_legacy_fetch(config: BotConfig, cache_dir: Path = Path("data/cache/olx")) -> Iterator[None]:
    original_fetch = legacy.fetch_url
    cache_dir.mkdir(parents=True, exist_ok=True)
    ttl_seconds = max(1, int(config.cache_ttl_hours)) * 3600

    def fetch_with_cache(url: str, timeout: int = 30, retries: int = 2, retry_delay: float = 2.0) -> str:
        incident = store().current_incident()
        if incident and (incident["kind"] in {"captcha", "schema_changed"} or incident["retry_at"] > time.time()):
            raise SourceAccessError("OLX session is paused; see /status or /verify.", kind=incident["kind"], url=incident["url"], retry_at=incident["retry_at"])
        path = cache_path_for(url, cache_dir)
        if path.exists() and time.time() - path.stat().st_mtime < ttl_seconds:
            html = path.read_text(encoding="utf-8", errors="replace")
            detection = detect_challenge(200, {}, html)
            if detection:
                restrict(detection, url, 200, load_reliability())
            return html
        delay = random.uniform(config.request_delay_min_seconds, config.request_delay_max_seconds)
        time.sleep(delay)
        html = original_fetch(url, timeout=timeout, retries=retries, retry_delay=retry_delay)
        path.write_text(html, encoding="utf-8")
        return html

    legacy.fetch_url = fetch_with_cache
    try:
        yield
    finally:
        legacy.fetch_url = original_fetch


def collect_raw_details(config: BotConfig) -> list[Any]:
    return legacy.collect_fanar_radius_seed_details(
        max_pages=config.max_pages_per_run,
        delay=config.request_delay_min_seconds,
    )


def enrich_details(details: list[Any], config: BotConfig) -> list[Any]:
    if not details:
        return []
    candidates = details[: max(0, config.max_detail_pages_per_run)]
    try:
        enriched = legacy.enrich_listings(
            [item.listing for item in candidates],
            workers=min(4, max(1, config.max_detail_pages_per_run)),
            detail_delay=config.request_delay_min_seconds,
        )
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        print(f"Detail enrichment skipped after transient error: {exc}")
        return details
    by_url = {item.listing.url: item for item in enriched}
    return [legacy.merge_details(item, by_url[item.listing.url]) if item.listing.url in by_url else item for item in details]


def to_pro_listings(details: list[Any], config: BotConfig) -> list[ProListing]:
    seller_counts: dict[str, int] = {}
    phone_counts: dict[str, int] = {}
    for item in details:
        key = item.seller_id or item.seller_name or item.listing.url
        seller_counts[key] = seller_counts.get(key, 0) + 1
        if item.phone_number:
            phone_counts[item.phone_number] = phone_counts.get(item.phone_number, 0) + 1

    listings: list[ProListing] = []
    for item in details:
        key = item.seller_id or item.seller_name or item.listing.url
        score = score_listing(item, seller_counts.get(key, 1), phone_counts.get(item.phone_number, 0))
        listing = from_seller_details(item, score.owner_score, score.broker_score, score.lead_type)
        listing.raw["owner_signals"] = score.owner_signals
        listing.raw["broker_signals"] = score.broker_signals
        if property_type_allowed(listing, config):
            listings.append(listing)
    return listings


def scrape_olx(config: BotConfig) -> list[ProListing]:
    with cached_legacy_fetch(config):
        raw_details = collect_raw_details(config)
        filtered = [item for item in raw_details if is_target_area(item, config) and price_allowed(item, config)]
        filtered.sort(key=lambda item: legacy.candidate_sort_key(item.listing))
        enriched = enrich_details(filtered, config)
    return to_pro_listings(enriched, config)
