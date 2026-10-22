from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

from parser import ProListing


SCHEMA = """
CREATE TABLE IF NOT EXISTS scrape_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    total_seen INTEGER DEFAULT 0,
    new_saved INTEGER DEFAULT 0,
    error TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    source TEXT,
    area TEXT,
    property_type TEXT,
    purpose TEXT,
    title TEXT,
    description TEXT,
    location TEXT,
    price_usd INTEGER,
    size_sqm INTEGER,
    bedrooms INTEGER,
    bathrooms INTEGER,
    parking INTEGER,
    floor TEXT,
    seller_name TEXT,
    phone TEXT,
    date_posted TEXT,
    published_at TEXT,
    image_count INTEGER,
    owner_score INTEGER,
    broker_score INTEGER,
    lead_type TEXT,
    dedupe_key TEXT,
    raw_json TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER,
    phone TEXT,
    seller_name TEXT,
    contact_method TEXT,
    created_at TEXT,
    FOREIGN KEY(listing_id) REFERENCES listings(id)
);
CREATE TABLE IF NOT EXISTS duplicates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_url TEXT,
    matched_url TEXT,
    reason TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    stage TEXT,
    message TEXT,
    created_at TEXT,
    FOREIGN KEY(run_id) REFERENCES scrape_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_listings_lead_type ON listings(lead_type);
CREATE INDEX IF NOT EXISTS idx_listings_area ON listings(area);
CREATE INDEX IF NOT EXISTS idx_listings_phone ON listings(phone);
CREATE INDEX IF NOT EXISTS idx_listings_dedupe_key ON listings(dedupe_key);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class LeadDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def start_run(self) -> int:
        cursor = self.connection.execute(
            "INSERT INTO scrape_runs (started_at, status) VALUES (?, ?)",
            (now(), "running"),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, total_seen: int, new_saved: int, error: str = "") -> None:
        self.connection.execute(
            "UPDATE scrape_runs SET finished_at = ?, status = ?, total_seen = ?, new_saved = ?, error = ? WHERE id = ?",
            (now(), status, total_seen, new_saved, error, run_id),
        )
        self.connection.commit()

    def log_error(self, run_id: int, stage: str, message: str) -> None:
        self.connection.execute(
            "INSERT INTO errors (run_id, stage, message, created_at) VALUES (?, ?, ?, ?)",
            (run_id, stage, message, now()),
        )
        self.connection.commit()

    def find_duplicate(self, listing: ProListing) -> tuple[str, str] | None:
        row = self.connection.execute("SELECT url FROM listings WHERE url = ?", (listing.url,)).fetchone()
        if row:
            return row["url"], "same url"
        if listing.phone:
            row = self.connection.execute("SELECT url FROM listings WHERE phone = ? LIMIT 1", (listing.phone,)).fetchone()
            if row:
                return row["url"], "same phone"
        row = self.connection.execute(
            "SELECT url FROM listings WHERE dedupe_key = ? LIMIT 1",
            (listing.dedupe_key,),
        ).fetchone()
        if row:
            return row["url"], "same title/location/price"
        return None

    def save_listing(self, listing: ProListing) -> bool:
        duplicate = self.find_duplicate(listing)
        if duplicate:
            self.connection.execute(
                "INSERT INTO duplicates (listing_url, matched_url, reason, created_at) VALUES (?, ?, ?, ?)",
                (listing.url, duplicate[0], duplicate[1], now()),
            )
            self.connection.commit()
            return False
        data = listing.as_dict()
        self.connection.execute(
            """
            INSERT INTO listings (
                url, source, area, property_type, purpose, title, description, location, price_usd, size_sqm,
                bedrooms, bathrooms, parking, floor, seller_name, phone, date_posted, published_at, image_count,
                owner_score, broker_score, lead_type, dedupe_key, raw_json, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["url"], data["source"], data["area"], data["property_type"], data["purpose"], data["title"],
                data["description"], data["location"], data["price_usd"], data["size_sqm"], data["bedrooms"],
                data["bathrooms"], data["parking"], data["floor"], data["seller_name"], data["phone"],
                data["date_posted"], data["published_at"], data["image_count"], data["owner_score"],
                data["broker_score"], data["lead_type"], data["dedupe_key"], json.dumps(data["raw"], ensure_ascii=False),
                now(), now(),
            ),
        )
        listing_id = self.connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        if listing.phone or listing.seller_name:
            self.connection.execute(
                "INSERT INTO contacts (listing_id, phone, seller_name, contact_method, created_at) VALUES (?, ?, ?, ?, ?)",
                (listing_id, listing.phone, listing.seller_name, "public_listing", now()),
            )
        self.connection.commit()
        return True

    def recent_listings(self, limit: int = 500) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM listings ORDER BY first_seen_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def export_csv(self, path: Path) -> None:
        rows = self.recent_listings(10000)
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = list(rows[0].keys()) if rows else ["url", "title", "area", "owner_score", "lead_type"]
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def export_json(self, path: Path) -> None:
        rows = self.recent_listings(10000)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    def close(self) -> None:
        self.connection.close()
