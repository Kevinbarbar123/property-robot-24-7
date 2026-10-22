from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from reliability_config import ReliabilityConfig, load_reliability


@dataclass(frozen=True)
class Detection:
    kind: str
    provider: str = ""
    retry_at: float = 0


def configured_widget(markup, selectors):
    class Elements(HTMLParser):
        def __init__(self):
            super().__init__()
            self.items = []
        def handle_starttag(self, tag, attrs):
            self.items.append((tag, dict(attrs)))
    parser = Elements()
    parser.feed(markup)
    for selector in selectors:
        for tag, attrs in parser.items:
            if selector.startswith(".") and selector[1:] in (attrs.get("class") or "").split():
                return True
            if selector.startswith("#") and selector[1:] == attrs.get("id"):
                return True
            match = re.fullmatch(r"([a-z]+)\[([a-z-]+)\*=['\"]([^'\"]+)['\"]\]", selector)
            if match and tag == match.group(1) and match.group(3) in (attrs.get(match.group(2)) or ""):
                return True
    return False


def retry_deadline(value: str, now: float | None = None) -> float:
    now = time.time() if now is None else now
    try:
        return now + max(0, int(value.strip()))
    except (ValueError, AttributeError):
        try:
            return max(now, parsedate_to_datetime(value).timestamp())
        except (TypeError, ValueError, OverflowError, AttributeError):
            return 0


def detect_challenge(status: int, headers, content: str, config: ReliabilityConfig | None = None) -> Detection | None:
    config = config or load_reliability()
    normalized_headers = {str(key).lower(): str(value).lower() for key, value in (headers or {}).items()}
    deadline = retry_deadline(normalized_headers.get("retry-after", ""))
    action = normalized_headers.get("x-amzn-waf-action", "")
    if status == 429:
        return Detection("rate_limited", retry_at=deadline)
    text = content.lower()
    markup = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", "", text, flags=re.S)
    visible = html.unescape(markup)
    visible = re.sub(r"<[^>]+>", " ", visible)
    visible = re.sub(r"\s+", " ", visible)
    wording = any(phrase.lower() in visible for phrase in config.challenge_phrases)
    provider = ""
    if re.search(r"<(?:iframe|div)[^>]+(?:recaptcha|g-recaptcha)", markup):
        provider = "recaptcha"
    elif re.search(r"<(?:iframe|div)[^>]+(?:hcaptcha|h-captcha)", markup):
        provider = "hcaptcha"
    elif re.search(r"<(?:iframe|div)[^>]+(?:cf-turnstile|challenges.cloudflare.com)", markup):
        provider = "turnstile"
    has_listing_data = '"initialstate"' in text and ('"ads"' in text or '"ad"' in text)
    widget = bool(provider) or configured_widget(markup, config.challenge_selectors)
    if config.enabled and (action in {"captcha", "challenge"} or normalized_headers.get("cf-mitigated") == "challenge" or (wording and not has_listing_data) or (widget and not has_listing_data)):
        return Detection("captcha", provider or ("waf" if action else "verification"), deadline)
    if status in {202, 401, 403} or action == "block":
        return Detection("access_restricted", retry_at=deadline)
    return None
