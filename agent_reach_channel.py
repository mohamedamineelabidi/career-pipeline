"""Agent Reach style READER channel: ordered read-only backends for public job pages.

Backend order (first success wins):
    1. direct  - plain GET with the existing extractor (fetch_job_descriptions)
    2. jina    - Jina Reader proxy ``https://r.jina.ai/<url>`` (Accept: text/plain, 25s)
    3. blocked - nothing worked; recorded, never bypassed

Hard rules: LinkedIn / Glassdoor / Indeed URLs are NEVER sent to Jina (login walls
and anti-bot protections are respected, not circumvented). No cookies, no
credentials, polite pacing (>= 2s between Jina calls). Only the public job URL
is forwarded to the third-party Jina Reader service — see AGENT_REACH.md.
"""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import fetch_job_descriptions as fjd

JINA_PREFIX = "https://r.jina.ai/"
JINA_TIMEOUT_SECONDS = 25
JINA_MIN_INTERVAL_SECONDS = 2.0
MIN_TEXT_CHARS = fjd.MIN_DESCRIPTION_CHARS
JINA_DENY_HOST_MARKERS = ("linkedin.com", "glassdoor.", "indeed.")
JINA_ERROR_MARKERS = ("target url returned error", "this page requires javascript", "login to continue")
_TRAILING_NOISE = re.compile(r"\n(?:Title|URL Source|Published Time|Markdown Content):.*", re.IGNORECASE)

_last_jina_at = [0.0]


def is_jina_denied(url: str) -> bool:
    host = urlparse(str(url or "").lower()).netloc
    return any(marker in host for marker in JINA_DENY_HOST_MARKERS)


def default_jina_fetcher(url: str) -> tuple[int, str]:
    request = urllib.request.Request(JINA_PREFIX + url, method="GET", headers={
        "User-Agent": fjd.USER_AGENT, "Accept": "text/plain", "X-No-Cache": "true",
    })
    try:
        with urllib.request.urlopen(request, timeout=JINA_TIMEOUT_SECONDS) as response:
            raw = response.read(2_000_000)
            charset = response.headers.get_content_charset() or "utf-8"
            return response.status, raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as error:
        try:
            body = error.read(100_000).decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - defensive
            body = ""
        return error.code, body


def clean_jina_text(body: str) -> str:
    """Strip the Jina front-matter (Title/URL Source/Markdown Content) and normalise."""
    text = body.replace("\r\n", "\n")
    marker = "Markdown Content:"
    if marker in text[:2000]:
        text = text.split(marker, 1)[1]
    else:
        text = re.sub(r"\A(?:(?:Title|URL Source|Published Time|Warning):.*\n)+", "", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)  # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links -> label
    return fjd._normalize(text)


def read_via_direct(url: str, *, fetcher=fjd.default_fetcher) -> tuple[str, str]:
    if fjd.is_login_wall_url(url):
        return "login_wall", ""
    try:
        result = fetcher(url)
    except Exception as error:
        return f"error:{type(error).__name__}", ""
    status, text = fjd.classify(url, result)
    return status, text


def read_via_jina(url: str, *, fetcher=default_jina_fetcher, sleep=time.sleep, clock=time.monotonic) -> tuple[str, str]:
    if is_jina_denied(url):
        return "login_wall", ""
    wait = JINA_MIN_INTERVAL_SECONDS - (clock() - _last_jina_at[0])
    if wait > 0:
        sleep(wait)
    _last_jina_at[0] = clock()
    try:
        status, body = fetcher(url)
    except Exception as error:
        return f"error:{type(error).__name__}", ""
    if status != 200:
        return f"error:{status}", ""
    lowered = body[:4000].lower()
    if any(marker in lowered for marker in JINA_ERROR_MARKERS) or fjd.looks_blocked(body):
        return "blocked", ""
    text = clean_jina_text(body)
    if len(text) < MIN_TEXT_CHARS:
        return "error:too_short", ""
    return "ok", text


def read_url(url: str, *, direct_fetcher=fjd.default_fetcher, jina_fetcher=default_jina_fetcher,
             sleep=time.sleep, clock=time.monotonic) -> dict:
    """Return {text, status, backend, attempts}. status: ok | login_wall | blocked."""
    attempts: list[dict] = []
    status, text = read_via_direct(url, fetcher=direct_fetcher)
    attempts.append({"backend": "direct", "status": status})
    if status == "ok":
        return {"text": text, "status": "ok", "backend": "direct", "attempts": attempts}
    if is_jina_denied(url):
        attempts.append({"backend": "jina", "status": "skipped_denylist"})
        return {"text": "", "status": "login_wall", "backend": "blocked", "attempts": attempts}
    status, text = read_via_jina(url, fetcher=jina_fetcher, sleep=sleep, clock=clock)
    attempts.append({"backend": "jina", "status": status})
    if status == "ok":
        return {"text": text, "status": "ok", "backend": "jina", "attempts": attempts}
    return {"text": "", "status": "blocked", "backend": "blocked", "attempts": attempts}
