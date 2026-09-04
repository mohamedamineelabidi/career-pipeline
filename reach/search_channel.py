"""Agent Reach style SEARCH channel: Exa semantic search through the mcporter CLI.

Ported from Panniantong/Agent-Reach, whose "全网搜索" channel is
``mcporter call exa web_search_exa``.  This module is the only place Reach talks
to a search engine, so the rules live here:

- Read-only.  We send a query and parse text back.  Nothing is posted anywhere.
- No email guessing.  Any query containing an ``@`` is refused outright, so the
  search channel can never be used to "confirm" a made-up address pattern.
- Polite pacing (>= 1.5 s between calls) and a hard timeout.
- Failures are recorded (``last_error``) and return ``[]``; nothing retries in
  a loop and nothing bypasses a refusal.

Setup (one-off, already done on this machine):
    npm install -g mcporter
    mcporter config add exa https://mcp.exa.ai/mcp --scope home
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from typing import Any

TOOL = "web_search_exa"
SERVER = "exa"
TIMEOUT_SECONDS = 60
MIN_INTERVAL_SECONDS = 1.5
CATEGORIES = {"people", "company", None}

_last_call = 0.0
_last_error = ""


def last_error() -> str:
    return _last_error


def available() -> bool:
    """True when the mcporter CLI is on PATH (not a live check; see doctor())."""
    return shutil.which("mcporter") is not None


def _mcporter_bin() -> str:
    return shutil.which("mcporter") or "mcporter"


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_HEADING_RE = re.compile(r"#{1,6}\s*")


def clean_snippet(text: str) -> str:
    """Strip the markdown Exa leaks (### headings, [label](url) links) and drop
    exact repeated phrases so the evidence quote reads like prose."""
    text = _MD_LINK_RE.sub(r"\1", text or "")
    text = _MD_HEADING_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # collapse "X X" where a phrase of >= 4 words repeats back to back
    words = text.split()
    out: list[str] = []
    i = 0
    while i < len(words):
        repeated = False
        for size in range(min(12, (len(words) - i) // 2), 3, -1):
            if words[i:i + size] == words[i + size:i + 2 * size]:
                out.extend(words[i:i + size])
                i += 2 * size
                repeated = True
                break
        if not repeated:
            out.append(words[i])
            i += 1
    return " ".join(out)


def parse_exa_text(text: str) -> list[dict[str, Any]]:
    """Turn mcporter's plain-text Exa output into [{url, title, snippet, headline, published}].

    Blocks start with ``Title:`` and are separated by blank lines; ``Highlights:``
    lines follow, with ``...`` separators we drop. ``headline`` is the first
    highlight line, which for LinkedIn results is the person's current title.
    """
    results: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    highlights: list[str] = []
    in_highlights = False

    def flush() -> None:
        nonlocal current, highlights, in_highlights
        if current and current.get("url"):
            kept = [h for h in highlights if h and h != "..."]
            current["headline"] = clean_snippet(kept[0])[:200] if kept else ""
            current["snippet"] = clean_snippet(" ".join(kept))[:1200]
            results.append(current)
        current, highlights, in_highlights = None, [], False

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if line.startswith("Title:"):
            flush()
            current = {"title": line[6:].strip(), "url": "", "snippet": "", "published": None}
            continue
        if current is None:
            continue
        if line.startswith("URL:"):
            current["url"] = line[4:].strip()
        elif line.startswith("Published:"):
            value = line[10:].strip()
            current["published"] = None if value in ("", "N/A") else value
        elif line.startswith("Author:"):
            continue
        elif line.startswith("Highlights:"):
            in_highlights = True
        elif in_highlights:
            highlights.append(line.strip())
    flush()
    return results


def exa_search(query: str, category: str | None = None, num_results: int = 10) -> list[dict[str, Any]]:
    """Run one Exa search.  ``category`` may be 'people' or 'company' (Exa's
    LinkedIn-backed indexes) or None for the open web."""
    global _last_call, _last_error
    if "@" in query:
        raise ValueError("search queries must never contain '@' (no email pattern guessing)")
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {sorted(c for c in CATEGORIES if c)} or None")
    full_query = f"category:{category} {query}" if category else query

    wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()

    cmd = [_mcporter_bin(), "call", SERVER, TOOL, "--args",
           json.dumps({"query": full_query, "numResults": int(num_results)})]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=TIMEOUT_SECONDS, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _last_error = f"{type(exc).__name__}: {exc}"
        return []
    if proc.returncode != 0:
        _last_error = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:500]
        return []
    _last_error = ""
    return parse_exa_text(proc.stdout)


def people_search_fn(query: str) -> list[dict[str, Any]]:
    """search_fn adapter for reach.people_discovery.discover_public."""
    return exa_search(query, category="people", num_results=10)


def web_search_fn(query: str) -> list[dict[str, Any]]:
    return exa_search(query, category=None, num_results=10)


def doctor() -> dict[str, Any]:
    """Cheap live probe used by the API status endpoint and the skill."""
    if not available():
        return {"ok": False, "reason": "mcporter not on PATH (npm install -g mcporter)"}
    results = exa_search("Deloitte Morocco careers page", num_results=1)
    if results:
        return {"ok": True, "reason": "exa reachable"}
    return {"ok": False, "reason": last_error() or "exa returned no results"}
