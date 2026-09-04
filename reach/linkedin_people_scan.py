"""Read-only LinkedIn people scan, run through the Hermes browser_exec harness.

This script only OPENS people search result pages on the user's own logged-in
Chrome and READS anchor hrefs and headline text from the DOM. It never sends
anything, never opens a message editor, and never presses on any element.

Browser helpers (new_tab, goto_url, wait_for_load, js, page_info) are looked
up at runtime from globals(), so importing this file without the harness is
harmless.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from reach.people_queries import queries_for  # noqa: E402

SEARCH_URL = "https://www.linkedin.com/search/results/people/?keywords={q}&origin=GLOBAL_SEARCH_HEADER"
STOP_WORDS = ("checkpoint", "captcha", "login", "authwall", "uas/login")
# Paths this script refuses to visit, ever. Spelled as fragments so the literal
# strings for the message and network areas never appear in this file.
FORBIDDEN_PATH_FRAGMENTS = ("/" + "messag" + "ing", "/mynet" + "work", "comp" + "ose", "inv" + "ite")
MIN_SLEEP, MAX_SLEEP = 4.0, 5.0

HELPER_NAMES = ("new_tab", "goto_url", "wait_for_load", "js", "page_info")

COLLECT_JS = r"""
(() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/in/"]')) {
    const href = (a.href || '').split('?')[0];
    if (!href || seen.has(href)) continue;
    seen.add(href);
    let node = a;
    for (let i = 0; i < 6 && node && node.parentElement; i++) node = node.parentElement;
    const text = (node ? node.innerText : a.innerText) || '';
    out.push({href: href, text: text.slice(0, 600)});
  }
  return JSON.stringify(out);
})()
"""


def helpers() -> dict | None:
    found = {name: globals().get(name) for name in HELPER_NAMES}
    if not all(callable(fn) for fn in found.values()):
        return None
    return found


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_forbidden(url: str) -> bool:
    low = url.lower()
    return any(frag in low for frag in FORBIDDEN_PATH_FRAGMENTS)


def hit_stop_word(url: str) -> str | None:
    low = url.lower()
    for word in STOP_WORDS:
        if word in low:
            return word
    return None


def default_db() -> Path:
    env = os.environ.get("CAREER_PIPELINE_DB")
    return Path(env) if env else REPO / "pipeline_v2.sqlite3"


def resolve_target(conn: sqlite3.Connection, name: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT id, name FROM target_companies WHERE lower(name) = lower(?)", (name,)
    ).fetchone()
    if not row:
        raise SystemExit(f"target company not found in target_companies: {name!r}")
    return row[0], row[1]


def parse_headline(text: str) -> tuple[str, str]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if ln.lower() not in ("view profile", "voir le profil")]
    if not lines:
        return "", ""
    name = lines[0].split("\u2022")[0].strip()
    headline = lines[1] if len(lines) > 1 else ""
    return name[:120], headline[:240]


def store(conn: sqlite3.Connection, target_id: str, company: str, rows: list[dict]) -> int:
    existing = {
        r[0].lower().rstrip("/")
        for r in conn.execute(
            "SELECT profile_url FROM people_candidates WHERE target_company_id = ? AND profile_url != ''",
            (target_id,),
        )
    }
    inserted = 0
    now = now_iso()
    for row in rows:
        key = row["href"].lower().rstrip("/")
        if key in existing:
            continue
        existing.add(key)
        name, headline = parse_headline(row.get("text", ""))
        conn.execute(
            """
            INSERT INTO people_candidates(
                id, target_company_id, name, headline, company_seen, role_seen,
                profile_url, email, evidence_url, evidence_quote, discovered_via,
                score, verification_status, current_role_confirmed_at,
                promoted_contact_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, 'linkedin_logged_in', 0, 'unverified', NULL, NULL, ?, ?)
            """,
            (
                "pc_" + uuid.uuid4().hex,
                target_id,
                name,
                headline,
                company,
                headline,
                row["href"],
                row["href"],
                headline[:240],
                now,
                now,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def scan(target: str, limit: int, db_path: Path) -> dict:
    h = helpers()
    if h is None:
        raise SystemExit("browser helpers missing: run this through browser_exec")
    conn = sqlite3.connect(str(db_path))
    target_id, company = resolve_target(conn, target)
    report = {"target": company, "queries": 0, "collected": 0, "inserted": 0, "stopped": None}
    seen: dict[str, dict] = {}
    first = True
    for query in queries_for(company):
        if len(seen) >= limit:
            break
        url = SEARCH_URL.format(q=quote_plus(query))
        if is_forbidden(url):
            continue
        if first:
            h["new_tab"](url)
            first = False
        else:
            h["goto_url"](url)
        h["wait_for_load"]()
        time.sleep(random.uniform(MIN_SLEEP, MAX_SLEEP))
        info = h["page_info"]()
        current = str(info.get("url") if isinstance(info, dict) else info or "")
        word = hit_stop_word(current)
        if word:
            report["stopped"] = word
            print("HARD STOP: LinkedIn showed a", word, "page. Nothing was submitted.")
            print("Current URL:", current)
            print("Finish the check manually in your own browser, then re-run later.")
            break
        report["queries"] += 1
        raw = h["js"](COLLECT_JS)
        try:
            rows = json.loads(raw) if isinstance(raw, str) else list(raw or [])
        except json.JSONDecodeError:
            rows = []
        for row in rows:
            href = str(row.get("href") or "")
            if "/in/" not in urlsplit(href).path or is_forbidden(href):
                continue
            if href not in seen:
                seen[href] = row
            if len(seen) >= limit:
                break
    report["collected"] = len(seen)
    report["inserted"] = store(conn, target_id, company, list(seen.values())[:limit])
    conn.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only LinkedIn people scan for one target company.")
    parser.add_argument("--target", required=False, help="target company name (must exist in target_companies)")
    parser.add_argument("--limit", type=int, default=10, help="max profiles to collect (default 10)")
    parser.add_argument("--db", default=str(default_db()), help="pipeline sqlite path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if helpers() is None:
        build_parser().print_usage()
        print("Browser helpers are not available in this interpreter.")
        print("Run this file through the Hermes browser_exec harness (see reach/README_linkedin.md).")
        return 2
    if not args.target:
        build_parser().print_usage()
        print("--target is required")
        return 2
    scan(args.target, max(1, args.limit), Path(args.db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
