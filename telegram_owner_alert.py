#!/usr/bin/env python3
"""
Daily Telegram alerts for new, owner-like OLX apartment listings.

Telegram setup uses Bot API environment variables:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import argparse
import atexit
import http.client
import json
import math
import os
import re
import socket
import ssl
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from property_bot import (
    BASE_URL,
    Listing,
    extract_olx_search_hits,
    fetch_url,
    is_apartment_candidate,
    money,
    normalize_for_match,
    normalize_text,
    parse_page_count,
)


STATE_PATH = Path("data/telegram_owner_alert_state.json")
LOCK_PATH = Path("data/telegram_owner_alert.lock")
OUTBOX_PATH = Path("data/telegram_outbox.json")
CRASH_LOG_PATH = Path("logs/telegram_owner_alert.crash.log")
LOG_DIR = Path("reports")
LOCK_STALE_SECONDS = 2 * 60 * 60
DEFAULT_MAX_PAGES = 3
DEFAULT_OWNER_POST_LIMIT = 2
DEFAULT_OWNER_SCORE_THRESHOLD = 4
DEFAULT_MIN_PRICE = 0
DEFAULT_DETAIL_WORKERS = 6
DEFAULT_MAX_CANDIDATES = 120
DEFAULT_DETAIL_SCORE_LIMIT = 12
DEFAULT_HISTORICAL_OWNER_SCORE_THRESHOLD = 5
FANAR_CENTER_LAT = 33.877799
FANAR_CENTER_LNG = 35.577951
DEFAULT_RADIUS_KM = 15.0
TARGET_AREA_LABEL = "the selected Metn target areas"
TELEGRAM_API_HOST = "api.telegram.org"
DEFAULT_TELEGRAM_CONNECT_HOSTS = (
    "149.154.167.99",
    "149.154.175.50",
    "149.154.175.100",
    "91.108.56.130",
    "91.108.56.170",
    "91.108.4.200",
)


class TelegramAPIError(Exception):
    def __init__(self, method: str, error_code: int | None, description: str) -> None:
        self.method = method
        self.error_code = error_code
        self.description = description
        super().__init__(f"Telegram API error for {method}: {error_code or 'unknown'} {description}".strip())


def log_crash(exc: BaseException) -> None:
    try:
        CRASH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CRASH_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} {type(exc).__name__}: {exc} ---\n")
            handle.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except OSError:
        pass


OLX_TARGET_SEARCHES = {
    "Fanar": {
        "query": "fanar",
        "location": "fanar",
        "aliases": ["fanar"],
    },
    "Mar Roukoz": {
        "query": "mar-roukoz",
        "location": "mar_roukoz",
        "aliases": ["mar roukoz", "mar roukos", "mar-roukoz", "mar-roukos"],
    },
    "Broumana": {
        "query": "broumana",
        "location": "broummana",
        "aliases": ["broumana", "broummana", "brummana", "broumana metn"],
    },
    "Beit Mery": {
        "query": "beit-mery",
        "location": "beit-meri",
        "aliases": ["beit mery", "beit merry", "beit-mery"],
    },
    "Jdeideh": {
        "query": "jdeideh",
        "location": "jdaide",
        "aliases": ["jdeideh", "jdeide", "jdaide", "jdeidet el metn", "jdeidet"],
    },
    "Rawda": {
        "query": "rawda",
        "location": "new-rawda",
        "aliases": ["rawda", "new rawda", "rawda metn"],
    },
    "Bsalim": {
        "query": "bsalim",
        "location": "bsalim",
        "aliases": ["bsalim", "bsaleem", "bsalim metn"],
    },
    "Mezher": {
        "query": "mezher",
        "location": "mezher",
        "aliases": ["mezher", "mazher", "mezher metn"],
    },
    "Biakout": {
        "query": "biakout",
        "location": "biaqout",
        "aliases": ["biakout", "biaqout", "biyakout", "biakout metn"],
    },
    "Sabtieh": {
        "query": "sabtieh",
        "location": "sabtieh",
        "aliases": ["sabtieh", "sabtaieh", "sabteih", "sabtaieh metn"],
    },
    "Dekwaneh": {
        "query": "dekwaneh",
        "location": "dekwaneh",
        "aliases": ["dekwaneh", "dekouaneh", "dekouane"],
    },
    "Mkalles": {
        "query": "mkalles",
        "location": "mkalles",
        "aliases": ["mkalles", "mekalles", "mkaless", "mkalles metn"],
    },
    "Sin El Fil": {
        "query": "sin-el-fil",
        "location": "sin-el-fil",
        "aliases": ["sin el fil", "sin-el-fil", "sinelfil", "sin el fill"],
    },
    "Jisr El Bacha": {
        "query": "jisr-el-bacha",
        "location": "jisr-el-bacha",
        "aliases": ["jisr el bacha", "jisr-el-bacha", "jesr el bacha"],
    },
    "Horsh Tabet": {
        "query": "horch-tabet",
        "location": "horsh-tabet",
        "aliases": ["horsh tabet", "horch tabet", "horch-tabet", "horsh-tabet"],
    },
    "Baouchrieh": {
        "query": "baouchrieh",
        "location": "baouchriye",
        "aliases": ["baouchrieh", "bauchrieh", "baouchriyeh", "sad el baouchrieh", "sed el baouchrieh"],
    },
    "Rabweh": {
        "query": "rabweh",
        "location": "rabweh",
        "aliases": ["rabweh", "rabieh", "rabieh metn"],
    },
    "Zalka": {
        "query": "zalka",
        "location": "zalqa",
        "aliases": ["zalka"],
    },
    "Jal El Dib": {
        "query": "jal-el-dib",
        "location": "jall-el-dieb",
        "aliases": ["jal el dib", "jall el dib", "jal-el-dib", "jall-el-dib"],
    },
    "Antelias": {
        "query": "antelias",
        "location": "antilias",
        "aliases": ["antelias"],
    },
    "Dbayeh": {
        "query": "dbayeh-metn",
        "location": "dbaye",
        "aliases": ["dbayeh", "dbaye", "d bayeh"],
    },
    "Nahr El Mott": {
        "query": "nahr-el-mott",
        "location": "nahr-el-mott",
        "aliases": ["nahr el mott", "nahr el mot", "nahr-el-mott", "nahr-el-mot"],
    },
    "Kornet Chehwan": {
        "query": "kornet-chehwan",
        "location": "qornet_chahouane",
        "aliases": ["kornet chehwan", "cornet chehwan", "qornet chehwan", "kornet-chehwan"],
    },
    "Ain Saadeh": {
        "query": "ain-saadeh",
        "location": "ain_saadeh",
        "aliases": ["ain saadeh", "ain saade", "ain-saadeh", "ain-saade"],
    },
    "Mansourieh": {
        "query": "mansourieh",
        "location": "mansouriyeh",
        "aliases": ["mansourieh", "mansourieh metn", "mansouriyeh"],
    },
    "Monteverde": {
        "query": "monteverde",
        "location": "monteverde",
        "aliases": ["monteverde", "monte verdi"],
    },
    "Roumieh": {
        "query": "roumieh",
        "location": "roumieh",
        "aliases": ["roumieh", "roumie"],
    },
    "Tilal Ain Saadeh": {
        "query": "tilal-ain-saade",
        "location": "tilal-ain-saade",
        "aliases": ["tilal ain saadeh", "tilal ain saade", "tilal-ain-saadeh", "tilal-ain-saade"],
    },
    "Ain Najem": {
        "query": "ain-najm",
        "location": "ain-najm",
        "aliases": ["ain najm", "ain najem", "ain-najm", "ain-najem"],
    },
}

OLX_PURPOSES = {
    "sale": "apartments-villas-for-sale",
    "rent": "apartments-villas-for-rent",
}

TARGET_CITY_ALIASES = {
    normalize_for_match(alias): city
    for city, config in OLX_TARGET_SEARCHES.items()
    for alias in [city, *config["aliases"]]
}

AGENCY_TERMS = (
    "agency",
    "agent",
    "broker",
    "real estate",
    "properties",
    "property",
    "group",
    "development",
    "developers",
    "company",
    "s.a.r.l",
    "sarl",
    "estate",
    "consultancy",
    "consultants",
    "c-properties",
    "confidence",
    "golden mark",
    "golden land",
    "balance real estate",
    "escwa",
    "yas real estate",
)

AGENCY_CODE_PREFIXES = (
    "gmc",
    "cprc",
    "cpcc",
    "cpes",
    "cpsk",
    "rgms",
    "nkp",
    "nk",
    "dy",
    "mqoa",
    "sgrm",
    "dpea",
    "spm",
    "rw",
    "rwr",
)

ARABIC_DIGIT_TRANSLATION = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


@dataclass
class SellerDetails:
    listing: Listing
    seller_id: str = ""
    seller_name: str = ""
    phone_number: str = ""
    account_ads_count: int | None = None
    seller_type: str = ""
    agency_name: str = ""
    agency_id: str = ""
    agent_code: str = ""
    ownership: str = ""
    purpose: str = ""
    lat: float | None = None
    lng: float | None = None
    distance_km: float | None = None
    description: str = ""
    owner_score: int = 0
    exclusion_reasons: tuple[str, ...] = ()
    from_search_page: bool = False


def extract_json_string(raw_html: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}":"((?:\\.|[^"\\])*)"', raw_html)
    if not match:
        return ""
    try:
        return json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return normalize_text(match.group(1))


def extract_json_number_as_string(raw_html: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}":(\d+)', raw_html)
    return match.group(1) if match else ""


def extract_json_int(raw_html: str, key: str) -> int | None:
    match = re.search(rf'"{re.escape(key)}":(\d+)', raw_html)
    return int(match.group(1)) if match else None


def extract_json_float(raw_html: str, key: str) -> float | None:
    match = re.search(rf'"{re.escape(key)}":(-?\d+(?:\.\d+)?)', raw_html)
    return float(match.group(1)) if match else None


def extract_balanced_json_object(text: str, start: int) -> str:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def extract_search_result_objects(raw_html: str) -> list[dict]:
    next_data_hits = extract_olx_search_hits(raw_html)
    if next_data_hits:
        return next_data_hits

    objects: list[dict] = []
    seen_ids: set[str] = set()
    position = 0
    while True:
        start = raw_html.find('{"_score":', position)
        if start == -1:
            break
        position = start + 1
        raw_object = extract_balanced_json_object(raw_html, start)
        if not raw_object:
            continue
        try:
            item = json.loads(raw_object)
        except json.JSONDecodeError:
            continue
        external_id = str(item.get("externalID") or "")
        if not external_id or external_id in seen_ids:
            continue
        seen_ids.add(external_id)
        objects.append(item)
    return objects


def get_nested_string(data: dict, *keys: str) -> str:
    value = data
    for key in keys:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return normalize_text(str(value)) if value is not None else ""


def get_nested_int(data: dict, *keys: str) -> int | None:
    value = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if value in {None, ""}:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def created_label_from_timestamp(timestamp: int | float | None) -> str:
    if not timestamp:
        return ""
    age = datetime.now() - datetime.fromtimestamp(float(timestamp))
    if age.total_seconds() < 0:
        return "just now"
    if age < timedelta(minutes=1):
        return "just now"
    if age < timedelta(hours=1):
        minutes = max(1, int(age.total_seconds() // 60))
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if age < timedelta(days=1):
        hours = max(1, int(age.total_seconds() // 3600))
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if age < timedelta(days=2):
        return "yesterday"
    days = int(age.days)
    if days < 14:
        return f"{days} days ago"
    weeks = max(2, round(days / 7))
    return f"{weeks} weeks ago"


def olx_listing_url(item: dict) -> str:
    external_id = get_nested_string(item, "externalID")
    slug = get_nested_string(item, "slug")
    if not external_id or not slug:
        return ""
    return f"{BASE_URL}/ad/{quote(slug, safe='-')}-ID{external_id}.html"


def item_location_text(item: dict) -> str:
    locations = item.get("location") or item.get("locationTranslations") or []
    names: list[str] = []
    if isinstance(locations, list):
        for location in locations:
            if not isinstance(location, dict):
                continue
            if "en" in location and isinstance(location["en"], dict):
                name = location["en"].get("name")
            else:
                name = location.get("name")
            if name and name not in {"Lebanon"}:
                names.append(normalize_text(str(name)))
    return ", ".join(reversed(names))


def item_location_names(item: dict) -> list[str]:
    locations = item.get("location") or item.get("locationTranslations") or []
    names: list[str] = []
    if isinstance(locations, list):
        for location in locations:
            if not isinstance(location, dict):
                continue
            if "en" in location and isinstance(location["en"], dict):
                name = location["en"].get("name")
            else:
                name = location.get("name")
            if name:
                names.append(normalize_text(str(name)))
    return names


def canonical_target_city(value: str) -> str:
    normalized = normalize_for_match(value)
    return TARGET_CITY_ALIASES.get(normalized, "")


def matches_target_area(item: dict, fallback_city: str) -> tuple[bool, str]:
    names = item_location_names(item)
    for name in reversed(names):
        city = canonical_target_city(name)
        if city:
            return True, city

    config = OLX_TARGET_SEARCHES[fallback_city]
    text = normalize_for_match(
        " ".join(
            [
                get_nested_string(item, "title"),
                get_nested_string(item, "description"),
                item_location_text(item),
            ]
        )
    )
    if any(normalize_for_match(alias) in text for alias in config["aliases"]):
        return True, fallback_city
    return False, ""


def search_item_to_details(item: dict, fallback_city: str, purpose: str) -> SellerDetails | None:
    title = get_nested_string(item, "title")
    url = olx_listing_url(item)
    if not title or not url:
        return None
    target_match, city = matches_target_area(item, fallback_city)
    if not target_match:
        return None

    extra_fields = item.get("extraFields") if isinstance(item.get("extraFields"), dict) else {}
    lat = extract_float_from_dict(item.get("geography"), "lat")
    lng = extract_float_from_dict(item.get("geography"), "lng")
    if lat is None or lng is None:
        geo_point = item.get("geo_point")
        if isinstance(geo_point, list) and len(geo_point) >= 2:
            lng = safe_float(geo_point[0])
            lat = safe_float(geo_point[1])

    distance_km = None
    if lat is not None and lng is not None:
        distance_km = haversine_km(FANAR_CENTER_LAT, FANAR_CENTER_LNG, lat, lng)

    seller_type = normalize_text(str(extra_fields.get("seller_type") or ""))
    agency = item.get("agency") if isinstance(item.get("agency"), dict) else {}
    contact_info = item.get("contactInfo") if isinstance(item.get("contactInfo"), dict) else {}
    description = get_nested_string(item, "description")
    phone_number = extract_phone_number_from_search_item(item, description)
    listing = Listing(
        city=city,
        title=title,
        price_usd=get_nested_int(item, "extraFields", "price") or get_nested_int(item, "price"),
        sqm=get_nested_int(item, "extraFields", "ft"),
        location=item_location_text(item),
        created=created_label_from_timestamp(item.get("timestamp") or item.get("createdAt")),
        url=url,
        source=f"OLX Lebanon {purpose}",
    )
    return SellerDetails(
        listing=listing,
        seller_id=get_nested_string(item, "userExternalID"),
        seller_name=get_nested_string(agency, "name") or normalize_text(str(contact_info.get("name") or "")),
        phone_number=phone_number,
        seller_type=seller_type,
        agency_name=get_nested_string(agency, "name"),
        agency_id=get_nested_string(agency, "id") or get_nested_string(agency, "externalID"),
        agent_code=get_nested_string(extra_fields, "reference_id"),
        ownership=get_nested_string(extra_fields, "ownership"),
        purpose="rent" if purpose == "rent" or item.get("purpose") == "for-rent" else "sale",
        lat=lat,
        lng=lng,
        distance_km=distance_km,
        description=description,
        from_search_page=True,
    )


def safe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_float_from_dict(value: object, key: str) -> float | None:
    if not isinstance(value, dict):
        return None
    return safe_float(value.get(key))


def extract_phone_number_from_search_item(item: dict, description: str) -> str:
    description_phone = extract_phone_number_from_description(description)
    if description_phone:
        return description_phone
    nested_phone = extract_phone_number_from_nested_data(item)
    if nested_phone:
        return nested_phone
    for agent in item.get("agents") or []:
        if isinstance(agent, dict):
            phone = normalize_phone_number(str(agent.get("phoneNumber") or ""))
            if phone:
                return phone
    return ""


def extract_phone_number_from_nested_data(value: object, depth: int = 0) -> str:
    if depth > 6:
        return ""
    if isinstance(value, dict):
        phone_keys = (
            "phoneNumber",
            "phone_number",
            "phone",
            "mobile",
            "mobileNumber",
            "contactNumber",
            "contactPhone",
            "whatsapp",
            "whatsappNumber",
        )
        for key in phone_keys:
            if key in value:
                phone = normalize_phone_number(str(value.get(key) or ""))
                if phone:
                    return phone
        for child in value.values():
            phone = extract_phone_number_from_nested_data(child, depth + 1)
            if phone:
                return phone
    elif isinstance(value, list):
        for child in value[:30]:
            phone = extract_phone_number_from_nested_data(child, depth + 1)
            if phone:
                return phone
    return ""


def listing_external_id_from_url(url: str) -> str:
    match = re.search(r"-ID(\d+)\.html(?:$|\?)", url)
    return match.group(1) if match else ""


def find_current_listing_item(raw_html: str, listing: Listing) -> dict:
    external_id = listing_external_id_from_url(listing.url)
    if not external_id:
        return {}
    for item in extract_search_result_objects(raw_html):
        if str(item.get("externalID") or "") == external_id:
            return item
    return {}


def extract_page_state_object(raw_html: str, key: str) -> dict:
    marker = f'"{key}":{{'
    index = raw_html.find(marker)
    if index == -1:
        return {}
    start = index + len(f'"{key}":')
    raw_object = extract_balanced_json_object(raw_html, start)
    if not raw_object:
        return {}
    try:
        value = json.loads(raw_object)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def extract_current_seller_profile(raw_html: str, listing: Listing) -> dict:
    external_id = listing_external_id_from_url(listing.url)
    seller_profile = extract_page_state_object(raw_html, "sellerProfile")
    params = seller_profile.get("params") if isinstance(seller_profile.get("params"), dict) else {}
    data = seller_profile.get("data") if isinstance(seller_profile.get("data"), dict) else {}
    if external_id and str(params.get("adExternalID") or "") == external_id:
        return data
    return {}


def extract_description(raw_html: str) -> str:
    description = extract_json_string(raw_html, "description")
    if description:
        return description
    match = re.search(r'<meta name="description" content="([^"]*)"', raw_html)
    return normalize_text(match.group(1)) if match else ""


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def extract_geography(raw_html: str) -> tuple[float | None, float | None]:
    geography_match = re.search(r'"geography":\{"lat":(-?\d+(?:\.\d+)?),"lng":(-?\d+(?:\.\d+)?)\}', raw_html)
    if geography_match:
        return float(geography_match.group(1)), float(geography_match.group(2))

    geo_point_match = re.search(r'"geo_point":\[(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)\]', raw_html)
    if geo_point_match:
        # OLX stores geo_point as [lng, lat].
        return float(geo_point_match.group(2)), float(geo_point_match.group(1))
    return None, None


def infer_purpose(listing: Listing) -> str:
    source = listing.source.lower()
    if "rent" in source:
        return "rent"
    if "sale" in source:
        return "sale"
    return "unknown"


def normalize_phone_number(value: str) -> str:
    value = value.translate(ARABIC_DIGIT_TRANSLATION)
    digits = re.sub(r"\D", "", value)
    if not digits:
        return ""
    if digits.startswith("00961"):
        digits = digits[2:]
    if digits.startswith("961") and len(digits) >= 10:
        return f"+{digits}"
    if digits.startswith("0") and len(digits) >= 8:
        return f"+961{digits[1:]}"
    if len(digits) == 7 and digits.startswith("3"):
        return f"+961{digits}"
    if len(digits) == 8 and digits[:2] in {"03", "70", "71", "76", "78", "79", "81"}:
        return f"+961{digits[1:] if digits.startswith('0') else digits}"
    return ""


def extract_phone_number_from_description(description: str) -> str:
    description = description.translate(ARABIC_DIGIT_TRANSLATION)
    phone_patterns = [
        r"(?:\+|00)?961[\s.-]*(?:3|70|71|76|78|79|81)[\s.-]*\d{3}[\s.-]*\d{3}",
        r"\b0?3[\s.-]*\d{3}[\s.-]*\d{3}\b",
        r"\b(?:70|71|76|78|79|81)[\s.-]*\d{3}[\s.-]*\d{3}\b",
    ]
    for pattern in phone_patterns:
        match = re.search(pattern, description)
        if match:
            phone = normalize_phone_number(match.group(0))
            if phone:
                return phone
    return ""


def extract_phone_number(raw_html: str, description: str) -> str:
    # Keep the old public helper name, but do not read phoneNumber globally from raw_html.
    # OLX embeds unrelated agent objects on detail pages, which can attach a wrong phone.
    return extract_phone_number_from_description(description)


def enrich_listing(listing: Listing) -> SellerDetails:
    try:
        raw_html = fetch_url(listing.url, timeout=12, retries=0)
    except (HTTPError, URLError, TimeoutError) as exc:
        return SellerDetails(listing=listing, exclusion_reasons=(f"detail fetch failed: {exc}",))

    current_item = find_current_listing_item(raw_html, listing)
    seller_profile = extract_current_seller_profile(raw_html, listing)
    extra_fields = current_item.get("extraFields") if isinstance(current_item.get("extraFields"), dict) else {}
    agency = current_item.get("agency") if isinstance(current_item.get("agency"), dict) else {}
    contact_info = current_item.get("contactInfo") if isinstance(current_item.get("contactInfo"), dict) else {}

    description = get_nested_string(current_item, "description") if current_item else extract_description(raw_html)
    seller_name = (
        normalize_text(str(seller_profile.get("name") or ""))
        or
        get_nested_string(agency, "name")
        or normalize_text(str(contact_info.get("name") or ""))
        or extract_contact_name(raw_html)
    )
    lat = extract_float_from_dict(current_item.get("geography"), "lat") if current_item else None
    lng = extract_float_from_dict(current_item.get("geography"), "lng") if current_item else None
    if lat is None or lng is None:
        lat, lng = extract_geography(raw_html)
    distance_km = None
    if lat is not None and lng is not None:
        distance_km = haversine_km(FANAR_CENTER_LAT, FANAR_CENTER_LNG, lat, lng)
    profile_phone = extract_phone_number_from_nested_data(seller_profile)
    description_phone = extract_phone_number_from_description(description)
    listing_phone = extract_phone_number_from_search_item(current_item, description) if current_item else ""
    phone_number = description_phone or profile_phone or listing_phone
    return SellerDetails(
        listing=listing,
        seller_id=normalize_text(str(seller_profile.get("externalID") or ""))
        or get_nested_string(current_item, "userExternalID")
        or extract_json_string(raw_html, "seller_id"),
        seller_name=seller_name,
        phone_number=phone_number,
        account_ads_count=safe_int(seller_profile.get("adsCount")) if seller_profile else extract_json_int(raw_html, "adsCount"),
        seller_type=get_nested_string(extra_fields, "seller_type") or extract_json_string(raw_html, "seller_type"),
        agency_name=get_nested_string(agency, "name"),
        agency_id=get_nested_string(agency, "id") or get_nested_string(agency, "externalID"),
        agent_code=get_nested_string(extra_fields, "reference_id") or extract_json_string(raw_html, "agent_code"),
        ownership=get_nested_string(extra_fields, "ownership") or extract_json_string(raw_html, "ownership"),
        purpose=infer_purpose(listing),
        lat=lat,
        lng=lng,
        distance_km=distance_km,
        description=description,
    )


def extract_contact_name(raw_html: str) -> str:
    match = re.search(r'"contactInfo":\{"roles":\[[^\]]*\],"name":"((?:\\.|[^"\\])*)"', raw_html)
    if not match:
        return ""
    try:
        return json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return normalize_text(match.group(1))


def agency_text_found(*values: str) -> bool:
    text = " ".join(value for value in values if value).lower()
    return any(term in text for term in AGENCY_TERMS)


def looks_like_reference_code(details: SellerDetails) -> bool:
    text = f"{details.listing.title} {details.description}"
    return bool(re.search(r"\b(?:ref|reference|code)\s*#?\s*[a-z]{1,4}\d{2,}\b", text, re.I))


def looks_like_reference_code_text(text: str) -> bool:
    return bool(re.search(r"\b(?:ref|reference|code)\s*#?\s*[a-z]{1,4}\d{2,}\b", text, re.I))


def preliminary_rejection_reasons(listing: Listing) -> tuple[str, ...]:
    reasons: list[str] = []
    if not is_apartment_candidate(listing):
        reasons.append("not apartment-like")
    if looks_like_reference_code_text(listing.title):
        reasons.append("real-estate reference code found in title")
    if re.search(r"\b(?:gmc|cprc|cpcc|cpes|cpsk|rgms|nkp|nk|dy|mqoa|sgrm|dpea|spm|rw|rwr)\w*\d+\w*\b", listing.title, re.I):
        reasons.append("broker-style code found in title")
    if agency_text_found(listing.title):
        reasons.append("agency keyword found in title")
    return tuple(reasons)


def candidate_sort_key(listing: Listing) -> tuple[int, float, int]:
    age = listing_age(listing.created)
    age_hours = age.total_seconds() / 3600 if age is not None else 99999
    title_penalty = 0
    if looks_like_reference_code_text(listing.title):
        title_penalty += 10
    if agency_text_found(listing.title):
        title_penalty += 10
    if not is_apartment_candidate(listing):
        title_penalty += 100
    return (title_penalty, age_hours, listing.price_usd or 0)


def agency_like_agent_code(value: str) -> bool:
    code = re.sub(r"[^a-z0-9]", "", value.lower())
    if not code or code == "n":
        return False
    return any(code.startswith(prefix) and any(char.isdigit() for char in code) for prefix in AGENCY_CODE_PREFIXES)


def score_owner_likelihood(
    details: SellerDetails,
    seller_post_count: int,
    phone_post_count: int = 0,
) -> tuple[int, tuple[str, ...]]:
    score = 0
    reasons: list[str] = []
    seller_type = details.seller_type.lower()

    if not is_apartment_candidate(details.listing):
        score += 100
        reasons.append("not apartment-like")
    if details.distance_km is None:
        score += 7
        reasons.append("missing coordinates")
    elif details.distance_km > DEFAULT_RADIUS_KM:
        score += 100
        reasons.append(f"{details.distance_km:.1f}km from Fanar")

    if seller_post_count > 3:
        score += 8
        reasons.append(f"seller has {seller_post_count} apartment posts in target areas")
    elif seller_post_count == 3:
        score += 1
        reasons.append("seller has 3 apartment posts in target areas")

    if phone_post_count > 3:
        score += 8
        reasons.append(f"phone appears on {phone_post_count} apartment posts in target areas")

    if details.agency_name or details.agency_id:
        score += 6
        reasons.append("agency profile present")
    if details.agent_code:
        if agency_like_agent_code(details.agent_code):
            score += 6
            reasons.append("agency-style reference code present")
        small_private_account = (
            not details.agency_name
            and not details.agency_id
            and seller_type not in {"2", "business", "agency"}
            and seller_post_count <= 3
        )
        score += 2 if small_private_account else 5
        reasons.append("agent/reference code present")
    if agency_text_found(details.seller_name, details.listing.title, details.description):
        score += 4
        reasons.append("agency keyword found")
    if looks_like_reference_code(details):
        score += 3
        reasons.append("real-estate reference code found")
    if seller_type in {"business", "agency", "2"}:
        score += 1
        reasons.append(f"declared seller type is {details.seller_type}")

    positive_text = f"{details.seller_name} {details.listing.title} {details.description}".lower()
    if any(term in positive_text for term in ["owner", "direct owner", "by owner", "private owner"]):
        score -= 2
        reasons.append("owner wording found")
    if details.phone_number:
        score -= 1
        reasons.append("phone exposed")

    return score, tuple(reasons)


def likely_owner(
    details: SellerDetails,
    seller_post_count: int,
    owner_score_threshold: int,
    phone_post_count: int = 0,
) -> tuple[bool, tuple[str, ...]]:
    score, reasons = score_owner_likelihood(details, seller_post_count, phone_post_count)
    details.owner_score = score
    return score <= owner_score_threshold, reasons


def merge_details(search_details: SellerDetails, fetched_details: SellerDetails) -> SellerDetails:
    if fetched_details.exclusion_reasons and not fetched_details.seller_name:
        search_details.exclusion_reasons = fetched_details.exclusion_reasons
        return search_details

    return SellerDetails(
        listing=search_details.listing,
        seller_id=fetched_details.seller_id or search_details.seller_id,
        seller_name=fetched_details.seller_name or search_details.seller_name,
        phone_number=fetched_details.phone_number or search_details.phone_number,
        account_ads_count=fetched_details.account_ads_count,
        seller_type=fetched_details.seller_type or search_details.seller_type,
        agency_name=fetched_details.agency_name or search_details.agency_name,
        agency_id=fetched_details.agency_id or search_details.agency_id,
        agent_code=fetched_details.agent_code or search_details.agent_code,
        ownership=fetched_details.ownership or search_details.ownership,
        purpose=fetched_details.purpose or search_details.purpose,
        lat=fetched_details.lat if fetched_details.lat is not None else search_details.lat,
        lng=fetched_details.lng if fetched_details.lng is not None else search_details.lng,
        distance_km=(
            fetched_details.distance_km
            if fetched_details.distance_km is not None
            else search_details.distance_km
        ),
        description=fetched_details.description or search_details.description,
        owner_score=search_details.owner_score,
        exclusion_reasons=search_details.exclusion_reasons,
        from_search_page=True,
    )


def enrich_listings(listings: list[Listing], workers: int, detail_delay: float) -> list[SellerDetails]:
    if not listings:
        return []
    workers = max(1, workers)
    if workers == 1:
        details = []
        for index, listing in enumerate(listings, start=1):
            print(f"Inspecting seller {index}/{len(listings)}: {listing.url}", file=sys.stderr)
            details.append(enrich_listing(listing))
            time.sleep(detail_delay)
        return details

    details_list: list[SellerDetails] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(enrich_listing, listing): listing for listing in listings}
        for index, future in enumerate(as_completed(futures), start=1):
            listing = futures[future]
            print(f"Inspected seller {index}/{len(listings)}: {listing.url}", file=sys.stderr)
            try:
                details_list.append(future.result())
            except Exception as exc:
                details_list.append(SellerDetails(listing=listing, exclusion_reasons=(f"detail fetch failed: {exc}",)))
            if detail_delay:
                time.sleep(detail_delay)
    return details_list


def balanced_detail_candidates(details_list: list[SellerDetails], limit: int) -> list[SellerDetails]:
    if not limit or len(details_list) <= limit:
        return details_list

    groups: dict[tuple[str, str], list[SellerDetails]] = {}
    for details in details_list:
        key = (details.purpose or infer_purpose(details.listing), details.listing.city)
        groups.setdefault(key, []).append(details)
    for group in groups.values():
        group.sort(key=lambda details: candidate_sort_key(details.listing))

    selected: list[SellerDetails] = []
    while len(selected) < limit and groups:
        for key in list(groups):
            group = groups[key]
            if not group:
                del groups[key]
                continue
            selected.append(group.pop(0))
            if len(selected) >= limit:
                break
    return selected


def olx_search_url(category_slug: str, query_slug: str, page: int, location_slug: str = "") -> str:
    if location_slug:
        url = f"{BASE_URL}/properties/{category_slug}/{location_slug}/"
    else:
        url = f"{BASE_URL}/properties/{category_slug}/q-apartments-{query_slug}/"
    if page > 1:
        url = f"{url}?page={page}"
    return url


def collect_fanar_radius_seed_details(max_pages: int, delay: float) -> list[SellerDetails]:
    all_details: list[SellerDetails] = []
    max_pages = max(1, max_pages)

    for purpose, category_slug in OLX_PURPOSES.items():
        for area, config in OLX_TARGET_SEARCHES.items():
            query = config["query"]
            location = config.get("location", "")
            first_url = olx_search_url(category_slug, query, 1, location)
            print(f"Fetching {purpose} {area}: {first_url}", file=sys.stderr)
            try:
                first_html = fetch_url(first_url, timeout=15, retries=1)
            except (HTTPError, URLError, TimeoutError) as exc:
                print(f"  skipped {purpose} {area}: {exc}", file=sys.stderr)
                continue

            page_count = min(parse_page_count(first_html), max_pages)
            all_details.extend(search_page_details(first_html, area, purpose))

            for page in range(2, page_count + 1):
                time.sleep(delay)
                url = olx_search_url(category_slug, query, page, location)
                print(f"Fetching {purpose} {area} page {page}/{page_count}: {url}", file=sys.stderr)
                try:
                    raw_html = fetch_url(url, timeout=15, retries=1)
                except (HTTPError, URLError, TimeoutError) as exc:
                    print(f"  skipped {purpose} {area} page {page}: {exc}", file=sys.stderr)
                    continue
                all_details.extend(search_page_details(raw_html, area, purpose))

    return dedupe_seller_details(all_details)


def search_page_details(raw_html: str, area: str, purpose: str) -> list[SellerDetails]:
    details: list[SellerDetails] = []
    for item in extract_search_result_objects(raw_html):
        result = search_item_to_details(item, area, purpose)
        if result:
            details.append(result)
    return details


def dedupe_seller_details(details: Iterable[SellerDetails]) -> list[SellerDetails]:
    by_url: dict[str, SellerDetails] = {}
    for item in details:
        by_url.setdefault(item.listing.url, item)
    return list(by_url.values())


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"seen_urls": []}
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        backup_path = path.with_name(f"{path.name}.corrupt-{datetime.now():%Y%m%d_%H%M%S}.bak")
        try:
            os.replace(path, backup_path)
            print(f"State file was corrupt; backed it up to {backup_path} and started fresh.", file=sys.stderr)
        except OSError as exc:
            print(f"State file is corrupt and could not be backed up: {exc}", file=sys.stderr)
        return {"seen_urls": []}
    except OSError as exc:
        print(f"State file could not be read; starting safely with empty state: {exc}", file=sys.stderr)
        return {"seen_urls": []}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    last_error: OSError | None = None
    for _ in range(3):
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, ensure_ascii=False, sort_keys=True)
            os.replace(temp_path, path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.2)
    if last_error is not None:
        print(f"State file could not be saved after retries: {last_error}", file=sys.stderr)
    try:
        temp_path.unlink(missing_ok=True)
    except OSError:
        pass


def load_message_outbox(path: Path = OUTBOX_PATH) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except json.JSONDecodeError:
        backup_path = path.with_name(f"{path.name}.corrupt-{datetime.now():%Y%m%d_%H%M%S}.bak")
        try:
            os.replace(path, backup_path)
            print(f"Outbox file was corrupt; backed it up to {backup_path}.", file=sys.stderr)
        except OSError as exc:
            print(f"Outbox file is corrupt and could not be backed up: {exc}", file=sys.stderr)
        return []
    except OSError as exc:
        print(f"Outbox file could not be read; continuing without queued messages: {exc}", file=sys.stderr)
        return []
    return data if isinstance(data, list) else []


def save_message_outbox(messages: list[dict], path: Path = OUTBOX_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    last_error: OSError | None = None
    for _ in range(3):
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(messages, handle, indent=2, ensure_ascii=False, sort_keys=True)
            os.replace(temp_path, path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.2)
    if last_error is not None:
        print(f"Outbox file could not be saved after retries: {last_error}", file=sys.stderr)
    try:
        temp_path.unlink(missing_ok=True)
    except OSError:
        pass


def queue_telegram_message(chat_id: str, message: str, reason: str, path: Path = OUTBOX_PATH) -> None:
    messages = load_message_outbox(path)
    messages.append(
        {
            "chat_id": chat_id,
            "message": message,
            "reason": reason,
            "queued_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    save_message_outbox(messages, path)


def flush_telegram_outbox(token: str, path: Path = OUTBOX_PATH) -> int:
    messages = load_message_outbox(path)
    if not messages:
        return 0

    remaining: list[dict] = []
    sent_count = 0
    for index, item in enumerate(messages):
        chat_id = str(item.get("chat_id") or "")
        message = str(item.get("message") or "")
        if not chat_id or not message:
            continue
        try:
            send_telegram_message(token, chat_id, message)
            sent_count += 1
        except (TelegramAPIError, HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            item["last_error"] = str(exc)
            item["last_attempt_at"] = datetime.now().isoformat(timespec="seconds")
            remaining.append(item)
            remaining.extend(messages[index + 1 :])
            break
    save_message_outbox(remaining, path)
    return sent_count


def telegram_connect_hosts() -> list[str]:
    raw_hosts = os.getenv("TELEGRAM_API_CONNECT_HOSTS", "")
    if raw_hosts:
        return [host.strip() for host in raw_hosts.split(",") if host.strip()]
    return list(DEFAULT_TELEGRAM_CONNECT_HOSTS)


def install_run_lock(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age_seconds = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age_seconds > LOCK_STALE_SECONDS:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            raise RuntimeError(f"Another alert scan is already running; lock file: {path}")
        break

    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "created_at": datetime.now().isoformat(timespec="seconds")}, handle)

    def release_lock() -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    atexit.register(release_lock)


def telegram_api(
    token: str,
    method: str,
    params: dict[str, str],
    *,
    timeout: int = 30,
    retries: int = 3,
) -> dict:
    payload = urlencode(params).encode("utf-8")

    backoff_seconds = 1.0
    max_backoff_seconds = 15.0
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        data = None
        connect_hosts: list[str | None] = telegram_connect_hosts() + [None]
        for connect_host in connect_hosts:
            try:
                data = telegram_api_request(token, method, payload, timeout, connect_host)
                break
            except HTTPError as exc:
                last_exc = exc
                if getattr(exc, "code", None) == 429 and attempt < retries:
                    time.sleep(backoff_seconds)
                    backoff_seconds = min(backoff_seconds * 2, max_backoff_seconds)
                    break
                raise
            except (URLError, TimeoutError, OSError, ValueError, http.client.HTTPException) as exc:
                last_exc = exc
                continue

        if data is None:
            if attempt >= retries and last_exc:
                raise last_exc
            time.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, max_backoff_seconds)
            continue

        if not data.get("ok", True):
            error_code = data.get("error_code")
            description = data.get("description") or "unknown error"
            # Telegram may respond with HTTP 200 but ok=false for rate limits and auth errors.
            if error_code == 429 and attempt < retries:
                retry_after = (data.get("parameters") or {}).get("retry_after")
                delay = float(retry_after) if retry_after is not None else backoff_seconds
                time.sleep(min(delay, max_backoff_seconds))
                backoff_seconds = min(backoff_seconds * 2, max_backoff_seconds)
                continue
            raise TelegramAPIError(method, error_code, description)

        return data

    if last_exc:
        raise last_exc
    return {"ok": False, "result": []}


def telegram_api_request(
    token: str,
    method: str,
    payload: bytes,
    timeout: int,
    connect_host: str | None = None,
) -> dict:
    path = f"/bot{token}/{method}"
    url = f"https://{TELEGRAM_API_HOST}{path}"
    if not connect_host:
        request = Request(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    request_bytes = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {TELEGRAM_API_HOST}\r\n"
        "Content-Type: application/x-www-form-urlencoded\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + payload

    context = ssl.create_default_context()
    with socket.create_connection((connect_host, 443), timeout=timeout) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=TELEGRAM_API_HOST) as tls_socket:
            tls_socket.settimeout(timeout)
            tls_socket.sendall(request_bytes)
            response = http.client.HTTPResponse(tls_socket)
            response.begin()
            body = response.read()
            if response.status >= 400:
                raise HTTPError(url, response.status, response.reason, response.headers, None)
            return json.loads(body.decode("utf-8"))


def send_telegram_message(token: str, chat_id: str, message: str) -> None:
    # Telegram messages have a hard length cap. Split on listing boundaries when possible.
    chunks = split_message(message)
    for chunk in chunks:
        telegram_api(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": "true",
            },
        )
        time.sleep(0.3)


def split_message(message: str, max_len: int = 3900) -> list[str]:
    if len(message) <= max_len:
        return [message]
    chunks: list[str] = []
    current = ""
    for part in message.split("\n\n"):
        candidate = f"{current}\n\n{part}".strip() if current else part
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = part[:max_len]
    if current:
        chunks.append(current)
    return chunks


def format_listing(details: SellerDetails, seller_post_count: int) -> str:
    listing = details.listing
    ratio = money(listing.price_per_sqm) if listing.price_per_sqm is not None else "n/a"
    seller = details.seller_name or "unknown account"
    phone = details.phone_number or "not shown by OLX"
    distance = f"{details.distance_km:.1f}km from Fanar" if details.distance_km is not None else "distance unknown"
    purpose = details.purpose or infer_purpose(listing)
    return "\n".join(
        [
            f"{listing.city} {purpose}: {listing.title}",
            f"Price: {money(listing.price_usd)} | SQM: {listing.sqm or 'n/a'} | USD/SQM: {ratio}",
            f"Location: {listing.location or 'n/a'} | {distance} | Posted: {listing.created or 'n/a'}",
            f"Account: {seller}",
            f"Phone: {phone}",
            f"Owner score: {details.owner_score} | Apartment posts found in target areas: {seller_post_count}",
            f"Link: {listing.url}",
        ]
    )


def listing_age(created: str) -> timedelta | None:
    text = created.strip().lower()
    if not text:
        return None
    if text in {"just now", "now"}:
        return timedelta()
    if text in {"today"}:
        return timedelta()
    if text == "yesterday":
        return timedelta(days=1)

    match = re.search(r"(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)\s+ago", text)
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("minute"):
        return timedelta(minutes=amount)
    if unit.startswith("hour"):
        return timedelta(hours=amount)
    if unit.startswith("day"):
        return timedelta(days=amount)
    if unit.startswith("week"):
        return timedelta(weeks=amount)
    if unit.startswith("month"):
        return timedelta(days=30 * amount)
    if unit.startswith("year"):
        return timedelta(days=365 * amount)
    return None


def within_created_days(listing: Listing, days: int | None) -> bool:
    if not days:
        return True
    age = listing_age(listing.created)
    return age is not None and age <= timedelta(days=days)


def build_alert_message(
    owner_details: list[tuple[SellerDetails, int]],
    best_count: int,
    summary: dict | None = None,
    near_misses: list[tuple[SellerDetails, int]] | None = None,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not owner_details:
        lines = [
            f"Owner-listing alert {now}",
            "",
            f"No strict owner apartment listings found in {TARGET_AREA_LABEL} for sale or rent.",
        ]
        if summary:
            lines.extend(
                [
                    "",
                    "Scan summary:",
                    f"Checked: {summary.get('new_listings', 0)} new sale/rent listings",
                    f"Inside target radius: {summary.get('inside_radius', 0)}",
                    f"Private-looking pages opened for confirmation: {summary.get('detail_checked', 0)}",
                    f"Agency/business-like rejected: {summary.get('agency_like_rejected', 0)}",
                ]
            )
        if near_misses:
            lines.extend(["", "Closest private-looking rejects, not sent as owner matches:"])
            for index, (details, seller_count) in enumerate(near_misses[:3], start=1):
                lines.extend(["", f"{index}. {format_listing(details, seller_count)}"])
                if details.exclusion_reasons:
                    lines.append("Rejected: " + "; ".join(details.exclusion_reasons))
        return "\n".join(lines)

    ranked = sorted(
        owner_details,
        key=lambda item: item[0].listing.price_per_sqm if item[0].listing.price_per_sqm is not None else float("inf"),
    )
    best = ranked[:best_count]
    lines = [f"Owner-listing alert {now}", "", f"New likely-owner listings: {len(owner_details)}"]
    if best:
        lines.extend(["", "Best price/sqm among today's owner-like listings:"])
        for index, (details, seller_count) in enumerate(best, start=1):
            lines.extend(["", f"{index}. {format_listing(details, seller_count)}"])
    return "\n".join(lines)


def write_decision_log(
    owner_details: list[tuple[SellerDetails, int]],
    rejected_details: list[tuple[SellerDetails, int]],
    summary: dict,
) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"owner_alert_decisions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    rows = {
        "summary": summary,
        "accepted": [decision_row(details, seller_count) for details, seller_count in owner_details],
        "rejected": [decision_row(details, seller_count) for details, seller_count in rejected_details],
    }
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def decision_row(details: SellerDetails, seller_count: int) -> dict:
    listing = details.listing
    return {
        "city": listing.city,
        "title": listing.title,
        "price_usd": listing.price_usd,
        "sqm": listing.sqm,
        "price_per_sqm": listing.price_per_sqm,
        "location": listing.location,
        "created": listing.created,
        "url": listing.url,
        "seller_id": details.seller_id,
        "seller_name": details.seller_name,
        "phone_number": details.phone_number,
        "account_ads_count": details.account_ads_count,
        "seller_type": details.seller_type,
        "agency_name": details.agency_name,
        "agency_id": details.agency_id,
        "agent_code": details.agent_code,
        "ownership": details.ownership,
        "purpose": details.purpose,
        "lat": details.lat,
        "lng": details.lng,
        "distance_km": details.distance_km,
        "owner_score": details.owner_score,
        "seller_target_post_count": seller_count,
        "exclusion_reasons": list(details.exclusion_reasons),
        "from_search_page": details.from_search_page,
    }


def build_summary(
    new_details: list[SellerDetails],
    accepted: list[tuple[SellerDetails, int]],
    rejected: list[tuple[SellerDetails, int]],
    detail_checked: int,
) -> dict:
    agency_like_rejected = 0
    for details, seller_count in rejected:
        if is_agency_like(details, seller_count):
            agency_like_rejected += 1
    return {
        "collected_after_filters": len(new_details),
        "new_listings": len(new_details),
        "inside_radius": sum(
            1 for details in new_details if details.distance_km is not None and details.distance_km <= DEFAULT_RADIUS_KM
        ),
        "sale": sum(1 for details in new_details if details.purpose == "sale"),
        "rent": sum(1 for details in new_details if details.purpose == "rent"),
        "detail_checked": detail_checked,
        "accepted": len(accepted),
        "rejected": len(rejected),
        "agency_like_rejected": agency_like_rejected,
    }


def is_agency_like(details: SellerDetails, seller_count: int) -> bool:
    if seller_count > 3:
        return True
    if details.agency_name or details.agency_id:
        return True
    if agency_like_agent_code(details.agent_code):
        return True
    if details.agent_code and not (
        seller_count <= 3
        and details.seller_type.lower() not in {"2", "business", "agency"}
    ):
        return True
    if details.seller_type in {"2", "business", "agency"}:
        return True
    return agency_text_found(details.seller_name, details.listing.title, details.description)


def private_looking_near_misses(
    rejected: list[tuple[SellerDetails, int]],
    limit: int = 3,
) -> list[tuple[SellerDetails, int]]:
    candidates: list[tuple[SellerDetails, int]] = []
    for details, seller_count in rejected:
        if seller_count > 3:
            continue
        if details.agency_name or details.agency_id:
            continue
        if agency_text_found(details.seller_name, details.listing.title, details.description):
            continue
        seller_type = details.seller_type.lower()
        if seller_type in {"2", "business", "agency"}:
            continue
        if details.distance_km is not None and details.distance_km > DEFAULT_RADIUS_KM + 1:
            continue
        candidates.append((details, seller_count))

    def sort_key(item: tuple[SellerDetails, int]) -> tuple[float, int, float]:
        details, _ = item
        distance_penalty = 0.0
        if details.distance_km is None:
            distance_penalty = 10.0
        elif details.distance_km > DEFAULT_RADIUS_KM:
            distance_penalty = details.distance_km - DEFAULT_RADIUS_KM
        ratio = details.listing.price_per_sqm if details.listing.price_per_sqm is not None else float("inf")
        return (distance_penalty, details.owner_score, ratio)

    return sorted(candidates, key=sort_key)[:limit]


def get_chat_id(token: str) -> int:
    result = telegram_api(token, "getUpdates", {})
    updates = result.get("result", [])
    if not updates:
        raise RuntimeError("No Telegram updates found. Send a message to your bot first, then rerun.")
    message = updates[-1].get("message") or updates[-1].get("channel_post") or {}
    chat = message.get("chat") or {}
    if "id" not in chat:
        raise RuntimeError("Could not find a chat id in the latest Telegram update.")
    return chat["id"]


def load_dotenv(path: Path = Path(".env"), *, override: bool = False) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value


def load_env_files() -> None:
    load_dotenv(Path(".env"), override=False)
    load_dotenv(Path(".env.private"), override=True)


def run(args: argparse.Namespace) -> int:
    load_env_files()
    token = args.telegram_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = args.telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID", "")

    if args.show_chat_id:
        if not token:
            print("Set TELEGRAM_BOT_TOKEN first.", file=sys.stderr)
            return 2
        try:
            print(get_chat_id(token))
        except (TelegramAPIError, HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            print(f"Telegram error: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.owner_score_threshold == DEFAULT_OWNER_SCORE_THRESHOLD and args.created_within_days >= 21:
        args.owner_score_threshold = DEFAULT_HISTORICAL_OWNER_SCORE_THRESHOLD

    try:
        install_run_lock(args.lock)
    except RuntimeError as exc:
        print(exc)
        return 0

    if token:
        sent_pending = flush_telegram_outbox(token)
        if sent_pending:
            print(f"Sent {sent_pending} queued Telegram message(s).")

    state = load_state(args.state)
    seen_urls = set(state.get("seen_urls", []))
    details_list = collect_fanar_radius_seed_details(max_pages=args.max_pages, delay=args.delay)
    new_details = details_list if args.ignore_seen else [
        details for details in details_list if details.listing.url not in seen_urls
    ]
    new_details = [details for details in new_details if (details.listing.price_usd or 0) >= args.min_price]
    new_details = [details for details in new_details if within_created_days(details.listing, args.created_within_days)]
    new_details.sort(key=lambda details: candidate_sort_key(details.listing))

    print(f"Collected {len(details_list)} listings; {len(new_details)} are new after state filtering.")

    seller_counts: dict[str, int] = {}
    phone_counts: dict[str, int] = {}
    for details in new_details:
        key = details.seller_id or details.seller_name or details.listing.url
        seller_counts[key] = seller_counts.get(key, 0) + 1
        if details.phone_number:
            phone_counts[details.phone_number] = phone_counts.get(details.phone_number, 0) + 1

    initial_scored: list[SellerDetails] = []
    for details in new_details:
        key = details.seller_id or details.seller_name or details.listing.url
        seller_count = seller_counts.get(key, 1)
        phone_count = phone_counts.get(details.phone_number, 0)
        _, reasons = likely_owner(details, seller_count, args.owner_score_threshold, phone_count)
        details.exclusion_reasons = reasons
        initial_scored.append(details)

    detail_candidates = [
        details
        for details in initial_scored
        if details.owner_score <= args.detail_score_limit
        and not preliminary_rejection_reasons(details.listing)
    ]
    detail_candidates = balanced_detail_candidates(detail_candidates, args.max_candidates)
    print(
        f"Scored {len(initial_scored)} listings from search pages; "
        f"opening {len(detail_candidates)} private-looking pages for phone/account confirmation."
    )

    fetched_by_url: dict[str, SellerDetails] = {}
    if detail_candidates:
        fetched_details = enrich_listings(
            [details.listing for details in detail_candidates],
            args.detail_workers,
            args.detail_delay,
        )
        fetched_by_url = {details.listing.url: details for details in fetched_details}

    accepted: list[tuple[SellerDetails, int]] = []
    rejected: list[tuple[SellerDetails, int]] = []
    merged_details: list[SellerDetails] = []
    for details in initial_scored:
        fetched = fetched_by_url.get(details.listing.url)
        if fetched:
            details = merge_details(details, fetched)
        merged_details.append(details)

    merged_phone_counts = dict(phone_counts)
    for details in merged_details:
        if details.phone_number:
            merged_phone_counts[details.phone_number] = max(
                merged_phone_counts.get(details.phone_number, 0),
                sum(1 for item in merged_details if item.phone_number == details.phone_number),
            )

    for details in merged_details:
        key = details.seller_id or details.seller_name or details.listing.url
        seller_count = seller_counts.get(key, 1)
        phone_count = merged_phone_counts.get(details.phone_number, 0)
        ok, reasons = likely_owner(details, seller_count, args.owner_score_threshold, phone_count)
        details.exclusion_reasons = reasons
        if ok:
            accepted.append((details, seller_count))
        else:
            rejected.append((details, seller_count))

    summary = build_summary(new_details, accepted, rejected, len(detail_candidates))
    near_misses = private_looking_near_misses(rejected)
    log_path = write_decision_log(accepted, rejected, summary)
    print(f"Accepted {len(accepted)} likely-owner listings; rejected {len(rejected)}. Log: {log_path}")

    message = build_alert_message(accepted, args.top, summary, near_misses)
    if args.dry_run:
        print("\n--- Telegram dry run ---\n")
        print(message)
    else:
        if not token or not chat_id:
            print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or use --dry-run.", file=sys.stderr)
            return 2
        try:
            send_telegram_message(token, chat_id, message)
        except (TelegramAPIError, HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            queue_telegram_message(chat_id, message, str(exc))
            print(f"Telegram send error: {exc}", file=sys.stderr)

    if args.mark_seen_on_dry_run or not args.dry_run:
        processed_urls = {details.listing.url for details in new_details}
        state["seen_urls"] = sorted(seen_urls.union(processed_urls))
        state["last_run_at"] = datetime.now().isoformat(timespec="seconds")
        save_state(args.state, state)

    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send daily Telegram alerts for likely-owner apartment listings.")
    parser.add_argument("--telegram-token", default="", help="Telegram bot token. Defaults to TELEGRAM_BOT_TOKEN.")
    parser.add_argument("--telegram-chat-id", default="", help="Telegram chat id. Defaults to TELEGRAM_CHAT_ID.")
    parser.add_argument("--show-chat-id", action="store_true", help="Print latest chat id after you message the bot.")
    parser.add_argument("--state", type=Path, default=STATE_PATH, help=f"State file. Default: {STATE_PATH}.")
    parser.add_argument("--lock", type=Path, default=LOCK_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="OLX pages per city to scan daily.")
    parser.add_argument("--delay", type=float, default=0.8, help="Delay between OLX search page requests.")
    parser.add_argument("--detail-delay", type=float, default=0.5, help="Delay between OLX detail page requests.")
    parser.add_argument(
        "--detail-workers",
        type=int,
        default=DEFAULT_DETAIL_WORKERS,
        help="Parallel seller detail fetches. Default: 6.",
    )
    parser.add_argument("--top", type=int, default=10, help="Maximum listings to include in Telegram ranking.")
    parser.add_argument("--min-price", type=int, default=DEFAULT_MIN_PRICE, help="Minimum listing price to consider.")
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=DEFAULT_MAX_CANDIDATES,
        help="Maximum private-looking seller detail pages to inspect after search-page scoring. Default: 120.",
    )
    parser.add_argument(
        "--detail-score-limit",
        type=int,
        default=DEFAULT_DETAIL_SCORE_LIMIT,
        help="Open detail pages only when the search-page owner score is at or below this value. Default: 12.",
    )
    parser.add_argument(
        "--created-within-days",
        type=int,
        default=0,
        help="Only consider listings whose OLX creation label is within this many days. Default: 0 means no age filter.",
    )
    parser.add_argument(
        "--owner-post-limit",
        type=int,
        default=DEFAULT_OWNER_POST_LIMIT,
        help="Deprecated compatibility option; owner filtering now uses --owner-score-threshold.",
    )
    parser.add_argument(
        "--owner-score-threshold",
        type=int,
        default=DEFAULT_OWNER_SCORE_THRESHOLD,
        help="Accept listings with owner-likelihood score at or below this value. Lower is stricter. Default: 4.",
    )
    parser.add_argument("--ignore-seen", action="store_true", help="Scan matching listings even if already in state.")
    parser.add_argument("--dry-run", action="store_true", help="Print the Telegram message instead of sending.")
    parser.add_argument(
        "--mark-seen-on-dry-run",
        action="store_true",
        help="With --dry-run, mark current new listings as seen so future runs only alert newer posts.",
    )
    return parser


def main() -> int:
    return run(build_arg_parser().parse_args())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log_crash(exc)
        raise
