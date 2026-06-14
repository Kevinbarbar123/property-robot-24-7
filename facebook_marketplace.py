#!/usr/bin/env python3
"""
Facebook Marketplace apartment search for the same Metn target areas used by
the OLX scan, scored with the same owner-likelihood rules (owner_scoring.py).

Facebook Marketplace has no public search API, so this module drives a real
(logged-in) browser session with Playwright. The session is created once with
facebook_login_setup.py and reused headlessly here.

Environment variables:
  FACEBOOK_SESSION_PATH        Path to the saved Playwright session.
                                Default: data/facebook_session_state.json
  FACEBOOK_MARKETPLACE_LOCATION  Facebook Marketplace location segment used in
                                search URLs. Default: "beirut". If results look
                                wrong, open Marketplace in your own browser,
                                search near Beirut, and copy the location
                                segment from the URL into this variable.

Facebook frequently changes Marketplace's HTML structure. If card or detail
extraction stops finding fields, run this file directly with --headed to watch
the browser and update the selectors/parsing helpers below.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

from property_bot import (
    Listing,
    is_apartment_candidate,
    money,
    normalize_text,
    parse_price,
    parse_sqm,
)
from owner_scoring import (
    SellerDetails,
    TARGET_AREAS,
    canonical_target_city,
    distance_from_fanar,
    extract_phone_number_from_description,
    preliminary_rejection_reasons,
)


FACEBOOK_BASE_URL = "https://www.facebook.com"
DEFAULT_SESSION_PATH = Path("data/facebook_session_state.json")
DEFAULT_MARKETPLACE_LOCATION = "beirut"
DEFAULT_MAX_LISTINGS_PER_PURPOSE = 150
DEFAULT_MAX_CANDIDATES_PER_PURPOSE = 15
DEFAULT_SCROLL_PAUSE = 1.5
DEFAULT_DETAIL_DELAY = 2.0
MAX_SCROLL_ATTEMPTS = 14

MARKETPLACE_QUERIES = {
    "rent": "apartment for rent",
    "sale": "apartment for sale",
}

_POSTED_UNIT_MAP = {
    "min": "minutes",
    "mins": "minutes",
    "minute": "minute",
    "minutes": "minutes",
    "hr": "hours",
    "hrs": "hours",
    "hour": "hour",
    "hours": "hours",
    "day": "day",
    "days": "days",
    "week": "week",
    "weeks": "weeks",
    "month": "month",
    "months": "months",
    "year": "year",
    "years": "years",
}


class FacebookMarketplaceError(Exception):
    pass


class FacebookSessionError(FacebookMarketplaceError):
    pass


def session_path_from_env() -> Path:
    return Path(os.getenv("FACEBOOK_SESSION_PATH", str(DEFAULT_SESSION_PATH)))


def marketplace_location_from_env() -> str:
    return os.getenv("FACEBOOK_MARKETPLACE_LOCATION", DEFAULT_MARKETPLACE_LOCATION)


def marketplace_search_url(purpose: str) -> str:
    query = MARKETPLACE_QUERIES[purpose]
    params = urlencode({"query": query, "sortBy": "creation_time_descend", "exact": "false"})
    return f"{FACEBOOK_BASE_URL}/marketplace/{marketplace_location_from_env()}/search/?{params}"


def normalize_marketplace_url(href: str) -> str:
    match = re.search(r"/marketplace/item/(\d+)", href)
    if not match:
        return ""
    return f"{FACEBOOK_BASE_URL}/marketplace/item/{match.group(1)}/"


def looks_like_price_text(line: str) -> bool:
    return bool(re.search(r"\d", line)) and bool(re.search(r"[$]|l\.?l\.?|usd", line, re.I))


def normalize_posted_label(body_text: str) -> str:
    lowered = body_text.lower()
    if "just now" in lowered or "just listed" in lowered:
        return "today"
    match = re.search(
        r"\b(\d+)\s*(min|mins|minute|minutes|hr|hrs|hour|hours|day|days|week|weeks|month|months|year|years)\b",
        lowered,
    )
    if not match:
        if "yesterday" in lowered:
            return "yesterday"
        return ""
    amount, unit = match.group(1), match.group(2)
    return f"{amount} {_POSTED_UNIT_MAP.get(unit, unit)} ago"


def card_to_seller_details(card: dict, purpose: str) -> SellerDetails | None:
    url = normalize_marketplace_url(card.get("href", ""))
    if not url:
        return None

    lines = [line.strip() for line in card.get("text", "").splitlines() if line.strip()]
    if not lines:
        return None

    price_usd: int | None = None
    title = ""
    location_text = ""
    for line in lines:
        if price_usd is None and looks_like_price_text(line):
            price_usd = parse_price(line)
            continue
        if not title:
            title = line
            continue
        if not location_text and line != title:
            location_text = line

    if not title:
        title = lines[0]

    city = canonical_target_city(f"{title} {location_text}") or canonical_target_city(location_text)
    if not city:
        return None

    centroid = TARGET_AREAS[city]
    lat, lng = float(centroid["lat"]), float(centroid["lng"])

    listing = Listing(
        city=city,
        title=normalize_text(title),
        price_usd=price_usd,
        sqm=parse_sqm(title),
        location=normalize_text(location_text),
        created="",
        url=url,
        source=f"Facebook Marketplace {purpose}",
    )
    return SellerDetails(
        listing=listing,
        lat=lat,
        lng=lng,
        distance_km=distance_from_fanar(lat, lng),
        purpose=purpose,
        from_search_page=True,
    )


def extract_description_text(body_text: str) -> str:
    marker = "\nDescription\n"
    index = body_text.find(marker)
    if index == -1:
        return ""
    remainder = body_text[index + len(marker):]
    end = len(remainder)
    for end_marker in ("\nSeller information", "\nDetails", "\nLocation", "\nSafety tips", "\nMore from"):
        pos = remainder.find(end_marker)
        if pos != -1:
            end = min(end, pos)
    return normalize_text(remainder[:end])


def extract_seller_info(page) -> tuple[str, str, str]:
    """Return (seller_name, seller_profile_url, seller_type)."""
    from playwright.sync_api import Error as PlaywrightError

    profile_link = page.locator("a[href*='/marketplace/profile/']").first
    try:
        if profile_link.count() > 0:
            name = normalize_text(profile_link.inner_text())
            href = profile_link.get_attribute("href") or ""
            if name:
                return name, href, "individual"
    except PlaywrightError:
        pass

    # Business/Page sellers link to their Page rather than a marketplace profile.
    try:
        for link in page.locator("h2 a, h3 a").all()[:5]:
            href = link.get_attribute("href") or ""
            name = normalize_text(link.inner_text())
            if name and href and "/marketplace/" not in href and "/login" not in href:
                return name, href, "business"
    except PlaywrightError:
        pass

    return "", "", ""


def enrich_detail(page, details: SellerDetails) -> None:
    page.goto(details.listing.url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)
    body_text = page.inner_text("body")

    description = extract_description_text(body_text)
    if description:
        details.description = description

    posted_label = normalize_posted_label(body_text)
    if posted_label:
        details.listing = replace(details.listing, created=posted_label)

    phone = extract_phone_number_from_description(f"{description} {body_text}")
    if phone:
        details.phone_number = phone

    seller_name, seller_href, seller_type = extract_seller_info(page)
    if seller_name:
        details.seller_name = seller_name
    if seller_href:
        seller_id_match = re.search(r"/marketplace/profile/(\d+)", seller_href) or re.search(
            r"profile\.php\?id=(\d+)", seller_href
        )
        if seller_id_match:
            details.seller_id = seller_id_match.group(1)
        else:
            details.seller_id = seller_href.rstrip("/").rsplit("/", 1)[-1]
    if seller_type == "business" and seller_name:
        details.seller_type = "business"
        details.agency_name = seller_name
    elif seller_type == "individual":
        details.seller_type = "individual"

    if not details.listing.sqm:
        sqm = parse_sqm(f"{details.listing.title} {description}")
        if sqm:
            details.listing = replace(details.listing, sqm=sqm)


def collect_search_cards(page, url: str, max_listings: int, scroll_pause: float) -> list[dict]:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(int(scroll_pause * 1000))

    from playwright.sync_api import Error as PlaywrightError

    seen_hrefs: set[str] = set()
    cards: list[dict] = []
    for _ in range(MAX_SCROLL_ATTEMPTS):
        before = len(seen_hrefs)
        anchors = page.locator("a[href*='/marketplace/item/']")
        for index in range(anchors.count()):
            anchor = anchors.nth(index)
            href = anchor.get_attribute("href") or ""
            if not href or href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            try:
                text = anchor.inner_text()
            except PlaywrightError:
                continue
            cards.append({"href": href, "text": text})
            if len(cards) >= max_listings:
                return cards
        if len(seen_hrefs) == before:
            break
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(int(scroll_pause * 1000))
    return cards


def dedupe_by_url(details: list[SellerDetails]) -> list[SellerDetails]:
    by_url: dict[str, SellerDetails] = {}
    for item in details:
        by_url.setdefault(item.listing.url, item)
    return list(by_url.values())


def collect_facebook_radius_seed_details(
    max_listings_per_purpose: int = DEFAULT_MAX_LISTINGS_PER_PURPOSE,
    max_candidates_per_purpose: int = DEFAULT_MAX_CANDIDATES_PER_PURPOSE,
    headless: bool = True,
    session_path: Path | None = None,
    scroll_pause: float = DEFAULT_SCROLL_PAUSE,
    detail_delay: float = DEFAULT_DETAIL_DELAY,
) -> list[SellerDetails]:
    session_path = session_path or session_path_from_env()
    if not session_path.exists():
        raise FacebookSessionError(
            f"No saved Facebook session at {session_path}. "
            "Run facebook_login_setup.py once (on a machine with a browser) to create it."
        )

    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    all_details: list[SellerDetails] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        try:
            context = browser.new_context(storage_state=str(session_path))
            page = context.new_page()
            for purpose in MARKETPLACE_QUERIES:
                url = marketplace_search_url(purpose)
                print(f"Fetching Facebook Marketplace {purpose}: {url}", file=sys.stderr)
                try:
                    cards = collect_search_cards(page, url, max_listings_per_purpose, scroll_pause)
                except PlaywrightError as exc:
                    print(f"  skipped Facebook {purpose}: {exc}", file=sys.stderr)
                    continue

                candidates: list[SellerDetails] = []
                for card in cards:
                    details = card_to_seller_details(card, purpose)
                    if details is None:
                        continue
                    if preliminary_rejection_reasons(details.listing):
                        continue
                    candidates.append(details)
                    if len(candidates) >= max_candidates_per_purpose:
                        break

                print(
                    f"  matched {len(candidates)} target-area candidates out of {len(cards)} cards",
                    file=sys.stderr,
                )

                for details in candidates:
                    try:
                        enrich_detail(page, details)
                    except PlaywrightError as exc:
                        details.exclusion_reasons = (f"facebook detail fetch failed: {exc}",)
                    all_details.append(details)
                    time.sleep(detail_delay)
        finally:
            browser.close()

    return dedupe_by_url(all_details)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone Facebook Marketplace scan for debugging.")
    parser.add_argument("--max-listings", type=int, default=DEFAULT_MAX_LISTINGS_PER_PURPOSE)
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES_PER_PURPOSE)
    parser.add_argument("--headed", action="store_true", help="Show the browser window for debugging selectors.")
    parser.add_argument("--session", type=Path, default=None, help="Override the saved session path.")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    try:
        details_list = collect_facebook_radius_seed_details(
            max_listings_per_purpose=args.max_listings,
            max_candidates_per_purpose=args.max_candidates,
            headless=not args.headed,
            session_path=args.session,
        )
    except FacebookSessionError as exc:
        print(exc, file=sys.stderr)
        return 2

    print(f"Collected {len(details_list)} Facebook Marketplace listings in target areas.")
    for details in details_list:
        listing = details.listing
        apartment_note = "" if is_apartment_candidate(listing) else " (not apartment-like)"
        print(f"- [{listing.city}] {listing.title} | {money(listing.price_usd)} | {listing.url}{apartment_note}")
        print(f"  seller={details.seller_name!r} type={details.seller_type!r} phone={details.phone_number!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
