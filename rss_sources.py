"""RSS / public-board channel (Agent Reach "RSS" channel): discover NEW opportunities only.

Usage:
    uv run python rss_sources.py --db career_pipeline_v2.sqlite3 [--feeds job_rss_feeds.json] [--dry-run] [--limit N]

Rules: read-only GETs of public feeds; inserts only URLs not already present in
``opportunities``; NEVER updates existing rows (status, description, anything).
source='rss:<host>', description from entry summary/content, publication_date from
published. Records an automation run (job 'rss_sources').
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
import time
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pipeline_v2

USER_AGENT = "Mozilla/5.0 (compatible; CareerPipelineV2 local RSS)"
TIMEOUT_SECONDS = 25
FEED_SLEEP_SECONDS = 2.0
DEFAULT_FEEDS = Path(__file__).with_name("job_rss_feeds.json")
TITLE_HINTS = ("data", "machine learning", "ml ", " ai", "ai ", "analytics", "deep learning", "nlp", "llm",
               "computer vision", "python", "engineer", "scientist", "intern", "stage", "pfe")


def default_http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read(5_000_000)


def _strip_html(fragment: str) -> str:
    text = re.sub(r"<(br|/p|/li|/div|/h\d)[^>]*>", "\n", str(fragment or ""), flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def _iso_date(value) -> str | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / (1000 if value > 1e11 else 1), tz=timezone.utc).date().isoformat()
    text = str(value).strip()
    match = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if match:
        return match.group(1)
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(text).date().isoformat()
    except Exception:
        return None


def _split_title(title: str) -> tuple[str, str]:
    """'Company: Role' / 'Role at Company' patterns common in WWR / RemoteOK feeds."""
    title = str(title or "").strip()
    if ": " in title:
        company, role = title.split(": ", 1)
        if len(company) < 60:
            return role.strip(), company.strip()
    match = re.match(r"^(.*)\s+at\s+([^@]+)$", title)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return title, ""


def parse_rss(raw: bytes, feed: dict) -> list[dict]:
    import feedparser

    parsed = feedparser.parse(raw)
    items = []
    for entry in parsed.entries:
        url = str(entry.get("link") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        content = ""
        if entry.get("content"):
            content = entry["content"][0].get("value", "")
        summary = content or entry.get("summary", "") or entry.get("description", "")
        role, company = _split_title(entry.get("title", ""))
        # WP Job Manager feeds (Jobicy etc.) carry the real employer in a namespaced element;
        # feedparser flattens it to job_listing_company. Prefer it over the feed name/author.
        company = company or str(entry.get("job_listing_company") or entry.get("author") or "").strip()
        company = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", company).strip()
        published = None
        for key in ("published_parsed", "updated_parsed"):
            if entry.get(key):
                published = time.strftime("%Y-%m-%d", entry[key])
                break
        published = published or _iso_date(entry.get("published") or entry.get("updated"))
        tags = [t.get("term", "") for t in entry.get("tags", [])] if entry.get("tags") else []
        location = str(entry.get("job_listing_location") or "").strip() or next(
            (t for t in tags if re.search(r"remote|europe|canada|worldwide|anywhere", t, re.I)), "")
        items.append({"title": role, "company": company, "url": url, "description": _strip_html(summary),
                      "publication_date": published, "location": location, "tags": tags})
    return items


def parse_ashby_json(raw: bytes, feed: dict) -> list[dict]:
    data = json.loads(raw.decode("utf-8", errors="replace"))
    company = re.sub(r"^Ashby - ", "", str(feed.get("name") or ""))
    items = []
    for job in data.get("jobs", []):
        if not job.get("isListed", True):
            continue
        url = job.get("jobUrl") or job.get("applyUrl") or ""
        items.append({"title": job.get("title", ""), "company": company, "url": url,
                      "description": _strip_html(job.get("descriptionHtml") or job.get("descriptionPlain") or ""),
                      "publication_date": _iso_date(job.get("publishedAt")), "location": job.get("location", ""),
                      "tags": [job.get("department", ""), job.get("employmentType", "")]})
    return items


def parse_workable_json(raw: bytes, feed: dict) -> list[dict]:
    data = json.loads(raw.decode("utf-8", errors="replace"))
    company = re.sub(r"^Workable - ", "", str(feed.get("name") or ""))
    account = urlparse(feed["url"]).path.rstrip("/").split("/")[-2]
    items = []
    for job in data.get("results", []):
        shortcode = job.get("shortcode", "")
        url = f"https://apply.workable.com/{account}/j/{shortcode}/" if shortcode else ""
        loc = job.get("location") or {}
        items.append({"title": job.get("title", ""), "company": company, "url": url,
                      "description": _strip_html(job.get("description") or ""),
                      "publication_date": _iso_date(job.get("published")),
                      "location": ", ".join(filter(None, [loc.get("city"), loc.get("country")])) or ("Remote" if job.get("remote") else ""),
                      "tags": [job.get("department", ""), job.get("type", "")]})
    return items


def parse_lever_json(raw: bytes, feed: dict) -> list[dict]:
    data = json.loads(raw.decode("utf-8", errors="replace"))
    company = re.sub(r"^Lever - | \(.*\)$", "", str(feed.get("name") or ""))
    return [{"title": j.get("text", ""), "company": company, "url": j.get("hostedUrl", ""),
             "description": _strip_html(j.get("descriptionPlain") or j.get("description") or ""),
             "publication_date": _iso_date(j.get("createdAt")),
             "location": (j.get("categories") or {}).get("location", ""), "tags": [(j.get("categories") or {}).get("team", "")]}
            for j in data]


def parse_greenhouse_json(raw: bytes, feed: dict) -> list[dict]:
    data = json.loads(raw.decode("utf-8", errors="replace"))
    company = re.sub(r"^Greenhouse - | \(.*\)$", "", str(feed.get("name") or ""))
    return [{"title": j.get("title", ""), "company": company, "url": j.get("absolute_url", ""),
             "description": _strip_html(html_lib.unescape(j.get("content") or "")),
             "publication_date": _iso_date(j.get("updated_at")),
             "location": (j.get("location") or {}).get("name", ""), "tags": []}
            for j in data.get("jobs", [])]


PARSERS = {"rss": parse_rss, "ashby_json": parse_ashby_json, "workable_json": parse_workable_json,
           "lever_json": parse_lever_json, "greenhouse_json": parse_greenhouse_json}


def parse_remoteok_json(raw: bytes, feed: dict) -> list[dict]:
    """RemoteOK public API (https://remoteok.com/api): first element is a legal notice, skip it."""
    data = json.loads(raw.decode("utf-8", errors="replace"))
    items = []
    for job in data:
        if not isinstance(job, dict) or not job.get("url") or not job.get("position"):
            continue
        items.append({"title": job.get("position", ""), "company": job.get("company", ""), "url": job["url"],
                      "description": _strip_html(job.get("description") or ""),
                      "publication_date": _iso_date(job.get("date")), "location": job.get("location", ""),
                      "tags": list(job.get("tags") or [])})
    return items


PARSERS["remoteok_json"] = parse_remoteok_json


def _passes_filter(item: dict, feed: dict) -> bool:
    keywords = feed.get("keyword_filter")
    if not keywords:
        return True
    haystack = f" {item.get('title', '')} {item.get('description', '')[:400]} ".lower()
    return any(k.lower() in haystack for k in keywords)


def load_feeds(path: Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [f for f in data.get("feeds", []) if f.get("enabled", True) and f.get("url")]


def build_job(item: dict, feed: dict, now: str) -> dict:
    host = urlparse(item["url"]).netloc.lower()
    description = str(item.get("description") or "")
    return {
        "title": item.get("title") or "Untitled",
        "company": item.get("company") or feed.get("name") or "Unknown",
        "location": item.get("location") or "",
        "url": item["url"],
        "source": f"rss:{host}",
        "publication_date": item.get("publication_date"),
        "description": description,
        "summary": description[:500],
        "rss_feed": feed.get("name"),
        "rss_feed_url": feed.get("url"),
        "rss_tags": [t for t in item.get("tags", []) if t],
        "discovered_at": now,
        "fit_score": 50,
        "eligibility_status": "unknown",
        "freshness_status": "recent" if item.get("publication_date") else "unknown",
        "source_verification_status": "unverified",
        "jd_fetch_status": "ok" if len(description) >= 300 else "",
    }


def insert_new(connection, job: dict, now: str) -> bool:
    if connection.execute("SELECT 1 FROM opportunities WHERE url=? LIMIT 1", (job["url"],)).fetchone():
        return False
    scoring = pipeline_v2.compute_opportunity_score(job)
    opportunity_id = pipeline_v2.stable_id("opp", job["url"])
    if connection.execute("SELECT 1 FROM opportunities WHERE id=?", (opportunity_id,)).fetchone():
        return False
    source = {k: v for k, v in job.items() if k != "description"}
    if job.get("description"):
        source["full_job_description"] = job["description"]
    connection.execute(
        """INSERT INTO opportunities(id, title, company, location, url, source, publication_date, role_kind,
               role_family, description, requirements, deadline, source_verification_status, fit_score,
               eligibility_status, freshness_status, verification_confidence, priority_score,
               score_schema_version, score_breakdown_json, archive_reason, match_score, status, source_json,
               created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (opportunity_id, job["title"], job["company"], job["location"], job["url"], job["source"],
         job.get("publication_date"), pipeline_v2.classify_opportunity(job), "", job.get("description", ""), "",
         None, scoring["source_verification_status"], scoring["fit_score"], scoring["eligibility_status"],
         scoring["freshness_status"], scoring["verification_confidence"], scoring["priority_score"],
         scoring["score_schema_version"], scoring["score_breakdown_json"], scoring["archive_reason"],
         scoring["fit_score"], "discovered", json.dumps(source, ensure_ascii=False, sort_keys=True), now, now),
    )
    return True


def run(db_path, *, feeds: list[dict], http_get=default_http_get, sleep=time.sleep, limit: int | None = None,
        dry_run: bool = False, log=lambda _m: None, per_feed_limit: int | None = None) -> dict:
    """``limit`` caps total NEW inserts; ``per_feed_limit`` caps NEW inserts per feed (so late feeds are not starved)."""
    summary = {"feeds": len(feeds), "entries": 0, "new": 0, "skipped_existing": 0, "filtered": 0,
               "feed_errors": {}, "new_by_source": {}, "dry_run": dry_run}
    inserted = 0
    with closing(pipeline_v2.connect(db_path)) as connection:
        for index, feed in enumerate(feeds):
            if limit is not None and inserted >= limit:
                break
            if index:
                sleep(FEED_SLEEP_SECONDS)
            parser = PARSERS.get(feed.get("type", "rss"), parse_rss)
            try:
                items = parser(http_get(feed["url"]), feed)
            except Exception as error:
                summary["feed_errors"][feed["url"]] = f"{type(error).__name__}: {error}"[:200]
                log(f"error     {feed.get('name')}: {type(error).__name__}")
                continue
            summary["entries"] += len(items)
            now = datetime.now(timezone.utc).isoformat()
            feed_inserted = 0
            for item in items:
                if limit is not None and inserted >= limit:
                    break
                if per_feed_limit is not None and feed_inserted >= per_feed_limit:
                    break
                if not item.get("url"):
                    continue
                if not _passes_filter(item, feed):
                    summary["filtered"] += 1
                    continue
                job = build_job(item, feed, now)
                exists = connection.execute("SELECT 1 FROM opportunities WHERE url=? LIMIT 1", (job["url"],)).fetchone()
                if exists:
                    summary["skipped_existing"] += 1
                    continue
                log(f"new       {job['source']:<32} {job['title'][:60]}  {job['url']}")
                inserted += 1
                feed_inserted += 1
                summary["new_by_source"][job["source"]] = summary["new_by_source"].get(job["source"], 0) + 1
                if not dry_run:
                    insert_new(connection, job, now)
            if not dry_run:
                connection.commit()
    summary["new"] = inserted
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--feeds", type=Path, default=DEFAULT_FEEDS)
    parser.add_argument("--limit", type=int, default=None, help="max NEW opportunities to insert")
    parser.add_argument("--per-feed-limit", type=int, default=None, help="max NEW opportunities per feed")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2
    feeds = load_feeds(args.feeds)
    summary = run(args.db, feeds=feeds, limit=args.limit, per_feed_limit=args.per_feed_limit, dry_run=args.dry_run, log=print)
    print()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not args.dry_run:
        status = "success" if summary["new"] else ("partial" if summary["feed_errors"] else "no_change")
        pipeline_v2.record_automation_run(args.db, "rss_sources", status, summary["new"], json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
