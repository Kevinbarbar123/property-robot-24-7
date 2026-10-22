# Lebanon Property Listing Bot

This folder has two tools:

- `property_bot.py`: makes CSV and Markdown reports for the selected Metn target areas around Fanar.
- `telegram_owner_alert.py`: sends daily Telegram alerts for new listings that look owner-posted.
- `telegram_command_bot.py`: keeps a Telegram command listener running so you can request a fresh scan whenever you want.
- `web_robot_app.py`: private iPhone-friendly web control panel for running scans without Telegram.

The first source is OLX Lebanon.

## One-Time Report

```powershell
python .\property_bot.py
```

The report tool writes:

- `reports/property_listings_*.csv`
- `reports/property_report_*.md`

Useful options:

```powershell
python .\property_bot.py --max-pages 2
python .\property_bot.py --top 25
python .\property_bot.py --min-price 75000
python .\property_bot.py --include-nearby
```

## Telegram Owner Alerts

Telegram bots cannot message a phone number directly. Create a Telegram bot with `@BotFather`, send your bot any message from your Telegram account, then get your chat ID.

1. Copy `.env.example` to `.env`.
2. Put your bot token in `TELEGRAM_BOT_TOKEN`.
3. After messaging your bot, run:

Tip: keep real secrets in `.env.private` (loaded automatically) and leave `.env` as placeholders so it is safer to share.

```powershell
python .\telegram_owner_alert.py --show-chat-id
```

4. Put that value in `TELEGRAM_CHAT_ID`.

Test without sending:

```powershell
python .\telegram_owner_alert.py --dry-run --max-pages 1
```

Prime the state so it does not send all old listings on the first real run:

```powershell
python .\telegram_owner_alert.py --dry-run --mark-seen-on-dry-run
```

Run normally:

```powershell
python .\telegram_owner_alert.py
```

Or use the helper script (appends to `logs/`):

```powershell
.\run_telegram_owner_alert.ps1
```

## Telegram On-Demand Commands

After `.env` has `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, keep the command listener running:

```powershell
python .\telegram_command_bot.py
```

Or use the helper script (appends to `logs/`):

```powershell
.\run_telegram_command_bot.ps1
```

Then message your Telegram bot:

```text
/latest
```

The bot will scan OLX again, inspect the sellers, send only new likely-owner listings, and mark those listings as seen. The daily alert and `/latest` share the same state file, so old listings do not repeat unless you delete `data/telegram_owner_alert_state.json`.

For the first catch-up scan, use:

```text
/last14
```

That scans only listings OLX labels as posted within the last 14 days, sends new likely-owner matches, and marks them as seen. After that, `/latest` and the 11 PM daily run continue day by day.

Each listing message includes:

- Confidence label and why it was sent
- Listing title and city
- Price, sqm, and USD/sqm
- OLX account name
- Phone number from the listing description first, then the current listing's seller profile when OLX exposes it, or `not shown by OLX`
- Listing link

Other commands:

```text
/latest
/status
/help
/last7
/last14
/last21
/maybe
/areas
/why <olx link or text>
/blacklist <seller/phone/url>
/hot
/prices
/agencies
/reposts
/export
/schedule HH:MM
/contacted <olx link>
/interested <olx link>
/notinterested <olx link>
/wrongnumber <olx link>
/leads
/remind <olx link> <note>
/reminders
/learnowner <phrase>
/summary
/ask <question>
```

## Private iPhone Web App

If Telegram is blocked or flaky, run the private web control panel on this PC:

```powershell
.\run_web_robot_app.ps1
```

Then open `logs/web_robot_app.out.log` to see the iPhone URL:

```powershell
Get-Content .\logs\web_robot_app.out.log -Tail 20
```

Open the `http://<your-pc-ip>:8787/` URL in Safari on your iPhone, then use Share -> Add to Home Screen. No PIN/password is required.

The web app runs scans with `--dry-run --mark-seen-on-dry-run`, so it shows the owner matches in the browser and still updates the shared seen-list state. Keep the PC awake and on the same Wi-Fi as the iPhone.

If the iPhone cannot open the URL, Windows Firewall may be blocking port 8787. Run this once and accept the administrator prompt:

```powershell
.\allow_web_robot_firewall.ps1
```

## Professional Owner Lead Pipeline

