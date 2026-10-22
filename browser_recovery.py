from __future__ import annotations

import json
import os
import queue
import secrets
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from challenge_detection import detect_challenge
from reliability_config import ROOT, load_reliability
from scan_progress import ProgressStore
from source_errors import SourceResponseError


def allowed_url(url):
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in {"olx.com.lb", "www.olx.com.lb"} and not parsed.username and not parsed.password


def browser_fetch(url, timeout):
    progress = ProgressStore()
    port = progress.get("browser_service_port")
    secret = progress.get("browser_service_secret")
    if not port or not secret:
        raise SourceResponseError("The verified browser service is unavailable; saved progress is retained.", kind="temporary", url=url)
    request = Request(f"http://127.0.0.1:{port}/recovery/internal", data=json.dumps({"url": url, "timeout": timeout}).encode(), headers={"Content-Type": "application/json", "X-Recovery-Internal": secret}, method="POST")
    with urlopen(request, timeout=timeout + 15) as response:
        result = json.load(response)
    if "error" in result:
        raise SourceResponseError(result["error"], kind="temporary", url=url)
    return result["status"], result["headers"], result["content"]


class BrowserService:
    def __init__(self):
        self.commands = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._worker, name="verification-browser", daemon=True)
        self.context = None
        self.page = None
        self.playwright = None
        self.incident_id = ""
        self.last_activity = time.time()
        self.started_at = 0
        self.verified = False
        self.last_status = 200
        self.thread.start()

    def call(self, action, **values):
        future = Future()
        self.commands.put((future, action, values))
        return future.result(timeout=60)

    def _launch(self):
        if self.context:
            return
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        config = load_reliability()
        profile = Path(os.getenv("RECOVERY_BROWSER_PROFILE", str(ROOT / "data/verification-browser")))
        profile.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(profile, 0o700)
        channel = os.getenv("RECOVERY_BROWSER_CHANNEL", config.browser_channel)
        self.context = self.playwright.chromium.launch_persistent_context(str(profile), channel=channel or None, headless=False, viewport={"width": 1024, "height": 768}, accept_downloads=False, timeout=30000)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.on("response", self._response)
        self.context.on("page", lambda page: page.close() if page != self.page else None)
        self.page.on("dialog", lambda dialog: dialog.dismiss())

    def _response(self, response):
        if response.request.is_navigation_request() and response.frame == self.page.main_frame:
            self.last_status = response.status

    def _close(self):
        try:
            if self.context:
                self.context.close()
        finally:
            self.context = None
            self.page = None
            if self.playwright:
                self.playwright.stop()
                self.playwright = None

    def _worker(self):
        while not self.stop_event.is_set():
            config = load_reliability()
            if self.context and (time.time() - self.last_activity > config.verification_idle_seconds or (not self.verified and self.started_at and time.time() - self.started_at > config.verification_max_seconds)):
                self._close()
            try:
                future, action, values = self.commands.get(timeout=2)
            except queue.Empty:
                continue
            if future.cancelled():
                continue
            try:
                if action != "frame":
                    self.last_activity = time.time()
                future.set_result(self._execute(action, values))
            except Exception as error:
                if action in {"start", "fetch"} or type(error).__module__.startswith("playwright"):
                    try:
                        self._close()
                    except Exception:
                        pass
                future.set_exception(error)
        self._close()

    def _execute(self, action, values):
        progress = ProgressStore()
        incident = progress.current_incident()
        if action == "start":
            if not incident or incident["kind"] != "captcha" or values["incident_id"] != incident["id"]:
                raise PermissionError("No active verification incident")
            if not allowed_url(incident["url"]):
                raise PermissionError("Only the affected OLX page may be verified")
            if self.incident_id == incident["id"] and self.context:
                return {"started": True}
            self._launch()
            self.incident_id = incident["id"]
            self.started_at = time.time()
            self.verified = False
            self.page.goto(incident["url"], wait_until="domcontentloaded", timeout=30000)
            return {"started": True}
        if action == "fetch":
            url = values["url"]
            if incident and (incident["kind"] in {"captcha", "schema_changed"} or incident["retry_at"] > time.time()):
                raise PermissionError("Scraping is paused")
            if not progress.get("browser_mode", False) or not allowed_url(url):
                raise PermissionError("Verified OLX browser session required")
            self._launch()
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=min(30000, int(values.get("timeout", 30)) * 1000))
            if not allowed_url(self.page.url):
                raise PermissionError("OLX redirected outside the authorized listing site")
            return {"status": response.status if response else 200, "headers": response.all_headers() if response else {}, "content": self.page.content()}
        if not self.page or not incident or incident["id"] != values.get("incident_id"):
            raise PermissionError("Verification browser is not active")
        if action == "frame":
            self._check_verified(incident)
            if self.verified:
                return {"verified": True}
            return self.page.screenshot(type="jpeg", quality=70)
        if action == "input":
            if values.get("type") == "click":
                self.page.mouse.click(max(0, min(1023, float(values["x"]))), max(0, min(767, float(values["y"]))))
            elif values.get("type") == "scroll":
                self.page.mouse.wheel(0, max(-768, min(768, float(values["delta"]))))
            elif values.get("type") == "text":
                self.page.keyboard.insert_text(str(values.get("text", ""))[:1000])
            elif values.get("type") == "key" and values.get("key") in {"Tab", "Enter", "Backspace", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Space"}:
                self.page.keyboard.press(values["key"])
            else:
                raise ValueError("Unsupported manual input")
            self._check_verified(incident)
            return {"verified": self.verified}
        raise ValueError("Unsupported browser action")

    def _check_verified(self, incident):
        if not allowed_url(self.page.url):
            return
        content = self.page.content()
        config = load_reliability()
        if detect_challenge(getattr(self, "last_status", 200), {}, content, config):
            return
        from telegram_owner_alert import find_current_listing_item, search_page_details
        from property_bot import Listing
        try:
            if "/ad/" in urlparse(self.page.url).path:
                listing = Listing("", "", None, None, "", "", self.page.url)
                if not find_current_listing_item(content, listing):
                    return
            else:
                search_page_details(content, "verification", "sale")
        except SourceResponseError:
            return
        progress = ProgressStore()
        progress.set("browser_mode", True)
        progress.resolve(incident["id"])
        progress.set("resume_requested", True)
        progress.set("resumed_incident", incident)
        self.verified = True

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=10)


SERVICE = None


def service():
    global SERVICE
    if SERVICE is None:
        SERVICE = BrowserService()
    return SERVICE


def register_service(port):
    progress = ProgressStore()
    progress.set("browser_service_port", port)
    progress.set("browser_service_secret", secrets.token_urlsafe(32))
