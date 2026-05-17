#!/usr/bin/env python3
"""
Private iPhone-friendly web control panel for the property robot.

Run:
  python .\\web_robot_app.py

Open the printed LAN URL from Safari on your iPhone and Add to Home Screen.
No PIN/password is required.
"""

from __future__ import annotations

import html
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from telegram_owner_alert import STATE_PATH, load_env_files


HOST = "0.0.0.0"
PORT = int(os.getenv("WEB_ROBOT_PORT") or os.getenv("PORT") or "8787")
APP_NAME = "Property Robot"
WEB_STATE_PATH = Path("data/web_app_state.json")
WEB_SCAN_LOG_PATH = Path("logs/web_robot_app.scan.log")
OUTBOX_PATH = Path("data/telegram_outbox.json")
REPORTS_DIR = Path("reports")
ALERT_SCRIPT = Path(__file__).with_name("telegram_owner_alert.py")
SCAN_LOCK = threading.Lock()
WEB_ERROR_LOG_PATH = Path("logs/web_robot_app.crash.log")


def log_error(message: str, exc: BaseException | None = None) -> None:
    try:
        WEB_ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with WEB_ERROR_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {message} ---\n")
            if exc is not None:
                handle.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except OSError:
        pass


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        backup = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d_%H%M%S')}.bak")
        try:
            os.replace(path, backup)
        except OSError:
            pass
        return default
    except OSError as exc:
        log_error(f"Could not read JSON state file {path}", exc)
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    last_error: OSError | None = None
    for _ in range(3):
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
            os.replace(temp_path, path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.2)
    if last_error is not None:
        log_error(f"Could not save JSON state file {path}", last_error)
    try:
        temp_path.unlink(missing_ok=True)
    except OSError:
        pass


def local_ip_address() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def status_summary() -> dict:
    robot_state = load_json(STATE_PATH, {"seen_urls": []})
    web_state = load_json(WEB_STATE_PATH, {})
    outbox = load_json(OUTBOX_PATH, [])
    return {
        "seen_count": len(robot_state.get("seen_urls", [])),
        "last_robot_run": robot_state.get("last_run_at", "never"),
        "queued_telegram_messages": len(outbox) if isinstance(outbox, list) else 0,
        "web": web_state,
    }


def report_files() -> list[Path]:
    reports: list[tuple[float, Path]] = []
    for item in REPORTS_DIR.glob("owner_alert_decisions_*.json"):
        try:
            reports.append((item.stat().st_mtime, item))
        except OSError as exc:
            log_error(f"Could not inspect report file {item}", exc)
    return [item for _, item in sorted(reports, reverse=True)]


def path_modified_text(path: Path) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
    except OSError as exc:
        log_error(f"Could not read modified time for {path}", exc)
        return "unknown time"


def decorate_report(path: Path, data: dict) -> dict:
    data = dict(data)
    data["_name"] = path.name
    data["_path"] = str(path)
    data["_modified"] = path_modified_text(path)
    return data


def latest_decision_report() -> dict:
    reports = report_files()
    if not reports:
        return {}
    fallback: dict = {}
    for report in reports:
        data = decorate_report(report, load_json(report, {}))
        if not fallback:
            fallback = data
        if data.get("accepted") or data.get("rejected"):
            if report != reports[0]:
                data["_note"] = "Newest scan had no new listings, so this shows the last useful report."
            return data
    return fallback


def decision_report_history(limit: int = 20) -> list[dict]:
    history = []
    for report in report_files()[:limit]:
        data = load_json(report, {})
        summary = data.get("summary") or {}
        history.append(
            {
                "name": report.name,
                "modified": path_modified_text(report),
                "accepted": summary.get("accepted", len(data.get("accepted") or [])),
                "rejected": summary.get("rejected", len(data.get("rejected") or [])),
                "new_listings": summary.get("new_listings", summary.get("collected_after_filters", 0)),
                "detail_checked": summary.get("detail_checked", 0),
                "useful": bool(data.get("accepted") or data.get("rejected")),
            }
        )
    return history