The modular pro pipeline keeps the existing Telegram bot intact and adds SQLite storage, duplicate protection, CSV/JSON exports, safer cached requests, and a Streamlit dashboard.

Run a pro scrape:

```powershell
python .\main.py
```

Run without Telegram alerts:

```powershell
python .\main.py --dry-run --no-alerts
```

Open the dashboard:

```powershell
streamlit run .\dashboard.py
```

Main files:

- `config.yaml` controls target areas, safe delays, max pages, exports, and alert threshold.
- `scraper.py` reuses the existing OLX collector with cache and delays.
- `scorer.py` creates 0-100 owner and broker scores.
- `database.py` writes SQLite tables for listings, contacts, scrape runs, duplicates, and errors.
- `notifier.py` sends Telegram alerts only for new likely-owner leads.
- `dashboard.py` shows totals, new leads, likely owners, filters, charts, and CSV export.

## Railway 24/7 Hosting

See [CLOUD_INDEPENDENCE.md](CLOUD_INDEPENDENCE.md) for current hosting blockers, secure migration, staging safeguards, and the laptop-to-cloud cutover checklist. Docker intentionally starts inactive until existing history is migrated; setting credentials alone no longer activates a second listener. Do not turn off the laptop until the cloud checks pass.

### Independent owner scans (no Codex credits)

Core OLX searches, owner scoring, history, and Telegram delivery run in Python without Codex or an AI API. The optional AI question helper falls back to built-in answers if the API is unavailable.

On Windows, run `powershell -NoProfile -ExecutionPolicy Bypass -File .\install_owner_schedule.ps1` once. The hidden `PropertyOwnerIndependentScan` task checks hourly and at sign-in, scans daily, and waits six hours after failures. It requires the computer to be awake and the user signed in. No terminal windows are opened and it does not start a second Telegram command poller.

On Railway, the entrypoint also starts `owner_scheduler.py` automatically. Set `OWNER_SCAN_INTERVAL_HOURS` (default `24`, minimum `6`) or `OWNER_SCAN_ENABLED=false` to disable the cloud schedule. Keep only one scheduled host active to avoid duplicate alerts across different databases. Persist `/app/data` on a Railway volume. Railway hosting must remain active for laptop-independent operation; Codex credits are unrelated to this Python process.

Inspect `data/owner_schedule.json`, `data/owner_scan_health.json`, and `logs/owner_scheduler.log` for the real status. HTTP challenges, access restrictions, empty responses, and unrecognized search data are failures, not successful zero-result scans. A source failure preserves seen history, returns exit code 3, sends at most one source-health notice per day, and pauses requests for six hours. Exit code 4 means another scan holds the lock. Source access restrictions require platform-permitted access; the bot does not solve or bypass them.

This repo is Railway-ready. Railway starts `railway_start.py`, which runs:

- the web app on Railway's `PORT`
- the Telegram command bot in a background supervisor

Set these Railway variables:

