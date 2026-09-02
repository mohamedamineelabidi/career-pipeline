"""Fetch full job descriptions for opportunities that lack one (read-only GETs).

Usage:
    uv run python fetch_job_descriptions.py --db career_pipeline_v2.sqlite3 [--limit N] [--dry-run]
    uv run python fetch_job_descriptions.py --db career_pipeline_v2.sqlite3 --use-reader [--limit N] [--dry-run]

``--use-reader`` retries rows whose previous fetch was ``blocked`` / ``error:*`` through
agent_reach_channel (direct GET -> Jina Reader -> blocked). Recovered rows get
jd_fetch_status='ok_reader' and source_json.jd_backend='jina'. LinkedIn/Glassdoor/Indeed
are never sent to Jina.

Safety: only public http(s) job pages are fetched with a plain GET. Login walls,
CAPTCHAs and anti-bot 403s are recorded, never bypassed. Nothing is submitted.
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import pipeline_v2

USER_AGENT = "Mozilla/5.0 (compatible; CareerPipelineV2 local)"
TIMEOUT_SECONDS = 20
MIN_DESCRIPTION_CHARS = 300
HOST_SLEEP_SECONDS = 1.5
LOGIN_WALL_HOSTS = ("glassdoor.",)
LOGIN_WALL_PATH_MARKERS = ("linkedin.com/login", "linkedin.com/authwall", "linkedin.com/uas/login", "linkedin.com/checkpoint")
ANTI_BOT_HOSTS = ("indeed.", "glassdoor.", "linkedin.")
BLOCK_MARKERS = (
    "verify you are a human", "are you a robot", "captcha", "access denied",
    "enable javascript and cookies to continue", "attention required! | cloudflare",
    "please sign in to continue", "sign in to view", "join now to see",
)


@dataclass
class FetchResult:
    status: int
    body: str
    final_url: str


def is_login_wall_url(url: str) -> bool:
    lowered = str(url or "").lower()
    if any(marker in lowered for marker in LOGIN_WALL_PATH_MARKERS):
        return True
    host = urlparse(lowered).netloc
    return any(marker in host for marker in LOGIN_WALL_HOSTS)


def default_fetcher(url: str) -> FetchResult:
    request = urllib.request.Request(url, method="GET", headers={
        "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en,fr;q=0.8",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(2_000_000)
            charset = response.headers.get_content_charset() or "utf-8"
            return FetchResult(response.status, raw.decode(charset, errors="replace"), response.geturl())
    except urllib.error.HTTPError as error:
        try:
            body = error.read(200_000).decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - defensive
            body = ""
        return FetchResult(error.code, body, error.geturl() or url)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "noscript", "svg", "iframe", "form"}
    BLOCK = {"div", "section", "article", "main", "li", "p", "ul", "ol", "td", "body", "h1", "h2", "h3", "h4", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.stack: list[list[str]] = [[]]
        self.blocks: list[str] = []
        self.json_ld: list[str] = []
        self._in_json_ld = False

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type", "").lower() == "application/ld+json":
            self._in_json_ld = True
            return
        if tag in self.SKIP:
            self.skip_depth += 1
            return
        if tag in self.BLOCK:
            self.stack.append([])

    def handle_endtag(self, tag):
        if tag == "script" and self._in_json_ld:
            self._in_json_ld = False
            return
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag in self.BLOCK and len(self.stack) > 1:
            parts = self.stack.pop()
            block = _normalize("\n".join(parts))
            if block:
                self.blocks.append(block)
                self.stack[-1].append(block)

    def handle_data(self, data):
        if self._in_json_ld:
            self.json_ld.append(data)
            return
        if self.skip_depth or not data.strip():
            return
        self.stack[-1].append(data)

    def finish(self):
        while len(self.stack) > 1:
            self.handle_endtag("div")
        root = _normalize("\n".join(self.stack[0]))
        if root:
            self.blocks.append(root)


def _normalize(value: str) -> str:
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s*\n+", "\n\n", value)
    return "\n".join(line.strip() for line in value.split("\n")).strip()


def _strip_html(fragment: str) -> str:
    parser = _TextExtractor()
    parser.feed(fragment)
    parser.finish()
    return parser.blocks[-1] if parser.blocks else _normalize(html_lib.unescape(re.sub(r"<[^>]+>", "\n", fragment)))


def _job_posting_description(raw_json: str) -> str:
    try:
        data = json.loads(raw_json.strip())
    except json.JSONDecodeError:
        return ""
    queue = [data]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "JobPosting" in kinds and node.get("description"):
                return _strip_html(html_lib.unescape(str(node["description"])))
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return ""


def extract_description(page_html: str) -> str:
    parser = _TextExtractor()
    parser.feed(page_html)
    parser.finish()
    for raw in parser.json_ld:
        found = _job_posting_description(raw)
        if len(found) >= 40:
            return found
    if not parser.blocks:
        return ""
    return max(parser.blocks, key=len)


def looks_blocked(body: str) -> bool:
    sample = body[:20000].lower()
    return any(marker in sample for marker in BLOCK_MARKERS)


def classify(url: str, result: FetchResult) -> tuple[str, str]:
    """Return (status_code, description)."""
    host = urlparse(url).netloc.lower()
    if is_login_wall_url(result.final_url):
        return "login_wall", ""
    if result.status == 403 and any(marker in host for marker in ANTI_BOT_HOSTS):
        return "login_wall", ""
    if result.status in (401, 403, 429, 503) and looks_blocked(result.body):
        return "blocked", ""
    if result.status != 200:
        return f"error:{result.status}", ""
    if looks_blocked(result.body):
        return "blocked", ""
    description = extract_description(result.body)
    if len(description) < MIN_DESCRIPTION_CHARS:
        return "error:too_short", ""
    return "ok", description


def candidates(connection) -> list[dict]:
    rows = connection.execute(
        "SELECT id, url, description, source_json, updated_at FROM opportunities "
        "WHERE status != 'closed' ORDER BY priority_score DESC, id"
    ).fetchall()
    out = []
    for row in rows:
        url = str(row["url"] or "").strip()
        if len(str(row["description"] or "")) >= MIN_DESCRIPTION_CHARS:
            continue
        if not url.lower().startswith(("http://", "https://")):
            continue
        out.append(dict(row))
    return out


def _already_labeled_anti_bot(row: dict) -> bool:
    """Anti-bot hosts already marked blocked/login_wall: never re-spend the direct-fetch budget on them."""
    host = urlparse(str(row.get("url") or "")).netloc.lower()
    status = str(pipeline_v2._source_fields(row).get("jd_fetch_status") or "")
    return any(marker in host for marker in ANTI_BOT_HOSTS) and status in ("login_wall", "blocked")


def run(db_path, *, fetcher=default_fetcher, sleep=time.sleep, limit: int | None = None, dry_run: bool = False,
        log=lambda _msg: None) -> dict:
    counts = Counter()
    hosts_blocked: set[str] = set()
    last_host_at: dict[str, float] = {}
    with closing(pipeline_v2.connect(db_path)) as connection:
        todo = [row for row in candidates(connection) if not _already_labeled_anti_bot(row)]
        if limit is not None:
            todo = todo[:limit]
        counts["candidates"] = len(todo)
        for row in todo:
            url = row["url"]
            host = urlparse(url).netloc.lower()
            if is_login_wall_url(url):
                status, description = "login_wall", ""
            else:
                wait = HOST_SLEEP_SECONDS - (time.monotonic() - last_host_at.get(host, -1e9))
                if wait > 0:
                    sleep(wait)
                last_host_at[host] = time.monotonic()
                try:
                    result = fetcher(url)
                    status, description = classify(url, result)
                except Exception as error:  # network failures are recorded, never raised
                    status, description = f"error:{type(error).__name__}", ""
            counts[status.split(":")[0]] += 1
            if status in ("blocked", "login_wall"):
                hosts_blocked.add(host)
            log(f"{status:<14} {row['id']}  {url}")
            if dry_run:
                continue
            now = datetime.now(timezone.utc).isoformat()
            source = pipeline_v2._source_fields(row)
            source["jd_fetch_status"] = status
            source["jd_fetched_at"] = now
            if status == "ok":
                source["full_job_description"] = description
                connection.execute(
                    "UPDATE opportunities SET description=?, source_json=?, updated_at=? WHERE id=?",
                    (description, json.dumps(source, ensure_ascii=False), now, row["id"]),
                )
            else:
                connection.execute(
                    "UPDATE opportunities SET source_json=?, updated_at=? WHERE id=?",
                    (json.dumps(source, ensure_ascii=False), now, row["id"]),
                )
            connection.commit()
    summary = {key: counts.get(key, 0) for key in ("candidates", "ok", "blocked", "login_wall", "error")}
    summary["hosts_blocked"] = sorted(hosts_blocked)
    summary["dry_run"] = dry_run
    return summary


def format_summary(summary: dict) -> str:
    lines = ["result        count", "------------  -----"]
    for key in ("candidates", "ok", "blocked", "login_wall", "error"):
        lines.append(f"{key:<12}  {summary.get(key, 0):>5}")
    hosts = summary.get("hosts_blocked") or []
    lines.append("hosts blocked/login-walled: " + (", ".join(hosts) if hosts else "none"))
    if summary.get("dry_run"):
        lines.append("(dry run: nothing written)")
    return "\n".join(lines)


READER_RETRY_PREFIXES = ("blocked", "error")


def reader_candidates(connection) -> list[dict]:
    """Rows with a short description whose last direct fetch was blocked or errored."""
    out = []
    for row in candidates(connection):
        status = str(pipeline_v2._source_fields(row).get("jd_fetch_status") or "")
        if status.split(":")[0] in READER_RETRY_PREFIXES:
            out.append(row)
    return out


def run_reader(db_path, *, reader=None, limit: int | None = None, dry_run: bool = False,
               log=lambda _msg: None) -> dict:
    """Retry blocked/error rows via agent_reach_channel.read_url (injectable ``reader``)."""
    import agent_reach_channel

    reader = reader or agent_reach_channel.read_url
    counts = Counter()
    recovered_hosts: Counter = Counter()
    still_blocked_hosts: Counter = Counter()
    with closing(pipeline_v2.connect(db_path)) as connection:
        todo = reader_candidates(connection)
        if limit is not None:
            todo = todo[:limit]
        counts["candidates"] = len(todo)
        for row in todo:
            url = row["url"]
            host = urlparse(url).netloc.lower()
            try:
                result = reader(url)
            except Exception as error:  # never raise on network failures
                result = {"text": "", "status": f"error:{type(error).__name__}", "backend": "blocked", "attempts": []}
            status, backend, text = result.get("status", "blocked"), result.get("backend", "blocked"), result.get("text", "")
            if status == "ok" and backend == "jina":
                marker = "ok_reader"
                recovered_hosts[host] += 1
            elif status == "ok":
                marker = "ok"
                recovered_hosts[host] += 1
            else:
                marker = status if status in ("login_wall", "blocked") else "blocked"
                still_blocked_hosts[host] += 1
            counts[marker] += 1
            log(f"{marker:<14} {backend:<8} {row['id']}  {url}")
            if dry_run:
                continue
            now = datetime.now(timezone.utc).isoformat()
            source = pipeline_v2._source_fields(row)
            source["jd_fetch_status"] = marker
            source["jd_fetched_at"] = now
            source["jd_reader_attempts"] = result.get("attempts", [])
            if marker in ("ok", "ok_reader"):
                source["jd_backend"] = backend
                source["full_job_description"] = text
                connection.execute(
                    "UPDATE opportunities SET description=?, source_json=?, updated_at=? WHERE id=?",
                    (text, json.dumps(source, ensure_ascii=False), now, row["id"]),
                )
            else:
                connection.execute(
                    "UPDATE opportunities SET source_json=?, updated_at=? WHERE id=?",
                    (json.dumps(source, ensure_ascii=False), now, row["id"]),
                )
            connection.commit()
    summary = {key: counts.get(key, 0) for key in ("candidates", "ok_reader", "ok", "blocked", "login_wall")}
    summary["recovered_by_host"] = dict(sorted(recovered_hosts.items()))
    summary["still_blocked_by_host"] = dict(sorted(still_blocked_hosts.items()))
    summary["dry_run"] = dry_run
    return summary


def format_reader_summary(summary: dict) -> str:
    lines = ["result        count", "------------  -----"]
    for key in ("candidates", "ok_reader", "ok", "blocked", "login_wall"):
        lines.append(f"{key:<12}  {summary.get(key, 0):>5}")
    lines.append("recovered by host: " + (json.dumps(summary.get("recovered_by_host") or {}) ))
    lines.append("still blocked by host: " + (json.dumps(summary.get("still_blocked_by_host") or {})))
    if summary.get("dry_run"):
        lines.append("(dry run: nothing written)")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--use-reader", action="store_true",
                        help="retry blocked/error rows via agent_reach_channel (direct -> Jina Reader)")
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2
    if args.use_reader:
        summary = run_reader(args.db, limit=args.limit, dry_run=args.dry_run, log=print)
        print()
        print(format_reader_summary(summary))
        if not args.dry_run:
            pipeline_v2.record_automation_run(
                args.db, "fetch_job_descriptions_reader",
                "success" if summary["ok_reader"] + summary["ok"] else "no_change",
                summary["ok_reader"] + summary["ok"], json.dumps(summary, ensure_ascii=False),
            )
        return 0
    summary = run(args.db, limit=args.limit, dry_run=args.dry_run, log=print)
    print()
    print(format_summary(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
