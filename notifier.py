from __future__ import annotations

from telegram_owner_alert import send_telegram_message

from parser import ProListing


def format_alert(listing: ProListing) -> str:
    contact = listing.phone or "Public listing contact only"
    return "\n".join(
        [
            "🔥 New Owner Lead",
            "",
            f"📍 Area: {listing.area or 'n/a'}",
            f"🏠 Type: {listing.property_type or 'n/a'}",
            f"📐 Size: {listing.size_sqm or 'n/a'} sqm",
            f"🛏 Bedrooms: {listing.bedrooms or 'n/a'}",
            f"💰 Price: ${listing.price_usd:,}" if listing.price_usd else "💰 Price: n/a",
            f"📞 Contact: {contact}",
            f"⭐ Owner Score: {listing.owner_score}/100",
            f"Broker Score: {listing.broker_score}/100",
            f"🔗 Link: {listing.url}",
        ]
    )


def should_alert(listing: ProListing, threshold: int) -> bool:
    return (
        listing.lead_type == "likely_owner"
        and listing.owner_score >= threshold
        and bool(listing.phone or listing.url)
        and bool(listing.price_usd and listing.price_usd > 0)
    )


def send_alerts(token: str, chat_id: str, listings: list[ProListing], threshold: int, dry_run: bool = False, delivery_key: str = "") -> int:
    if not token or not chat_id:
        return 0
    sent = 0
    for listing in listings:
        if not should_alert(listing, threshold):
            continue
        message = format_alert(listing)
        if dry_run:
            print(message)
        else:
            send_telegram_message(token, chat_id, message, delivery_key=f"{delivery_key}:{listing.url}" if delivery_key else "")
        sent += 1
    return sent