def older_accepted_listings(limit: int = 40) -> list[dict]:
    listings: list[dict] = []
    seen_urls: set[str] = set()
    for report in report_files():
        data = load_json(report, {})
        report_time = path_modified_text(report)
        for item in data.get("accepted") or []:
            url = str(item.get("url") or "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            enriched = dict(item)
            enriched["_report_name"] = report.name
            enriched["_report_time"] = report_time
            listings.append(enriched)
            if len(listings) >= limit:
                return listings
    return listings


def recent_phone_listings(limit: int = 80) -> list[dict]:
    listings: list[dict] = []
    seen_urls: set[str] = set()
    for report in report_files():
        data = load_json(report, {})
        report_time = path_modified_text(report)
        for item in (data.get("accepted") or []) + (data.get("rejected") or []):
            if not str(item.get("phone_number") or "").strip():
                continue
            url = str(item.get("url") or "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            enriched = dict(item)
            enriched["_report_name"] = report.name
            enriched["_report_time"] = report_time
            listings.append(enriched)
            if len(listings) >= limit:
                return listings
    return listings


def manual_check_candidates(rejected: list[dict], limit: int = 8) -> list[dict]:
    candidates = []
    for item in rejected:
        score = item.get("owner_score")
        reasons = " ".join(str(reason).lower() for reason in item.get("exclusion_reasons") or [])
        seller_posts = item.get("seller_target_post_count") or 0
        if isinstance(score, int) and score <= 8 and seller_posts <= 3:
            candidates.append(item)
        elif "owner wording found" in reasons:
            candidates.append(item)
    candidates.sort(key=lambda item: (item.get("owner_score", 99), -(item.get("price_usd") or 0)))
    return candidates[:limit]


def listing_age_seconds(item: dict) -> int:
    text = str(item.get("created") or "").strip().lower()
    if not text:
        return 10**9
    if "just now" in text:
        return 0
    if text == "yesterday":
        return 24 * 3600
    match = re.search(r"(\d+)\s*(minute|hour|day|week|month|year)", text)
    if not match:
        return 10**9
    amount = safe_int(match.group(1), 999999)
    unit = match.group(2)
    multipliers = {
        "minute": 60,
        "hour": 3600,
        "day": 24 * 3600,
        "week": 7 * 24 * 3600,
        "month": 30 * 24 * 3600,
        "year": 365 * 24 * 3600,
    }
    return amount * multipliers.get(unit, 10**9)


def listing_sort_key(item: dict) -> tuple[str, int, str]:
    return (
        str(item.get("city") or "Unknown").lower(),
        listing_age_seconds(item),
        str(item.get("title") or "").lower(),
    )


def newest_sort_key(item: dict) -> tuple[int, str]:
    return (listing_age_seconds(item), str(item.get("title") or "").lower())


def sorted_listings(items: list[dict]) -> list[dict]:
    return sorted(items, key=listing_sort_key)


def grouped_listing_sections(items: list[dict], badge: str, *, exclude_urls: set[str] | None = None) -> str:
    exclude_urls = exclude_urls or set()
    groups: dict[str, list[dict]] = {}
    for item in items:
        if str(item.get("url") or "") in exclude_urls:
            continue
        city = str(item.get("city") or "Unknown area")
        groups.setdefault(city, []).append(item)
    if not groups:
        return "<p class='muted'>No listings found in this section yet.</p>"

    sections: list[str] = []
    for city in sorted(groups):
        city_items = sorted(groups[city], key=newest_sort_key)
        count = len(city_items)
        cards = "\n".join(listing_card(item, badge) for item in city_items)
        sections.append(
            f"""<section class="area-section">
              <div class="area-heading"><h3>{html.escape(city)}</h3><span class="pill">{count} listing{'s' if count != 1 else ''}</span></div>
              <div class="grid">{cards}</div>
            </section>"""
        )
    return "\n".join(sections)


def whatsapp_number(raw_phone: str) -> str:
    digits = "".join(ch for ch in raw_phone if ch.isdigit())
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("961"):
        return digits
    if digits.startswith("0") or len(digits) in {7, 8}:
        return "961" + digits.lstrip("0")
    return digits


def whatsapp_message(item: dict) -> str:
    title = str(item.get("title") or "your property")
    url = str(item.get("url") or "")
    return (
        "Hi sir, I hope you are well. "
        f"I saw your apartment listing on OLX: {title}. "
        "Would you be open to working with a broker if I have a serious client for it? "
        f"{url}"
    ).strip()


def safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def start_scan(created_within_days: int) -> bool:
    if SCAN_LOCK.locked():
        return False
    created_within_days = max(0, min(created_within_days, 31))
    thread = threading.Thread(target=run_scan, args=(created_within_days,), daemon=True)
    thread.start()
    return True


def run_scan(created_within_days: int) -> None:
    with SCAN_LOCK:
        command_label = f"last{created_within_days}" if created_within_days else "latest"
        started = time.time()
        output = ""
        returncode = 1
        try:
            state = load_json(WEB_STATE_PATH, {})
            state.update(
                {
                    "running": True,
                    "command": command_label,
                    "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "finished_at": "",
                    "duration_seconds": 0,
                    "returncode": None,
                }
            )
            save_json(WEB_STATE_PATH, state)

            command = [sys.executable, str(ALERT_SCRIPT), "--dry-run", "--mark-seen-on-dry-run"]
            if created_within_days:
                command.extend(["--created-within-days", str(created_within_days)])
                command.extend(["--max-pages", "4" if created_within_days >= 21 else "3"])
                command.extend(["--max-candidates", "140" if created_within_days >= 21 else "80"])

            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(ALERT_SCRIPT.parent),
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=1800,
                )
                output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
                returncode = completed.returncode
            except subprocess.TimeoutExpired as exc:
                output = f"Scan timed out after 30 minutes.\n{exc}"
                returncode = 124
            except Exception as exc:
                output = f"Scan failed before it could finish.\n{type(exc).__name__}: {exc}"
                returncode = 125
                log_error(f"Scan thread failed for {command_label}", exc)

            try:
                WEB_SCAN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                with WEB_SCAN_LOG_PATH.open("a", encoding="utf-8") as handle:
                    handle.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} command={command_label} returncode={returncode} ---\n")
                    handle.write(output.rstrip() + "\n")
            except OSError as exc:
                log_error("Could not write web scan log", exc)
        finally:
            state = load_json(WEB_STATE_PATH, {})
            state.update(
                {
                    "running": False,
                    "command": command_label,
                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "duration_seconds": int(time.time() - started),
                    "returncode": returncode,
                    "last_output": output[-12000:] if output else "Scan ended without output.",
                }
            )
            save_json(WEB_STATE_PATH, state)


