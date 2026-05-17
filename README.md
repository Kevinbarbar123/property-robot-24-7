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

- Listing title and city
- Price, sqm, and USD/sqm
- OLX account name
- Phone number from the listing description first, then the current listing's seller profile when OLX exposes it, or `not shown by OLX`
- Listing link

Other commands:

```text
/status
/help
/last7
/last14
/last21
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
