from __future__ import annotations

import random
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request

from challenge_detection import detect_challenge
from reliability_config import load_reliability
import scan_progress
from source_errors import SourceAccessError, SourceResponseError


def store():
    return scan_progress.CURRENT_STORE or scan_progress.ProgressStore()


def restrict(detection, url, status, config):
    now = time.time()
    cooldown = config.rate_limit_cooldown if detection.kind == "rate_limited" else config.restriction_cooldown
    deadline = max(detection.retry_at, now + cooldown)
    error = SourceAccessError(f"OLX {detection.kind.replace('_', ' ')} (HTTP {status}); requests paused.", kind=detection.kind, url=url, status=status, retry_at=deadline, provider=detection.provider)
    store().incident(error, scan_progress.CURRENT_JOB)
    raise error


def fetch(url, timeout, retries, retry_delay, request_fn):
    config = load_reliability()
    progress = store()
    host = urlparse(url).hostname or ""
    is_detail = "/ad/" in urlparse(url).path
    incident = progress.current_incident()
    if incident and (incident["kind"] in {"captcha", "schema_changed"} or incident["retry_at"] > time.time()):
        raise SourceAccessError("OLX session is paused; see /status or /verify.", kind=incident["kind"], url=incident["url"], retry_at=incident["retry_at"], provider=incident["provider"])
    if is_detail and scan_progress.CURRENT_JOB:
        cached = progress.cache(url)
        if cached is not None:
            return cached
    request = Request(url, headers={"User-Agent": "PropertyRobot/1.0 (+public listing monitor)", "Accept-Language": "en-US,en;q=0.9"})
    attempts = min(retries + 1, config.max_attempts)
    for attempt in range(attempts):
        lease = progress.gate(host, config)
        try:
            if progress.get("browser_mode", False):
                from browser_recovery import browser_fetch
                status, headers, content = browser_fetch(url, timeout)
            else:
                with request_fn(request, timeout=timeout) as response:
                    status = response.status
                    headers = response.headers
                    content = response.read().decode("utf-8", "replace")
            detection = detect_challenge(status, headers, content, config)
            if detection:
                restrict(detection, url, status, config)
            if not content.strip():
                raise SourceResponseError("OLX returned an empty response; listing availability is unknown.", kind="temporary", url=url)
            if status >= 500:
                raise HTTPError(url, status, "temporary server failure", headers, None)
            if incident:
                progress.resolve(incident["id"])
                progress.set("resumed_incident", incident)
            return content
        except HTTPError as error:
            try:
                content = error.read(1024 * 1024).decode("utf-8", "replace")
            except (AttributeError, OSError):
                content = ""
            detection = detect_challenge(error.code, error.headers, content, config)
            if detection:
                restrict(detection, url, error.code, config)
            if error.code < 500:
                raise
            if attempt == attempts - 1:
                failure = SourceResponseError("OLX server requests failed after bounded retries.", kind="temporary", url=url, status=error.code, retry_at=time.time() + config.temporary_cooldown)
                progress.incident(failure, scan_progress.CURRENT_JOB)
                raise failure from error
        except (URLError, TimeoutError, OSError) as error:
            if attempt == attempts - 1:
                failure = SourceResponseError("OLX connection failed after bounded retries.", kind="temporary", url=url, retry_at=time.time() + config.temporary_cooldown)
                progress.incident(failure, scan_progress.CURRENT_JOB)
                raise failure from error
        finally:
            progress.release(lease)
        delay = min(config.backoff_cap, max(config.backoff_base, retry_delay) * 2 ** attempt)
        time.sleep(random.uniform(delay / 2, delay))
    raise SourceResponseError("OLX request failed", kind="temporary", url=url)
