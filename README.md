# Lebanon Property Listing Bot

This folder has two tools:

- `property_bot.py`: makes CSV and Markdown reports for the selected Metn target areas around Fanar.
- `telegram_owner_alert.py`: sends daily Telegram alerts for new listings that look owner-posted.
- `telegram_command_bot.py`: keeps a Telegram command listener running so you can request a fresh scan whenever you want.
- `web_robot_app.py`: private iPhone-friendly web control panel for running scans without Telegram.

The two sources are OLX Lebanon and Facebook Marketplace (see [Facebook Marketplace Search](#facebook-marketplace-search) below for setup).

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

- Listing title and city
- Price, sqm, and USD/sqm
- OLX/Facebook account name
- Phone number from the listing description first, then the current listing's seller profile when OLX/Facebook exposes it, or `not shown by OLX`/`not shown by Facebook`
- Listing link

Other commands:

```text
/status
/help
/last7
/last14
/last21
```

## Facebook Marketplace Search

Facebook Marketplace has no public search API like OLX, so this source drives a real, logged-in browser session (Playwright) instead. Matched listings are scored with the same owner-likelihood rules as OLX (`owner_scoring.py`), for the same Metn target areas.

### One-time setup

1. Install dependencies, including the Playwright browser:

```powershell
pip install -r requirements.txt
playwright install chromium
```

2. On a machine with a browser/display (not Railway), create the saved session once:

```powershell
python .\facebook_login_setup.py
```

A Chromium window opens at facebook.com. Log into the Facebook account you want the bot to browse Marketplace as, solve any 2FA/checkpoint prompts, then go back to the terminal and press Enter. This saves `data/facebook_session_state.json` (already covered by `.gitignore` -- never commit it; treat it like a password).

3. Optional: if results look wrong, open Marketplace in your own browser, search near Beirut, copy the location segment from the URL, and set it as `FACEBOOK_MARKETPLACE_LOCATION` in `.env`/`.env.private`.

### Running

Once `data/facebook_session_state.json` exists, `telegram_owner_alert.py` automatically scans Facebook Marketplace rent/sale alongside OLX, so `/latest`, `/last7`, `/last14`, `/last21`, and the web app pick it up too. If the session file is missing, the scan silently falls back to OLX only.

Useful flags:

```powershell
python .\telegram_owner_alert.py --skip-facebook
python .\telegram_owner_alert.py --facebook-max-listings 100
python .\telegram_owner_alert.py --facebook-session path\to\session.json
```

You can also run the Facebook scan standalone for debugging:

```powershell
python .\facebook_marketplace.py --headed
```

`--headed` shows the browser window. Facebook frequently changes Marketplace's HTML, so if card or detail extraction stops finding fields, use `--headed` to watch the browser and update the selectors in `facebook_marketplace.py`.

### Scoring differences from OLX

Facebook Marketplace search results do not expose exact coordinates or the same structured agency fields OLX does, so the scan adapts:

- Each matched listing gets the approximate centroid coordinates of its target area (`owner_scoring.TARGET_AREAS`), so distance-from-Fanar scoring still applies.
- Seller post counts, phone-number reuse, "owner" wording, reference codes, and agency keywords are computed the same way as OLX, from the listing title/description and the seller's Marketplace profile/Page.
- A seller posting via a Facebook Page is treated like a business/agency; a seller using a personal Marketplace profile is treated like an individual.

### Account risk

This feature browses Marketplace using your personal Facebook account's logged-in session. Facebook may flag or restrict accounts used for automated browsing -- use an account you're comfortable with for this, and keep scan frequency modest.

### Railway

The Dockerfile already installs Playwright + Chromium. To enable Facebook Marketplace on Railway:

1. Run `facebook_login_setup.py` locally to create `data/facebook_session_state.json`.
2. Put that file on the same Railway volume used for seen-listing state (mounted at `/app/data`), or set `FACEBOOK_SESSION_PATH` to wherever you place it.

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

## Railway 24/7 Hosting

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
3. Add a Railway volume mounted at `/app/data` if you want seen-listing state to survive redeploys.
4. Open the Railway public URL for the iPhone web app.

## Owner Filter

The Telegram alert now focuses on the selected Metn OLX areas for both sale and rent listings: Fanar, Mar Roukoz, Broumana, Beit Mery, Jdeideh, Rawda, Bsalim, Mezher, Biakout, Sabtieh, Dekwaneh, Mkalles, Sin El Fil, Jisr El Bacha, Horsh Tabet, Baouchrieh, Rabweh, Zalka, Jal El Dib, Antelias, Dbayeh, Nahr El Mott, Kornet Chehwan, Ain Saadeh, Mansourieh, Monteverde, Roumieh, Tilal Ain Saadeh, and Ain Najem.

It uses an owner-likelihood score instead of one hard rejection, because some OLX owner listings still carry messy seller metadata.

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
