#!/usr/bin/env python3
"""
Apartment listing bot for Fanar, Jdeideh, Ain Saadeh, Broumana, and Mar Roukoz.

The first source implemented is OLX Lebanon because its listing pages expose the
fields needed for price-per-sqm analysis without requiring a browser session.
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


BASE_URL = "https://www.olx.com.lb"
DEFAULT_OUTPUT_DIR = Path("reports")
CRASH_LOG_PATH = Path("logs/property_bot.crash.log")
NON_APARTMENT_TERMS = (
    "villa",
    "villas",
    "land",
    "plot",
    "building",
    "warehouse",
    "office",
    "shop",
    "showroom",
)


CITY_SEARCHES = {
    "Fanar": {
        "query": "fanar",
        "aliases": ["fanar"],
    },
    "Jdeideh": {
        "query": "jdeideh",
        "aliases": ["jdeide", "jdeideh", "jdeidet", "jdeidet el metn"],
    },
    "Bsalim": {
        "query": "bsalim",
        "aliases": ["bsalim", "bsaleem", "bsalim metn"],
    },
    "Mezher": {
        "query": "mezher",
        "aliases": ["mezher", "mazher", "mezher metn"],
    },
    "Biakout": {
        "query": "biakout",
        "aliases": ["biakout", "biaqout", "biyakout", "biakout metn"],
    },
    "Mkalles": {
        "query": "mkalles",
        "aliases": ["mkalles", "mekalles", "mkaless", "mkalles metn"],
    },
    "Sin El Fil": {
        "query": "sin-el-fil",
        "aliases": ["sin el fil", "sin-el-fil", "sinelfil", "sin el fill"],
    },
    "Jisr El Bacha": {
        "query": "jisr-el-bacha",
        "aliases": ["jisr el bacha", "jisr-el-bacha", "jesr el bacha"],
    },
    "Horsh Tabet": {
        "query": "horch-tabet",
        "aliases": ["horsh tabet", "horch tabet", "horch-tabet", "horsh-tabet"],
    },
    "Baouchrieh": {
        "query": "baouchrieh",
        "aliases": ["baouchrieh", "bauchrieh", "baouchriyeh", "sad el baouchrieh", "sed el baouchrieh"],
    },
    "Nahr El Mott": {
        "query": "nahr-el-mott",
        "aliases": ["nahr el mott", "nahr el mot", "nahr-el-mott", "nahr-el-mot"],
    },
    "Kornet Chehwan": {
        "query": "kornet-chehwan",
        "aliases": ["kornet chehwan", "cornet chehwan", "qornet chehwan", "kornet-chehwan"],
    },
    "Ain Saadeh": {
        "query": "ain-saadeh",
        "aliases": ["ain saadeh", "ain saade", "ain-saadeh", "ain-saade"],
    },
    "Broumana": {
        "query": "broumana",
        "aliases": ["broumana", "broummana", "brummana"],
    },
    "Mar Roukoz": {
        "query": "mar-roukoz",
        "aliases": ["mar roukoz", "mar roukos", "mar-roukoz", "mar-roukos"],
    },
}


@dataclass(frozen=True)
class Listing:
    city: str
    title: str
    price_usd: int | None
    sqm: int | None
    location: str
    created: str
    url: str
    source: str = "OLX Lebanon"

    @property
    def price_per_sqm(self) -> float | None:
        if not self.price_usd or not self.sqm:
            return None
        return self.price_usd / self.sqm


def normalize_text(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_for_match(value: str) -> str:
    value = normalize_text(value).lower()
    value = value.replace("-", " ")
    return re.sub(r"\s+", " ", value)


def parse_price(value: str | None) -> int | None:
    if not value:
        return None
    if "usd" not in value.lower() and "$" not in value:
        return None
    digits = re.sub(r"[^\d]", "", value)
    return int(digits) if digits else None


def parse_sqm(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(\d[\d,]*)\s*(?:sqm|sq\.?\s*m\.?|m2)", value, re.I)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def fetch_url(url: str, timeout: int = 30, retries: int = 2, retry_delay: float = 2.0) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except HTTPError as exc:
            if exc.code < 500 or attempt == retries:
                raise
            time.sleep(retry_delay * (attempt + 1))
        except (URLError, TimeoutError, OSError):
            if attempt == retries:
                raise
            time.sleep(retry_delay * (attempt + 1))

    raise TimeoutError(f"Unable to fetch {url}")


def olx_search_url(query_slug: str, page: int) -> str:
    url = f"{BASE_URL}/properties/apartments-villas-for-sale/q-apartments-{query_slug}/"
    if page > 1:
        url = f"{url}?page={page}"
    return url


def parse_olx_page(raw_html: str, city: str, aliases: Iterable[str], strict: bool) -> list[Listing]:
    blocks = re.findall(r'<li class="" aria-label="Listing">(.*?)</li>', raw_html)
    listings: list[Listing] = []
    alias_terms = [normalize_for_match(alias) for alias in aliases]

    for block in blocks:
        title = extract_first(block, r"<h2[^>]*>(.*?)</h2>")
        price = extract_first(block, r'aria-label="Price".*?<span[^>]*>(.*?)</span>')
        area = extract_first(block, r'aria-label="Area".*?<span[^>]*>(.*?)</span>')
        location = extract_first(block, r'aria-label="Location">(.*?)<span')
        created = extract_first(block, r'aria-label="Creation date">(.*?)</span>')
        href = extract_first(block, r'<a href="([^"]+)" title=')

        if not title or not href:
            continue

        clean_title = normalize_text(title)
        clean_location = normalize_text(location or "")
        match_blob = normalize_for_match(f"{clean_title} {clean_location}")
        if strict and not any(term in match_blob for term in alias_terms):
            continue

        listings.append(
            Listing(
                city=city,
                title=clean_title,
                price_usd=parse_price(normalize_text(price or "")),
                sqm=parse_sqm(normalize_text(area or "")),
                location=clean_location,
                created=normalize_text(created or ""),
                url=urljoin(BASE_URL, html.unescape(href)),
            )
        )

    return listings


def extract_first(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, flags=re.S)
    return match.group(1) if match else None


def parse_page_count(raw_html: str) -> int:
    match = re.search(r'"page_count":(\d+)', raw_html)
    return int(match.group(1)) if match else 1


def collect_olx(max_pages: int, delay: float, strict: bool) -> list[Listing]:
    all_listings: list[Listing] = []

    for city, config in CITY_SEARCHES.items():
        query = config["query"]
        aliases = config["aliases"]
        first_url = olx_search_url(query, 1)
        print(f"Fetching {city}: {first_url}", file=sys.stderr)

        try:
            first_html = fetch_url(first_url)
        except (HTTPError, URLError, TimeoutError) as exc:
            print(f"  skipped {city}: {exc}", file=sys.stderr)
            continue

        page_count = min(parse_page_count(first_html), max_pages)
        all_listings.extend(parse_olx_page(first_html, city, aliases, strict))

        for page in range(2, page_count + 1):
            time.sleep(delay)
            url = olx_search_url(query, page)
            print(f"Fetching {city} page {page}/{page_count}: {url}", file=sys.stderr)
            try:
                raw_html = fetch_url(url)
            except (HTTPError, URLError, TimeoutError) as exc:
                print(f"  skipped {city} page {page}: {exc}", file=sys.stderr)
                continue
            all_listings.extend(parse_olx_page(raw_html, city, aliases, strict))

    return dedupe_listings(all_listings)


def dedupe_listings(listings: Iterable[Listing]) -> list[Listing]:
    by_url: dict[str, Listing] = {}
    for listing in listings:
        by_url.setdefault(listing.url, listing)
    return list(by_url.values())


def sortable_price(listing: Listing) -> tuple[int, int]:
    return (0 if listing.price_usd is not None else 1, listing.price_usd or 10**12)


def is_apartment_candidate(listing: Listing) -> bool:
    text = normalize_for_match(listing.title)
    return not any(re.search(rf"\b{re.escape(term)}\b", text) for term in NON_APARTMENT_TERMS)


def best_value_listings(listings: Iterable[Listing], limit: int, min_price: int) -> list[Listing]:
    priced = [
        listing
        for listing in listings
        if listing.price_per_sqm is not None
        and (listing.price_usd or 0) >= min_price
        and is_apartment_candidate(listing)
    ]
    return sorted(priced, key=lambda item: item.price_per_sqm or float("inf"))[:limit]


def write_csv(listings: list[Listing], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "city",
                "price_usd",
                "sqm",
                "price_per_sqm",
                "title",
                "location",
                "created",
                "source",
                "url",
            ]
        )
        for listing in sorted(listings, key=lambda item: (item.city, sortable_price(item))):
            writer.writerow(
                [
                    listing.city,
                    listing.price_usd or "",
                    listing.sqm or "",
                    f"{listing.price_per_sqm:.2f}" if listing.price_per_sqm else "",
                    listing.title,
                    listing.location,
                    listing.created,
                    listing.source,
                    listing.url,
                ]
            )


def money(value: int | float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.0f}"


def write_markdown_report(listings: list[Listing], path: Path, top_n: int, min_price: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    best = best_value_listings(listings, top_n, min_price)
    lines: list[str] = [
        "# Lebanon Apartment Listing Report",
        "",
        f"Generated: {generated_at}",
        "",
        "Source: OLX Lebanon search pages.",
        "",
        "## Best Apartment Price Per SQM",
        "",
        f"Minimum price used for this ranking: {money(min_price)}. Villa, land, building, office, shop, and showroom titles are excluded from this apartment ranking.",
        "",
    ]

    if best:
        lines.extend(
            [
                "| Rank | City | Price | SQM | USD/SQM | Title | Link |",
                "| ---: | --- | ---: | ---: | ---: | --- | --- |",
            ]
        )
        for index, listing in enumerate(best, start=1):
            lines.append(
                "| {rank} | {city} | {price} | {sqm} | {ratio} | {title} | [Open]({url}) |".format(
                    rank=index,
                    city=listing.city,
                    price=money(listing.price_usd),
                    sqm=listing.sqm or "n/a",
                    ratio=money(listing.price_per_sqm),
                    title=escape_table(listing.title),
                    url=listing.url,
                )
            )
    else:
        lines.append("No listings had both price and sqm data.")

    lines.extend(["", "## Listings By City", ""])
    for city in CITY_SEARCHES:
        city_listings = [listing for listing in listings if listing.city == city]
        city_listings.sort(key=sortable_price)
        lines.extend([f"### {city}", ""])

        if not city_listings:
            lines.extend(["No matching listings found.", ""])
            continue

        lines.extend(
            [
                "| Price | SQM | USD/SQM | Created | Title | Link |",
                "| ---: | ---: | ---: | --- | --- | --- |",
            ]
        )
        for listing in city_listings:
            lines.append(
                "| {price} | {sqm} | {ratio} | {created} | {title} | [Open]({url}) |".format(
                    price=money(listing.price_usd),
                    sqm=listing.sqm or "n/a",
                    ratio=money(listing.price_per_sqm),
                    created=escape_table(listing.created or "n/a"),
                    title=escape_table(listing.title),
                    url=listing.url,
                )
            )
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def print_console_summary(listings: list[Listing], top_n: int, min_price: int) -> None:
    print(f"Collected {len(listings)} unique listings.")
    for city in CITY_SEARCHES:
        city_count = sum(1 for listing in listings if listing.city == city)
        print(f"  {city}: {city_count}")

    best = best_value_listings(listings, top_n, min_price)
    if not best:
        print("\nNo listing met the price/sqm data and minimum-price filters.")
        return

    winner = best[0]
    print(f"\nBest apartment by lowest price/sqm, excluding prices below {money(min_price)}:")
    print(f"  {winner.city}: {winner.title}")
    print(f"  Price: {money(winner.price_usd)} | SQM: {winner.sqm} | USD/SQM: {money(winner.price_per_sqm)}")
    print(f"  {winner.url}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect Lebanon apartment listings and rank them by price per sqm."
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=20,
        help="Maximum OLX result pages to fetch per city. Default: 20.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.8,
        help="Delay in seconds between page requests. Default: 0.8.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for CSV and Markdown reports. Default: reports.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="How many best-value listings to show in the report. Default: 10.",
    )
    parser.add_argument(
        "--min-price",
        type=int,
        default=50_000,
        help="Ignore lower prices when choosing best value, to avoid placeholder prices. Default: 50000.",
    )
    parser.add_argument(
        "--include-nearby",
        action="store_true",
        help="Keep all OLX search results even if the city name is not in the title/location.",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    listings = collect_olx(
        max_pages=max(1, args.max_pages),
        delay=max(0, args.delay),
        strict=not args.include_nearby,
    )

    date_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = args.output_dir / f"property_listings_{date_stamp}.csv"
    report_path = args.output_dir / f"property_report_{date_stamp}.md"
    write_csv(listings, csv_path)
    min_price = max(0, args.min_price)
    write_markdown_report(listings, report_path, args.top, min_price)
    print_console_summary(listings, args.top, min_price)
    print(f"\nCSV: {csv_path}")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        try:
            CRASH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with CRASH_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} {type(exc).__name__}: {exc} ---\n")
                handle.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        except OSError:
            pass
        raise
