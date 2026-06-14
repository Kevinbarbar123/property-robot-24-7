#!/usr/bin/env python3
"""
Shared owner-likelihood scoring rules used by every listing source (OLX,
Facebook Marketplace, ...).

Keeping this in one module means every source is judged by the same rules:
apartment-only, inside the selected Metn target areas, and scored by how
likely the seller is a private owner rather than an agency/broker.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from property_bot import Listing, is_apartment_candidate, normalize_for_match


FANAR_CENTER_LAT = 33.877799
FANAR_CENTER_LNG = 35.577951
DEFAULT_RADIUS_KM = 15.0

ARABIC_DIGIT_TRANSLATION = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

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

# Approximate centroid for each selected Metn target area, used to score
# distance from Fanar when a source (e.g. Facebook Marketplace) does not
# expose precise listing coordinates. These are approximate town centers,
# not exact addresses.
TARGET_AREAS: dict[str, dict[str, object]] = {
    "Fanar": {"aliases": ["fanar"], "lat": 33.8778, "lng": 35.5780},
    "Mar Roukoz": {"aliases": ["mar roukoz", "mar roukos", "mar-roukoz", "mar-roukos"], "lat": 33.8651, "lng": 35.5938},
    "Broumana": {"aliases": ["broumana", "broummana", "brummana", "broumana metn"], "lat": 33.8719, "lng": 35.6189},
    "Beit Mery": {"aliases": ["beit mery", "beit merry", "beit-mery"], "lat": 33.8466, "lng": 35.6015},
    "Jdeideh": {"aliases": ["jdeideh", "jdeide", "jdaide", "jdeidet el metn", "jdeidet"], "lat": 33.8826, "lng": 35.5469},
    "Rawda": {"aliases": ["rawda", "new rawda", "rawda metn"], "lat": 33.8830, "lng": 35.6010},
    "Bsalim": {"aliases": ["bsalim", "bsaleem", "bsalim metn"], "lat": 33.8995, "lng": 35.6155},
    "Mezher": {"aliases": ["mezher", "mazher", "mezher metn"], "lat": 33.8865, "lng": 35.6005},
    "Biakout": {"aliases": ["biakout", "biaqout", "biyakout", "biakout metn"], "lat": 33.8780, "lng": 35.6075},
    "Sabtieh": {"aliases": ["sabtieh", "sabtaieh", "sabteih", "sabtaieh metn"], "lat": 33.8927, "lng": 35.5594},
    "Dekwaneh": {"aliases": ["dekwaneh", "dekouaneh", "dekouane"], "lat": 33.8838, "lng": 35.5495},
    "Mkalles": {"aliases": ["mkalles", "mekalles", "mkaless", "mkalles metn"], "lat": 33.8760, "lng": 35.5680},
    "Sin El Fil": {"aliases": ["sin el fil", "sin-el-fil", "sinelfil", "sin el fill"], "lat": 33.8795, "lng": 35.5398},
    "Jisr El Bacha": {"aliases": ["jisr el bacha", "jisr-el-bacha", "jesr el bacha"], "lat": 33.8840, "lng": 35.5660},
    "Horsh Tabet": {"aliases": ["horsh tabet", "horch tabet", "horch-tabet", "horsh-tabet"], "lat": 33.8869, "lng": 35.5605},
    "Baouchrieh": {"aliases": ["baouchrieh", "bauchrieh", "baouchriyeh", "sad el baouchrieh", "sed el baouchrieh"], "lat": 33.8909, "lng": 35.5523},
    "Rabweh": {"aliases": ["rabweh", "rabieh", "rabieh metn"], "lat": 33.8420, "lng": 35.5985},
    "Zalka": {"aliases": ["zalka"], "lat": 33.9070, "lng": 35.5571},
    "Jal El Dib": {"aliases": ["jal el dib", "jall el dib", "jal-el-dib", "jall-el-dib"], "lat": 33.9228, "lng": 35.6005},
    "Antelias": {"aliases": ["antelias"], "lat": 33.9168, "lng": 35.5930},
    "Dbayeh": {"aliases": ["dbayeh", "dbaye", "d bayeh"], "lat": 33.9418, "lng": 35.6098},
    "Nahr El Mott": {"aliases": ["nahr el mott", "nahr el mot", "nahr-el-mott", "nahr-el-mot"], "lat": 33.9010, "lng": 35.5868},
    "Kornet Chehwan": {"aliases": ["kornet chehwan", "cornet chehwan", "qornet chehwan", "kornet-chehwan"], "lat": 33.9018, "lng": 35.6280},
    "Ain Saadeh": {"aliases": ["ain saadeh", "ain saade", "ain-saadeh", "ain-saade"], "lat": 33.8870, "lng": 35.6210},
    "Mansourieh": {"aliases": ["mansourieh", "mansourieh metn", "mansouriyeh"], "lat": 33.8460, "lng": 35.5870},
    "Monteverde": {"aliases": ["monteverde", "monte verdi"], "lat": 33.8530, "lng": 35.6080},
    "Roumieh": {"aliases": ["roumieh", "roumie"], "lat": 33.9120, "lng": 35.6310},
    "Tilal Ain Saadeh": {"aliases": ["tilal ain saadeh", "tilal ain saade", "tilal-ain-saadeh", "tilal-ain-saade"], "lat": 33.8830, "lng": 35.6240},
    "Ain Najem": {"aliases": ["ain najm", "ain najem", "ain-najm", "ain-najem"], "lat": 33.8800, "lng": 35.6160},
}


# Sorted longest-alias-first so e.g. "Tilal Ain Saadeh" matches before the
# shorter "Ain Saadeh" when both appear as substrings of the same text.
_TARGET_AREA_ALIAS_TERMS: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            (normalize_for_match(alias), city)
            for city, config in TARGET_AREAS.items()
            for alias in [city, *config["aliases"]]
        ),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
)


def canonical_target_city(text: str) -> str:
    """Find a known target-area alias anywhere in free-form text (title, location, ...)."""
    match_blob = normalize_for_match(text)
    if not match_blob:
        return ""
    for alias_term, city in _TARGET_AREA_ALIAS_TERMS:
        if alias_term and re.search(rf"\b{re.escape(alias_term)}\b", match_blob):
            return city
    return ""


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def distance_from_fanar(lat: float, lng: float) -> float:
    return haversine_km(FANAR_CENTER_LAT, FANAR_CENTER_LNG, lat, lng)


def is_facebook_listing(listing: Listing) -> bool:
    return listing.source.startswith("Facebook Marketplace")


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


def agency_text_found(*values: str) -> bool:
    text = " ".join(value for value in values if value).lower()
    return any(term in text for term in AGENCY_TERMS)


def looks_like_reference_code(details: SellerDetails) -> bool:
    text = f"{details.listing.title} {details.description}"
    return looks_like_reference_code_text(text)


def looks_like_reference_code_text(text: str) -> bool:
    return bool(re.search(r"\b(?:ref|reference|code)\s*#?\s*[a-z]{1,4}\d{2,}\b", text, re.I))


def agency_like_agent_code(value: str) -> bool:
    code = re.sub(r"[^a-z0-9]", "", value.lower())
    if not code or code == "n":
        return False
    return any(code.startswith(prefix) and any(char.isdigit() for char in code) for prefix in AGENCY_CODE_PREFIXES)


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
