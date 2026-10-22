import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import browser_recovery
import recovery_portal
import scan_progress
from source_errors import SourceAccessError


class VerificationFlowTests(unittest.TestCase):
    def test_manual_phone_controls_verify_the_same_browser_session(self):
        from playwright.sync_api import sync_playwright
        normal = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": {"initialState": {"search": {"ads": {"hits": [], "totalHits": 0}}}}}}) + '</script>'
        challenge = '<div class="h-captcha">Simulated verification required</div><button style="position:absolute;top:100px;left:10px;width:250px;height:60px" onclick="document.documentElement.innerHTML=' + __import__('html').escape(json.dumps(normal), quote=True) + '">Complete simulated verification</button>'
        navigations = []

        class SimulatedBrowser(browser_recovery.BrowserService):
            def _launch(self):
                if self.context:
                    return
                self.playwright = sync_playwright().start()
                channel = os.getenv("RECOVERY_BROWSER_CHANNEL", "chrome") or None
                self.context = self.playwright.chromium.launch_persistent_context(os.environ["RECOVERY_BROWSER_PROFILE"], channel=channel, headless=True, viewport={"width": 1024, "height": 768})
                def fixture(route):
                    navigations.append(route.request.url)
                    route.fulfill(status=200, content_type="text/html", body=challenge)
                self.context.route("**/*", fixture)
                self.page = self.context.pages[0]

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                recovery_portal.dispatch(self, self.path, "GET")
            def do_POST(self):
                recovery_portal.dispatch(self, self.path, "POST")
            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"OLX_RECOVERY_DB": str(Path(temporary) / "recovery.sqlite3"), "RECOVERY_BROWSER_PROFILE": str(Path(temporary) / "profile"), "RECOVERY_PUBLIC_URL": "https://bot.example"}):
            progress = scan_progress.ProgressStore()
            incident = progress.incident(SourceAccessError("simulated", kind="captcha", url="https://olx.com.lb/properties/apartments-villas-for-sale/fanar/"))
            token = progress.link(incident["id"])
            browser = SimulatedBrowser()
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with patch.object(recovery_portal, "service", return_value=browser):
                try:
                    with urlopen(base + "/verify") as response:
                        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                    request = Request(base + "/recovery/claim", data=json.dumps({"token": token}).encode(), headers={"Origin": "https://bot.example", "Content-Type": "application/json"})
                    with urlopen(request, timeout=45) as response:
                        cookie = response.headers["Set-Cookie"]
                        self.assertIn("Secure; HttpOnly; SameSite=Strict", cookie)
                    cookie_value = cookie.split(";", 1)[0]
                    with urlopen(Request(base + "/recovery/frame", headers={"Cookie": cookie_value}), timeout=30) as response:
                        self.assertEqual(response.headers["Content-Type"], "image/jpeg")
                        self.assertGreater(len(response.read()), 100)
                    request = Request(base + "/recovery/input", data=json.dumps({"type": "click", "x": 100, "y": 130}).encode(), headers={"Origin": "https://bot.example", "Content-Type": "application/json", "Cookie": cookie_value})
                    with urlopen(request, timeout=30) as response:
                        self.assertTrue(json.load(response)["verified"])
                    self.assertTrue(progress.get("browser_mode"))
                    self.assertTrue(progress.get("resume_requested"))
                    self.assertFalse(progress.current_incident())
                    self.assertEqual(navigations, [incident["url"]])
                finally:
                    server.shutdown()
                    server.server_close()
                    browser.stop()


if __name__ == "__main__":
    unittest.main()
