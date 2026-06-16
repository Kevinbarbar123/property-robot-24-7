#!/usr/bin/env python3
"""
One-time helper to create the saved Facebook session used by
facebook_marketplace.py.

Run this on a machine with a display (not on Railway):

    python3 facebook_login_setup.py

A real Chromium window opens at facebook.com. Log into the Facebook account
you want the bot to browse Marketplace as, solve any 2FA/checkpoint prompts,
then come back to this terminal and press Enter. The logged-in session
(cookies + storage) is saved to FACEBOOK_SESSION_PATH (default:
data/facebook_session_state.json) so facebook_marketplace.py can reuse it
headlessly without logging in again.

The saved file grants access to this Facebook account - treat it like a
password. It is covered by .gitignore (data/*_state.json) and must never be
committed. On Railway, put it on a persistent volume and point
FACEBOOK_SESSION_PATH at it.

Using a personal account for automated browsing carries a risk that Facebook
flags or restricts the account. Use an account you are comfortable with for
this purpose.
"""

from __future__ import annotations

import os
from pathlib import Path

from facebook_marketplace import FACEBOOK_BASE_URL, session_path_from_env


def load_dotenv(path: Path = Path(".env"), *, override: bool = False) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value


def main() -> int:
    load_dotenv(Path(".env"), override=False)
    load_dotenv(Path(".env.private"), override=True)

    session_path = session_path_from_env()
    session_path.parent.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(FACEBOOK_BASE_URL, wait_until="domcontentloaded")

            print("A browser window has opened.")
            print("1. Log into the Facebook account the bot should use for Marketplace.")
            print("2. Complete any 2FA or checkpoint prompts.")
            print("3. Once you see your Facebook feed, come back here and press Enter.")
            input("Press Enter when you are logged in... ")

            context.storage_state(path=str(session_path))
            print(f"Saved Facebook session to {session_path}")
        finally:
            browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