```text
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Recommended Railway setup:

1. Deploy from the GitHub repo.
2. Add the variables above in Railway.
3. Add the required Railway volume at `/app/data`, keep one replica, and follow the private history migration checklist.
4. Stop the laptop listener/schedules at cutover, then set `BOT_ACTIVE=true` and validate Telegram and the public iPhone website.

## Owner Filter

Telegram commands use polling and require no webhook. A configured webhook prevents polling even if outgoing automatic alerts still work. The listener checks this at startup and records the problem in `data/telegram_command_bot_health.json`. The supervisors retry conflicts after 15 minutes without automatically deleting webhooks. An OS file lock prevents duplicate listeners in this workspace. `/search`, `Search now`, `/latest`, and `/scan` all start an on-demand scan.

The Telegram alert now focuses on the selected Metn OLX areas for both sale and rent listings: Fanar, Mar Roukoz, Broumana, Beit Mery, Jdeideh, Rawda, Bsalim, Mezher, Biakout, Sabtieh, Dekwaneh, Mkalles, Sin El Fil, Jisr El Bacha, Horsh Tabet, Baouchrieh, Sed El Bauchrieh, Bourj Hammoud, Dawra, Zalka, Jal El Dib, Antelias, Dbayeh, Nahr El Mott, Kornet Chehwan, Ain Saadeh, Mansourieh, Monteverde, Roumieh, Tilal Ain Saadeh, Ain Najem, Naccache, Awkar, Mtayleb, Beit Chaar, Nabay, Baabdat, Rabieh, Beit Misk, Majzoub, Wata Amaret Chalhoub, and Qennabet Broumana.

It uses an owner-likelihood risk score (lower is better; default maximum 4), with hard rejection for explicit agency profiles, business seller types, agency wording, and heavy property-posting activity. Classification is probabilistic, not proof of ownership.

Total OLX account ads across all categories are only a soft risk signal (2 points above 3 ads, 3 points above 8), not proof that someone is a broker. Automatic broker learning uses seller UUIDs and phone numbers rather than personal names; explicit `/blacklist` entries remain respected.

The September 28 filter revision rechecks non-agency rejections reported in the last 10 days when those listings appear in the normal scan. Already accepted listings are excluded; completed rechecks are persisted in the existing state file. It does not clear history or bypass source restrictions. New dry-run reports do not suppress future live alerts.

To keep scans fast, the bot first reads OLX's search-page listing data for seller type, coordinates, account name, description, and reference fields. It only opens detail pages for listings that already look private enough to confirm phone/account details.

The score uses these checks:

- The listing is apartment-like.
- The listing is inside the selected target-city list.
- The seller or phone has no more than 3 apartment sale/rent posts in the selected target areas.
- Agency names, agency ids, agent codes, and reference codes add risk.
- Owner wording and exposed phone numbers improve confidence.

You can tighten or loosen the owner scoring:

```powershell
python .\telegram_owner_alert.py --owner-score-threshold 2
python .\telegram_owner_alert.py --owner-score-threshold 6
```

Each alert run writes a decision log under `reports/` so you can see which listings were accepted or rejected and why.

## Preventive source recovery

Normal scans still use public HTTP pages. `reliability.json` controls detection, bounded retries, conservative pacing, cache expiry, and extraction selectors. Request starts are shared across workers and processes; CAPTCHA, 403, and 429 stop further requests for the affected OLX source. A plain 403 is an access restriction, not automatically a CAPTCHA. Retry-After deadlines survive restarts.

`data/recovery.sqlite3` stores completed search pages, detail results, original scan options, notification receipts, and recovery incidents. Unfinished jobs resume with the same criteria. Existing seen-listing history is retained. `/status` reports recovery progress; `/resume` continues eligible saved work; `/verify` issues a private manual verification link when CAPTCHA is detected.

For phone verification, the web app and scanner must share the same host and data directory. Set `RECOVERY_PUBLIC_URL=https://your-service-domain` (Railway can derive this from `RAILWAY_PUBLIC_DOMAIN`). The link displays only the bot's OLX tab. Tap **Open verification session**, solve the website challenge yourself, and the worker resumes after recognizing valid OLX data. Links are single-use, private sessions expire after 15 minutes idle or one hour total, and verification access is revoked on completion. A missing browser or public URL leaves progress paused.

Install browser recovery locally with `python -m pip install -r requirements-recovery.txt`. Windows uses installed Google Chrome with a separate profile under `data/verification-browser`; it does not use your everyday profile. Railway's Docker image installs Chromium and Xvfb, sets `RECOVERY_BROWSER_CHANNEL` to an empty value, and persists the profile on the `/app/data` volume. No browser is launched during normal HTTP scans. Browser process access is never exposed publicly.

Recovery never solves CAPTCHAs automatically or changes identities/proxies to evade restrictions. A separate phone visit to OLX cannot verify the bot's session. If verification fails, the job remains paused. Repeated connection failures stop automatic recovery after three cycles; `/resume` enables a deliberate retry once the deadline permits it. Page-format changes require parser review before `/resume`.

Acknowledged Telegram chunks are not resent. If the send succeeds but its reply is lost, Telegram provides no idempotency key; the bot records an uncertain delivery and avoids automatic resending. `/status` reports those receipts for manual review. Old outbox entries retain their existing behavior; new entries carry delivery identities.

Run simulated recovery and regression checks with `python -m unittest discover -q`. Tests use temporary databases and mocked responses, never real CAPTCHA requests. Keep one active scanning host and Telegram listener. Do not disable the laptop tasks until the cloud service, persistent data migration, and recovery page have been validated.
