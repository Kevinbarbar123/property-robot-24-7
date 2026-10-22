# Preventive OLX recovery upgrade — October 12, 2026

## Implemented

- Existing urllib scraper retained. Both the main Telegram scanner and optional SQLite/export pipeline use the shared recovery layer.
- Modular detection distinguishes reCAPTCHA, hCaptcha, Turnstile, generic verification, HTTP access restrictions, rate limits, connection failures, and parser changes.
- Shared conservative request limits, exponential backoff with jitter, persistent Retry-After deadlines, validated detail caching, and pause-before-retry behavior.
- Durable SQLite checkpoints preserve completed pages, listing details, original job options, and pending work across interruptions. Existing seen history remains intact.
- Protected phone verification page controls only the bot's persistent browser session. Link tokens are single-use; cookies are secure/HttpOnly; origin checks and idle/absolute expiry protect access. Verified OLX data is required before automatic continuation.
- Telegram `/verify`, `/resume`, and expanded `/status`, incident notifications, recipient/chunk delivery receipts, and monitoring of ambiguous deliveries.
- Recovery-worker lock prevents competing resume workers. Browser resources are cleaned on shutdown, timeout, and failures; repeated connection recovery is bounded.
- Railway Docker setup includes matching Playwright/Chromium, Xvfb, persistent data support, and Asia/Beirut timezone. Normal scans do not start a browser.

## Preservation and checkpoint

- Baseline Git checkpoint: `06b6a39`. Credentials, data, logs, browser profiles, backups, and browser-control artifacts are excluded.
- Backup: `backups/20261012-preventive-recovery`.
- AST comparison confirms existing area maps, property exclusions, owner scoring, ordering, age filters, and deduplication functions are unchanged. Existing Windows task definitions and scan intervals remain enabled.
- Core source integration changes: property_bot.py, telegram_owner_alert.py, telegram_command_bot.py, web_robot_app.py, main.py, scraper.py, notifier.py, and railway_start.py. Independent recovery modules and configuration were added; README and environment examples were updated.

## Verification

- All **55 automated tests pass**, including the original 25 regression tests.
- Simulated CAPTCHA/403/429, Retry-After formats, bounded retries, saved-page resumption, receipt deduplication, timeout expiry, origin rejection, browser cleanup, and authorized-command checks pass.
- A real Chrome browser with all navigation intercepted by fixtures completed the protected claim → screenshot → manual input → verified-page flow. No real OLX CAPTCHA was triggered or solved.
- Python compile checks and Git whitespace checks pass. The pinned Playwright 1.63.0 release exists and supports Python 3.12.
- Local Telegram listener was reloaded through its existing hidden supervisor. Local web/recovery service started hidden on port 8787.

## Remaining deployment work

Railway is not authenticated in the available browser/CLI, no Railway token is configured, and its connector is not yet connected. The old service URL returns "Application not found." The cloud deployment, private migration of existing history, and phone-accessible HTTPS verification link have therefore **not** been activated. Existing laptop services remain running; no competing cloud poller was started and no Windows schedules were disabled.

Connect the suggested Railway integration to continue restoration. Deployment must keep `/app/data` persistent, preserve Telegram credentials privately, retain the saved history, and set/derive the HTTPS public origin before enabling the verification link. Validate the cloud service before disabling the laptop scanner/listener. Docker is unavailable locally, so the Linux image build still needs deployment validation.

## Limitations and recovery instructions

- Complete actual website verification manually through the Telegram link. The system does not solve CAPTCHA, spoof fingerprints, rotate proxies, or bypass restrictions.
- Some verification methods may require capabilities beyond the image/keyboard controls (for example audio); persistent access denial remains paused.
- Website format changes pause safely and still require parser review. Configurable extraction paths and selectors make that review smaller; they cannot predict every future redesign.
- Telegram cannot guarantee exactly-once delivery after a lost response. Uncertain sends are recorded and are not automatically resent; `/status` reports them for manual review.
- For a future verification incident, use `/verify`; after a temporary failure, use `/status` and allow the deadline to expire, or `/resume` when eligible. Pending jobs retain their original criteria.
- Roll back source with the local baseline checkpoint or source backup while preserving current runtime data. Do not overwrite newer history with an old backup or publish runtime data to GitHub.
