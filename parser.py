from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class ProListing:
    url: str
    source: str
    area: str
    property_type: str
    purpose: str
    title: str
    description: str
    location: str
    price_usd: int | None
    size_sqm: int | None
    bedrooms: int | None
    bathrooms: int | None
    parking: int | None
    floor: str
    seller_name: str
    phone: str
    date_posted: str
    published_at: str
    image_count: int | None
    owner_score: int
    broker_score: int
    lead_type: str
    raw: dict[str, Any]

    @property
    def dedupe_key(self) -> str:
        base = "|".join(
            [
                self.phone or "",
                normalize_key(self.title),
                normalize_key(self.location),
                str(self.price_usd or ""),
            ]
        )
        return hashlib.sha256(base.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["dedupe_key"] = self.dedupe_key
        return data


def normalize_key(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]+", " ", (value or "").lower())).strip()


def infer_property_type(title: str, description: str, purpose: str) -> str:
    text = f"{title} {description}".lower()
    if "land" in text or "plot" in text:
        base = "land"
    elif "villa" in text:
        base = "villa"
    else:
        base = "apartment"
    return f"{base}_{purpose or 'sale'}"


def first_int(patterns: list[str], text: str) -> int | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
    return None


def infer_bedrooms(title: str, description: str) -> int | None:
    text = f"{title} {description}"
    return first_int([r"\b(\d+)\s*(?:bedrooms?|beds?|br)\b", r"\b(\d+)\s*master"], text)


def infer_bathrooms(title: str, description: str) -> int | None:
    return first_int([r"\b(\d+)\s*(?:bathrooms?|baths?|wc)\b"], f"{title} {description}")


def infer_parking(title: str, description: str) -> int | None:
    text = f"{title} {description}"
    if re.search(r"\bparking\b", text, re.I):
        return first_int([r"\b(\d+)\s*parking"], text) or 1
    return None


def infer_floor(title: str, description: str) -> str:
    text = f"{title} {description}"
    match = re.search(r"\b(?:floor|fl)\s*[:#-]?\s*([A-Za-z0-9]+)\b", text, re.I)
    return match.group(1) if match else ""


def image_count_from_raw(raw: dict[str, Any]) -> int | None:
    for key in ("images", "photos", "pictures"):
        value = raw.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def from_seller_details(details: Any, owner_score: int, broker_score: int, lead_type: str) -> ProListing:
    listing = details.listing
    description = getattr(details, "description", "") or ""
    title = getattr(listing, "title", "") or ""
    purpose = getattr(details, "purpose", "") or "sale"
    raw = {
        "seller_id": getattr(details, "seller_id", ""),
        "agency_name": getattr(details, "agency_name", ""),
        "agency_id": getattr(details, "agency_id", ""),
        "agent_code": getattr(details, "agent_code", ""),
        "account_ads_count": getattr(details, "account_ads_count", None),
        "existing_owner_score": getattr(details, "owner_score", None),
        "exclusion_reasons": list(getattr(details, "exclusion_reasons", ()) or ()),
    }
    return ProListing(
        url=getattr(listing, "url", ""),
        source="OLX Lebanon",
        area=getattr(listing, "city", "") or "",
        property_type=infer_property_type(title, description, purpose),
        purpose=purpose,
        title=title,
        description=description,
        location=getattr(listing, "location", "") or "",
        price_usd=getattr(listing, "price_usd", None),
        size_sqm=getattr(listing, "sqm", None),
        bedrooms=infer_bedrooms(title, description),
        bathrooms=infer_bathrooms(title, description),
        parking=infer_parking(title, description),
        floor=infer_floor(title, description),
        seller_name=getattr(details, "seller_name", "") or "",
        phone=getattr(details, "phone_number", "") or "",
        date_posted=getattr(listing, "created", "") or "",
        published_at="",
        image_count=image_count_from_raw(raw),
        owner_score=owner_score,
        broker_score=broker_score,
        lead_type=lead_type,
        raw=raw,
    )