def money_text(value) -> str:
    return f"${value:,}" if isinstance(value, int) and value > 0 else "price unknown"


def listing_card(item: dict, badge: str = "") -> str:
    title = html.escape(str(item.get("title") or "Untitled"))
    city = html.escape(str(item.get("city") or "unknown"))
    price = money_text(item.get("price_usd"))
    raw_phone = str(item.get("phone_number") or "").strip()
    phone = html.escape(raw_phone or "phone not shown")
    url = html.escape(str(item.get("url") or "#"))
    seller = html.escape(str(item.get("seller_name") or "unknown seller"))
    score = html.escape(str(item.get("owner_score", "n/a")))
    created = html.escape(str(item.get("created") or item.get("_report_time") or "unknown age"))
    purpose = html.escape(str(item.get("purpose") or "listing"))
    reasons = ", ".join(str(reason) for reason in (item.get("exclusion_reasons") or [])[:3])
    reason_html = f"<div class='reason'>Why: {html.escape(reasons)}</div>" if reasons else ""
    wa_number = whatsapp_number(raw_phone)
    wa_text = quote(whatsapp_message(item))
    phone_digits = "+" + wa_number if wa_number.startswith("961") else wa_number
    phone_link = f"<a class='mini' href='tel:{html.escape(phone_digits)}'>Call</a>" if wa_number else ""
    whatsapp_link = f"<a class='mini whatsapp' href='https://wa.me/{html.escape(wa_number)}?text={wa_text}' target='_blank' rel='noreferrer'>WhatsApp Business</a>" if wa_number else ""
    contact_html = (
        f"<div class='contact-row'><span class='muted'>Owner number</span><a class='contact-number' href='https://wa.me/{html.escape(wa_number)}?text={wa_text}' target='_blank' rel='noreferrer'>{phone}</a></div>"
        if wa_number
        else "<div class='contact-row'><span class='muted'>Owner number not shown by OLX yet. Open OLX to check the listing.</span></div>"
    )
    badge_html = f"<span class='pill'>{html.escape(badge)}</span>" if badge else ""
    return f"""<article class="card listing">
      <div>{badge_html}<strong>{title}</strong>
      <span class="muted">{city} - {purpose} - {price} - {created}</span><br>
      <span class="muted">{seller} - {phone} - score {score}</span></div>
      {contact_html}
      {reason_html}
      <div class="listing-actions">
        <a class="mini primary" href="{url}" target="_blank" rel="noreferrer">Open OLX</a>
        {phone_link}
        {whatsapp_link}
      </div>
    </article>"""


