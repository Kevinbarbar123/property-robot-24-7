from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


OWNER_PHRASES = (
    "owner",
    "direct owner",
    "no commission",
    "no brokers",
    "no broker",
    "private sale",
    "direct sale",
    "from owner",
)

BROKER_PHRASES = (
    "agency",
    "commission",
    "agent",
    "broker",
    "real estate",
    "property consultant",
    "reference",
    "ref#",
)


@dataclass
class ScoreResult:
    owner_score: int
    broker_score: int
    lead_type: str
    owner_signals: list[str]
    broker_signals: list[str]


def phrase_hits(text: str, phrases: tuple[str, ...]) -> list[str]:
    normalized = text.lower()
    return [phrase for phrase in phrases if phrase in normalized]


def score_listing(details: Any, seller_post_count: int = 1, phone_post_count: int = 0) -> ScoreResult:
    listing = details.listing
    description = getattr(details, "description", "") or ""
    text = " ".join([getattr(listing, "title", ""), description, getattr(details, "seller_name", "")])
    owner_signals = phrase_hits(text, OWNER_PHRASES)
    broker_signals = phrase_hits(text, BROKER_PHRASES)

    owner_score = 20
    broker_score = 0

    if owner_signals:
        owner_score += min(35, len(owner_signals) * 12)
    if getattr(details, "phone_number", ""):
        owner_score += 15
    if seller_post_count <= 1:
        owner_score += 15
    elif seller_post_count <= 3:
        owner_score += 8
    if description and len(description.split()) < 35:
        owner_score += 7
        owner_signals.append("simple description")
    if re.search(r"\b(i|my|home|apartment)\b", description.lower()):
        owner_score += 5
        owner_signals.append("personal tone")

    account_ads_count = getattr(details, "account_ads_count", None)
    if account_ads_count and account_ads_count > 10:
        broker_score += 30
        broker_signals.append(f"many OLX ads ({account_ads_count})")
    if seller_post_count > 3:
        broker_score += 20
        broker_signals.append(f"many target listings ({seller_post_count})")
    if phone_post_count > 2:
        broker_score += 15
        broker_signals.append(f"phone reused ({phone_post_count})")
    if getattr(details, "agency_name", "") or getattr(details, "agency_id", ""):
        broker_score += 35
        broker_signals.append("agency profile")
    if getattr(details, "agent_code", ""):
        broker_score += 20
        broker_signals.append("agent/reference code")
    if broker_signals:
        broker_score += min(25, len(broker_signals) * 8)

    owner_score = max(0, min(100, owner_score - min(45, broker_score // 2)))
    broker_score = max(0, min(100, broker_score))

    if owner_score >= 70 and broker_score < 45:
        lead_type = "likely_owner"
    elif owner_score >= 50 and broker_score < 65:
        lead_type = "possible_owner"
    elif broker_score >= 65:
        lead_type = "likely_broker"
    else:
        lead_type = "unknown"

    return ScoreResult(owner_score, broker_score, lead_type, owner_signals[:8], broker_signals[:8])
