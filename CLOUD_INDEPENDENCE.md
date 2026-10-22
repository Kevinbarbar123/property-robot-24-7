# Laptop-independent operation

## Current status — October 23, 2026

The runtime is ready for staged hosting, but **cloud activation and cutover are not complete**. The existing laptop listener and Windows scan schedule remain enabled. Do not switch the laptop off expecting cloud service yet.

The authenticated Railway dashboard shows an expired trial and zero of one services online in project `diligent-elegance`. No subscription has been purchased. The Railway connector/CLI is not authenticated; native browser control could read the dashboard but subsequent screen captures timed out. This is not a working deployment.

Railway also disclosed a security incident on October 21 involving environment variables. Its October 22 updates recommend rotating stored secrets and reconnecting GitHub. Whether this bot's credentials were exposed is not established. Review the provider's guidance before restoring service:

- [Security disclosure](https://blog.railway.com/p/security-disclosure-wednesday-october-21st)
- [Credential reset and GitHub reconnection](https://blog.railway.com/p/security-disclosure-recommendation-redeploy-buckets-and-reconnect-github-after-the-credential-reset)
- [Current incident status](https://status.railway.com/incident/QH8L8B3E)

## What runs without Codex

`railway_start.py` runs the existing website, Telegram command listener, recovery worker, and persisted daily owner schedule in one long-running Python container. OLX scraping, owner classification, history, exports, and Telegram alerts do not call Codex or require OpenAI credits. Optional AI conversation continues to use its existing provider configuration and built-in fallback; it is not required for scans. Hosting and internet access must remain paid/available.

Docker starts with `BOT_ACTIVE=false`. In staging, there is no Telegram polling, automatic scan, manual web scan, or automatic recovery. `/healthz` returns `{"status":"ok","mode":"staging"}`. This permits testing without disrupting the laptop.

Activation requires both a real persistent volume and an imported-history marker. Reports and logs are linked into the volume, not left on ephemeral container storage. The Telegram listener has its existing workspace lock; deploy only one replica and one scanning host. A local watchdog checks the website and required supervisor threads every 30 seconds. Three failures request a complete restart. Railway's deployment health check is startup-only, not continuous uptime monitoring.

## Safe deployment and cutover

1. Activate hosting and connect authenticated Railway management access. Use the existing GitHub repository `Kevinbarbar123/property-robot-24-7`, not another bot's repository. Review Railway security guidance; replace credentials previously stored there. Telegram tokens must be rotated through BotFather if they were stored in the affected platform. Do not paste tokens into a GitHub file, issue, chat, or log.
2. Deploy the Dockerfile with **one replica**, serverless/app sleeping disabled, and a persistent volume at `/app/data`. Allow space for history, cached pages, browser profile, and backups; monitor usage. Keep `BOT_ACTIVE=false` and the Docker persistence/migration safeguards enabled.
3. Generate the HTTPS service domain. Leave `RECOVERY_BROWSER_CHANNEL` empty in Linux; Docker supplies Chromium and Xvfb. `RAILWAY_PUBLIC_DOMAIN` supplies the website/verification origin automatically; alternatively set `RECOVERY_PUBLIC_URL=https://your-domain`.
4. Check `/healthz`, the website, and build/test logs. The staging health response is not proof of active Telegram delivery. The GitHub workflow runs Linux regressions and a container smoke test without Telegram/OLX requests or production secrets.
5. At the short cutover window, disable `KimiPropertyTelegramBotHiddenWatchdog` and `PropertyOwnerIndependentScan`, stop only this project's verified listener/supervisors, and wait for any in-flight scan to finish. Export the task definitions for rollback. Other trading and seller-radar bots must not be stopped.
6. Create a fresh final archive while those services remain stopped:

   ```powershell
   python migrate_bot.py export --final --output backups/cloud-final.tar.gz
   ```

   This refuses an active local listener/scan or unfinished job. It preserves seen listings, Telegram offsets, delivery receipts, saved schedules, lead statuses, blacklists, and history reports. SQLite's backup API snapshots the database consistently. Only copied cache/browser-control tokens are removed; the original database is unchanged. Credentials and personal browser profiles are excluded. Archives contain private listing/contact history: never commit or publish them. A normal export without `--final` is a live backup, deliberately not valid for activation.

7. Transfer the final archive privately using the authorized Railway volume upload/SSH facilities. Stop any staging process before importing; do not import while it is serving requests. On the service/container, with `BOT_ACTIVE=false`, restore into the empty volume:

   ```sh
   python migrate_bot.py restore /app/data/cloud-final.tar.gz --data /app/data
   ```

   Import validates names, sizes, SHA-256 checksums, JSON, and SQLite integrity. It refuses overwriting existing bot data. A successful import writes `migration-ready.json`. Do not manufacture this marker or disable `REQUIRE_MIGRATED_STATE` to skip history. Logs and the uploaded archive can already exist in the destination.
8. Add the valid Telegram token and the existing approved chat IDs through private Railway variables. Preserve both destinations. Enable `BOT_ACTIVE=true`, keep `OWNER_SCAN_ENABLED=true`, and retain `OWNER_SCAN_INTERVAL_HOURS=24` unless intentionally changing the interval.
9. Verify `/healthz` reports `active`; use `/status` and `/latest` from Telegram; confirm both approved phones can use the bot, the public URL works off home Wi-Fi, imported history is present, existing listings are not resent, and the saved schedule has its original next-run deadline. Test a redeploy to confirm data persistence. Leave laptop tasks disabled after successful cloud validation.
10. Enable provider volume backups and billing/usage alerts. Keep a separate private backup outside the running volume. The app has no new CAPTCHA bypass and cannot guarantee availability during platform restrictions/outages. Manual verification still requires the protected `/verify` session when needed.

The website intentionally remains password-free as requested. Anyone who can reach its public URL can view its listing history and request scans; the manual verification portal has separate protection. Decide whether to add access control before sharing the public URL widely.

## Rollback

If cloud activation fails, stop the cloud listener/scanner before re-enabling the laptop tasks. Do not operate both against different state copies. If the cloud processed listings or commands, export its latest state back before restarting the laptop; restoring an old laptop snapshot risks duplicate notifications and lost history. Keep credentials synchronized privately after any rotation.

## Validation

Windows: `python -m unittest discover -v` ran 72 tests: 71 passed and one Linux symlink test was skipped. The real-browser verification test uses intercepted local fixtures only. Compile and whitespace checks passed. Linux/Docker CI is configured; report its actual result separately after GitHub execution. This machine has no Docker installation, so no local container build is claimed.

Sources: [Railway volumes](https://docs.railway.com/volumes), [config-as-code](https://docs.railway.com/config-as-code/reference), [health checks](https://docs.railway.com/deployments/healthchecks).