def contact_demo_card() -> str:
    return listing_card(
        {
            "title": "WhatsApp contact preview",
            "city": "Fanar",
            "purpose": "demo",
            "price_usd": 0,
            "created": "example",
            "seller_name": "Owner",
            "phone_number": "03 123 456",
            "owner_score": "demo",
            "url": "https://www.olx.com.lb/",
            "exclusion_reasons": ["example only"],
        },
        "Preview",
    )


def history_card(item: dict) -> str:
    name = html.escape(str(item.get("name") or "report.json"))
    modified = html.escape(str(item.get("modified") or "unknown time"))
    accepted = html.escape(str(item.get("accepted", 0)))
    rejected = html.escape(str(item.get("rejected", 0)))
    new_listings = html.escape(str(item.get("new_listings", 0)))
    detail_checked = html.escape(str(item.get("detail_checked", 0)))
    useful = "Useful" if item.get("useful") else "Empty"
    useful_class = "ok" if item.get("useful") else "muted"
    return f"""<a class="card history-item" href="/reports/{name}" target="_blank" rel="noreferrer">
      <div><strong>{modified}</strong><br><span class="muted">{name}</span></div>
      <div class="history-counts">
        <span class="chip {useful_class}">{useful}</span>
        <span class="chip">{accepted} accepted</span>
        <span class="chip">{rejected} rejected</span>
        <span class="chip">{new_listings} new</span>
        <span class="chip">{detail_checked} opened</span>
      </div>
    </a>"""


def css() -> str:
    return """
    :root {
      color-scheme: light;
      --page:#f5f1e9;
      --paper:#fffaf1;
      --ink:#1d2324;
      --muted:#6f7470;
      --line:rgba(29,35,36,.12);
      --accent:#0f3d35;
      --accent-2:#c69b5b;
      --danger:#b64747;
      --shadow:0 24px 70px rgba(44,35,24,.13);
    }
    * { box-sizing: border-box; }
    body {
      margin:0;
      min-height:100vh;
      font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",sans-serif;
      background:radial-gradient(circle at top left,rgba(198,155,91,.28),transparent 34rem),linear-gradient(180deg,#fbf7ef 0%,var(--page) 48%,#eee6d8 100%);
      color:var(--ink);
    }
    main { width:min(1120px,100%); margin:0 auto; padding:22px 16px 104px; }
    .topbar { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:18px; padding:10px 4px; }
    .brand { display:flex; align-items:center; gap:12px; font-weight:850; letter-spacing:-.02em; }
    .logo { width:46px; height:46px; display:grid; place-items:center; border-radius:16px; background:var(--accent); color:#fff7e8; box-shadow:0 14px 35px rgba(15,61,53,.18); font-size:20px; }
    .hero { position:relative; overflow:hidden; display:grid; grid-template-columns:minmax(0,1.05fr) minmax(310px,.95fr); gap:24px; align-items:center; padding:28px; border-radius:34px; background:linear-gradient(135deg,rgba(255,250,241,.96),rgba(240,231,216,.95)); box-shadow:var(--shadow); border:1px solid rgba(255,255,255,.78); }
    .hero:after { content:""; position:absolute; width:360px; height:360px; right:-130px; top:-160px; border-radius:50%; background:rgba(198,155,91,.18); pointer-events:none; }
    .hero-copy,.search-panel { position:relative; z-index:1; }
    h1 { margin:10px 0 12px; font-size:clamp(38px,8vw,68px); line-height:.92; letter-spacing:-.07em; }
    h2 { margin:34px 0 8px; font-size:23px; letter-spacing:-.03em; }
    p { color:var(--muted); line-height:1.55; }
    form { margin:0; }
    button,.button,input { width:100%; border:0; border-radius:18px; padding:15px 16px; font-size:17px; font-weight:800; font-family:inherit; }
    button,.button { display:block; color:#fff8ea; background:var(--accent); text-align:center; text-decoration:none; cursor:pointer; transition:transform .16s ease, box-shadow .16s ease, opacity .16s ease; }
    button:hover,.button:hover { transform:translateY(-1px); box-shadow:0 14px 34px rgba(15,61,53,.18); }
    button.secondary { color:var(--ink); background:#efe7d9; border:1px solid var(--line); }
    button.search { min-height:92px; font-size:24px; letter-spacing:.02em; background:linear-gradient(135deg,#0f3d35,#174f44); box-shadow:0 20px 48px rgba(15,61,53,.24); }
    button:disabled { opacity:.55; cursor:not-allowed; }
    .search-panel { padding:18px; border-radius:28px; background:rgba(255,255,255,.58); border:1px solid rgba(255,255,255,.86); box-shadow:inset 0 0 0 1px rgba(29,35,36,.04); backdrop-filter:blur(12px); }
    .actions { display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); gap:10px; margin-top:10px; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:14px; margin-top:16px; }
    .card { background:rgba(255,250,241,.86); border:1px solid rgba(29,35,36,.09); border-radius:26px; padding:18px; box-shadow:0 14px 42px rgba(44,35,24,.08); }
    .metric { font-size:31px; font-weight:900; letter-spacing:-.04em; color:var(--accent); }
    .pill { display:inline-flex; align-items:center; gap:7px; padding:8px 11px; border-radius:999px; background:#eee3d1; color:#514c43; font-size:13px; font-weight:800; border:1px solid rgba(29,35,36,.07); }
    .eyebrow { color:var(--accent-2); font-weight:900; text-transform:uppercase; letter-spacing:.11em; font-size:12px; }
    .hint,.reason,.muted { color:var(--muted); }
    .hint { margin:8px 2px 0; font-size:14px; }
    .ok { color:var(--accent); }
    .warn { color:var(--accent-2); }
    .bad { color:var(--danger); }
    .listing { display:flex; flex-direction:column; gap:12px; color:inherit; text-decoration:none; position:relative; overflow:hidden; }
    .listing strong { display:block; margin:8px 0 7px; font-size:18px; line-height:1.16; letter-spacing:-.02em; }
    .listing-actions { display:grid; grid-template-columns:repeat(auto-fit,minmax(112px,1fr)); gap:8px; margin-top:4px; }
    .mini { display:block; padding:10px 12px; border-radius:14px; background:#efe7d9; color:var(--ink); text-align:center; text-decoration:none; font-weight:800; border:1px solid var(--line); }
    .mini.primary { color:#fff8ea; background:var(--accent); border-color:transparent; }
    .mini.whatsapp { color:#082c1a; background:#d9f4df; border-color:#b9e7c3; }
    .contact-row { display:grid; grid-template-columns:1fr; gap:8px; padding:11px; border-radius:18px; background:rgba(15,61,53,.06); border:1px solid rgba(15,61,53,.12); }
    .contact-number { color:var(--accent); font-weight:850; overflow-wrap:anywhere; }
    .history-list { display:grid; gap:10px; margin-top:12px; }
    .history-item { display:grid; grid-template-columns:1fr auto; gap:12px; align-items:center; color:inherit; text-decoration:none; }
    .history-counts { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:7px; }
    .chip { display:inline-flex; padding:6px 9px; border-radius:999px; background:#efe7d9; color:var(--muted); font-size:13px; font-weight:800; }
    .area-section { margin-top:16px; }
    .area-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:0 4px; }
    .area-heading h3 { margin:0; font-size:18px; letter-spacing:-.02em; }
    pre { white-space:pre-wrap; overflow-wrap:anywhere; background:#211f1b; border-radius:20px; padding:16px; color:#f6ead8; max-height:420px; overflow:auto; border:1px solid rgba(255,255,255,.08); }
    .section-intro { margin-top:0; max-width:760px; }
    .bottom-search { position:fixed; left:0; right:0; bottom:0; padding:10px 14px max(10px,env(safe-area-inset-bottom)); background:linear-gradient(180deg,rgba(245,241,233,0),rgba(245,241,233,.92) 28%,#f5f1e9); backdrop-filter:blur(14px); z-index:20; }
    .bottom-search form { width:min(1120px,100%); margin:0 auto; }
    .bottom-search button { min-height:58px; border-radius:20px; }
    @media (max-width: 760px) { .hero { grid-template-columns:1fr; padding:20px; } .topbar { align-items:flex-start; } }
    @media (max-width: 620px) { main { padding-inline:12px; } .grid { grid-template-columns:1fr; } .actions { grid-template-columns:1fr 1fr; } .history-item { grid-template-columns:1fr; } .history-counts { justify-content:flex-start; } button.search { min-height:84px; font-size:21px; } }
    """


def page_shell(title: str, body: str, *, refresh: bool = False) -> bytes:
    refresh_tag = '<meta http-equiv="refresh" content="10">' if refresh else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="{APP_NAME}">
  <meta name="theme-color" content="#f5f1e9">
  {refresh_tag}
  <link rel="manifest" href="/manifest.json">
  <link rel="apple-touch-icon" href="/icon.svg">
  <title>{html.escape(title)}</title>
  <style>{css()}</style>
</head>
<body><main>{body}</main></body>
</html>""".encode("utf-8")


def home_page() -> bytes:
    summary = status_summary()
    web_state = summary.get("web") or {}
    running = bool(web_state.get("running")) or SCAN_LOCK.locked()
    report = latest_decision_report()
    accepted = sorted_listings(report.get("accepted") or [])
    rejected = report.get("rejected") or []
    manual_checks = manual_check_candidates(rejected)
    older_listings = sorted_listings(older_accepted_listings())
    phone_listings = sorted_listings(recent_phone_listings())
    latest_urls = {str(item.get("url") or "") for item in accepted}
    report_summary = report.get("summary") or {}
    report_note = report.get("_note", "")
    output = html.escape(str(web_state.get("last_output") or "No web scan output yet."))
    cards = grouped_listing_sections(accepted[:60], "Best match") if accepted else "<p class='muted'>No accepted owner listings in the latest report.</p>"
    manual_cards = grouped_listing_sections(sorted_listings(manual_checks), "Manual check") if manual_checks else "<p class='muted'>No close calls right now. The filter is seeing mostly agency listings.</p>"
    older_cards = grouped_listing_sections(older_listings, "Older match", exclude_urls=latest_urls)
    phone_cards = grouped_listing_sections(phone_listings, "Phone found") if phone_listings else "<p class='muted'>No phone numbers found in recent reports yet.</p>"
    contact_preview = contact_demo_card() if not any(item.get("phone_number") for item in accepted + manual_checks) else ""
    history_cards = "\n".join(history_card(item) for item in decision_report_history(20))
    search_button_text = "Searching..." if running else "SEARCH NEW LISTINGS NOW"
    search_disabled = "disabled" if running else ""
    status_text = "Live scan" if running else "Ready"
    returncode = web_state.get("returncode", "-")
    body = f"""
    <nav class="topbar">
      <div class="brand"><div class="logo">PR</div><div>Property Robot<br><span class="muted">Private owner finder</span></div></div>
      <span class="pill">Status: {status_text}</span>
    </nav>
    <section class="hero">
      <div class="hero-copy">
        <span class="eyebrow">Daily control panel</span>
        <h1>{APP_NAME}</h1>
        <p>Search fresh owner listings from your iPhone, review older matches, and contact owners faster with a cleaner workflow that does not depend on Telegram.</p>
      </div>
      <div class="search-panel">
        <form method="post" action="/scan">
          <input type="hidden" name="days" value="0">
          <button class="search" type="submit" {search_disabled}>{search_button_text}</button>
        </form>
        <p class="hint">Best daily button: searches only new unseen listings, then marks them as seen so you do not review the same homes forever.</p>
        <div class="actions">
          <form method="post" action="/scan"><input type="hidden" name="days" value="7"><button class="secondary" type="submit" {search_disabled}>Search Last 7 Days</button></form>
          <form method="post" action="/scan"><input type="hidden" name="days" value="14"><button class="secondary" type="submit" {search_disabled}>Search Last 14 Days</button></form>
        </div>
      </div>
    </section>
    <section class="grid">
      <div class="card"><div class="metric">{summary['seen_count']}</div><p>Seen listings</p></div>
      <div class="card"><div class="metric">{report_summary.get('accepted', 0)}</div><p>Accepted latest report</p></div>
      <div class="card"><div class="metric warn">{len(manual_checks)}</div><p>Worth manual check</p></div>
      <div class="card"><div class="metric">{len(older_listings)}</div><p>Older accepted found</p></div>
      <div class="card"><div class="metric">{len(phone_listings)}</div><p>Phone numbers found</p></div>
      <div class="card"><div class="metric">{summary['queued_telegram_messages']}</div><p>Queued Telegram messages</p></div>
      <div class="card"><div class="metric {'bad' if returncode not in (None, 0, '-') else 'ok'}">{returncode}</div><p>Last web scan code</p></div>
    </section>
    <h2>Latest Owner Matches</h2>
    <div>{cards}</div>
    <h2>Older Owner Listings Found</h2>
    <p class="section-intro">Previously accepted owner-like listings from older scans. Newest historical matches appear first, and duplicates are hidden.</p>
    <div>{older_cards}</div>
    <h2>Phone Numbers Found</h2>
    <p class="section-intro">Recent listings where the robot found a phone number, grouped by area and sorted newest upload to oldest. Verify the listing before contacting because this can include agency rows too.</p>
    <div>{phone_cards}</div>
    <h2>Worth Manual Check</h2>
    <p class="section-intro">These are not strong enough to auto-accept, but they have low owner-risk scores or owner-like wording. Useful when the market is quiet.</p>
    <div>{manual_cards}</div>
    {"<h2>Contact Preview</h2><p class='muted'>When OLX exposes an owner phone number, the card gets a WhatsApp Business button like this.</p><div class='grid'>" + contact_preview + "</div>" if contact_preview else ""}
    <h2>Scan History</h2>
    <p class="section-intro">Recent scan reports, newest first. Tap a row to open the raw decision log if you want to inspect every accepted/rejected listing.</p>
    <div class="history-list">{history_cards or "<p class='muted'>No scan history yet.</p>"}</div>
    <h2>System Status</h2>
    <div class="card">
      <p>Last robot run: <strong>{html.escape(str(summary['last_robot_run']))}</strong></p>
      <p>Web scan: <strong>{html.escape(str(web_state.get('command', 'none')))}</strong> - started {html.escape(str(web_state.get('started_at', 'never')))} - finished {html.escape(str(web_state.get('finished_at', '')))}</p>
      <p>Latest report: <strong>{html.escape(str(report.get('_path', 'none')))}</strong></p>
      {"<p class='warn'>" + html.escape(str(report_note)) + "</p>" if report_note else ""}
    </div>
    <h2>Last Scan Output</h2>
    <pre>{output}</pre>
    <div class="bottom-search">
      <form method="post" action="/scan">
        <input type="hidden" name="days" value="0">
        <button class="search" type="submit" {search_disabled}>{search_button_text}</button>
      </form>
    </div>
    """
    return page_shell(APP_NAME, body, refresh=running)


class RobotServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class RobotHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/manifest.json":
                self.send_json(
                    {
                        "name": APP_NAME,
                        "short_name": "Robot",
                        "start_url": "/",
                        "display": "standalone",
                        "background_color": "#f5f1e9",
                        "theme_color": "#0f3d35",
                        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
                    }
                )
                return
            if parsed.path == "/icon.svg":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/svg+xml")
                self.end_headers()
                self.wfile.write(
                    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#0f3d35"/><circle cx="382" cy="130" r="52" fill="#c69b5b"/><path d="M114 332c48-112 95-168 142-168s94 56 142 168" fill="none" stroke="#fff7e8" stroke-width="38" stroke-linecap="round"/><path d="M172 312h168" stroke="#c69b5b" stroke-width="28" stroke-linecap="round"/></svg>'
                )
                return
            if parsed.path.startswith("/reports/"):
                self.send_report_file(parsed.path)
                return
            if parsed.path == "/status.json":
                self.send_json(status_summary())
                return
            self.send_html(home_page())
        except Exception as exc:
            log_error(f"GET failed path={self.path}", exc)
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Temporary web app error. Try refresh.")

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            fields = self.read_form()
            if parsed.path == "/scan":
                days = safe_int(fields.get("days", ["0"])[0], 0)
                started = start_scan(days)
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/?started=1" if started else "/?busy=1")
                self.end_headers()
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            log_error(f"POST failed path={self.path}", exc)
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Temporary web app error. Try refresh.")

    def read_form(self) -> dict[str, list[str]]:
        length = safe_int(self.headers.get("Content-Length", "0"), 0)
        body = self.rfile.read(max(0, min(length, 10_000))).decode("utf-8", errors="replace")
        return parse_qs(body)

    def send_html(self, content: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, payload) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_report_file(self, path: str) -> None:
        name = Path(unquote(path.removeprefix("/reports/"))).name
        report_path = REPORTS_DIR / name
        if not name.startswith("owner_alert_decisions_") or report_path.suffix != ".json" or not report_path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            content = report_path.read_bytes()
        except OSError as exc:
            log_error(f"Could not read report file {report_path}", exc)
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        try:
            Path("logs").mkdir(exist_ok=True)
            with Path("logs/web_robot_app.access.log").open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.client_address[0]} {format % args}\n")
        except OSError:
            pass


def main() -> int:
    load_env_files()
    server = RobotServer((HOST, PORT), RobotHandler)
    lan_url = f"http://{local_ip_address()}:{PORT}/"
    print(f"{APP_NAME} is running.")
    print(f"Open on this PC: http://127.0.0.1:{PORT}/")
    print(f"Open on iPhone:  {lan_url}")
    print("No PIN is required. Keep this window running. In Safari: Share -> Add to Home Screen.")
    try:
        server.serve_forever()
    except Exception as exc:
        log_error("Web server stopped unexpectedly", exc)
        raise
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
