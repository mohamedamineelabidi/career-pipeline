"""Morocco AI / Cloud / Data job radar built on top of ``job_sources``.

A radar query is a small config object (see ``reach/queries_morocco.json``)::

    {"keywords": "stage PFE data", "location": "Rabat",
     "role_kind": "internship", "role_family": "data_engineer"}

``run_radar`` translates each one into the JobSpy query shape ``job_sources``
already accepts, asks a *fetcher* for listings, and hands every listing to
``job_sources.upsert_opportunity`` — so the content-hash rule is the same one the
rest of the pipeline uses: user-owned fields (status, archive_reason, notes) are
never touched and a row is only rewritten when the sha256 of its content differs.

Storage conventions (the ``opportunities.role_kind`` column is a CHECK-constrained
classifier, ``role_family`` | ``exact_vacancy``, owned by ``pipeline_v2``):
- the radar's ``role_kind`` (internship | job) goes to ``job_type`` and
  ``source_json.role_kind``;
- ``role_family`` goes to the ``role_family`` column and ``source_json.role_family``;
- ``source_json`` additionally carries ``{"radar": <tag>, "query": <keywords>}``.

CLI::

    uv run python -m reach.morocco_radar --db PATH [--dry-run] [--limit N]
        [--queries reach/queries_morocco.json] [--tag morocco_ai_cloud]

Safety: read-only public listings only, no login, no CAPTCHA bypass, no proxies,
>= 2 s between queries hitting the same site.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import job_sources
import pipeline_v2

DEFAULT_QUERIES_PATH = Path(__file__).with_name("queries_morocco.json")
DEFAULT_TAG = "morocco_ai_cloud"
MIN_SECONDS_BETWEEN_QUERIES = job_sources.MIN_SECONDS_BETWEEN_QUERIES
LOCATIONS = ("Casablanca", "Rabat", "Morocco", "Maroc")
ROLE_KINDS = ("internship", "job")
ROLE_FAMILIES = ("ai_engineer", "data_engineer", "ml_engineer", "cloud_engineer", "mlops", "data_scientist")
DEFAULT_SITES = ["linkedin", "indeed"]

Fetcher = Callable[[dict], list[dict]]


# --------------------------------------------------------------------------- queries

def load_queries(path: Path | str = DEFAULT_QUERIES_PATH) -> list[dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    queries = payload.get("queries") if isinstance(payload, dict) else payload
    if not isinstance(queries, list):
        raise ValueError("radar queries config must be a JSON list")
    return [validate_query(query) for query in queries]


def validate_query(query: dict) -> dict:
    keywords = str(query.get("keywords") or "").strip()
    if not keywords:
        raise ValueError(f"radar query without keywords: {query!r}")
    location = str(query.get("location") or "").strip()
    role_kind = str(query.get("role_kind") or "").strip()
    role_family = str(query.get("role_family") or "").strip()
    if location not in LOCATIONS:
        raise ValueError(f"location {location!r} not in {LOCATIONS}")
    if role_kind not in ROLE_KINDS:
        raise ValueError(f"role_kind {role_kind!r} not in {ROLE_KINDS}")
    if role_family not in ROLE_FAMILIES:
        raise ValueError(f"role_family {role_family!r} not in {ROLE_FAMILIES}")
    return {"keywords": keywords, "location": location, "role_kind": role_kind, "role_family": role_family}


def to_job_sources_query(query: dict) -> dict:
    """Translate a radar query into the normalized shape ``job_sources`` consumes."""
    location = query["location"]
    if location in ("Morocco", "Maroc"):
        location_text = "Morocco"
    else:
        location_text = f"{location}, Morocco"
    is_internship = query["role_kind"] == "internship"
    return job_sources.normalize_query({
        "name": f"radar:{query['role_family']}:{query['role_kind']}:{query['keywords']}",
        "search_term": query["keywords"],
        "location": location_text,
        "country": "morocco",
        "sites": DEFAULT_SITES,
        "hours_old": 336 if is_internship else 168,
        "job_type": "internship" if is_internship else None,
        "is_remote": False,
        "results_wanted": 20,
    })


# --------------------------------------------------------------------------- fetching

def default_fetcher(query: dict) -> list[dict]:
    """Real path: delegate to job_sources' JobSpy / LinkedIn-guest scraper.

    Returns listing dicts with the keys ``run_radar`` expects. JobSpy-specific
    keys are kept so ``job_sources.map_job`` can still read salary/level facts.
    """
    records = job_sources.default_scraper(to_job_sources_query(query))
    listings = []
    for record in records:
        listing = dict(record)
        listing.setdefault("url", record.get("job_url"))
        listing.setdefault("publication_date", record.get("date_posted"))
        listing.setdefault("source", record.get("site"))
        listings.append(listing)
    return listings


def listing_to_record(listing: dict, query: dict) -> dict:
    """Listing dict -> the record shape ``job_sources.map_job`` understands."""
    record = dict(listing)
    record.setdefault("job_url", listing.get("url"))
    record.setdefault("date_posted", listing.get("publication_date"))
    record.setdefault("site", listing.get("source"))
    record["job_type"] = query["role_kind"]
    return record


# --------------------------------------------------------------------------- core

def run_radar(
    conn: sqlite3.Connection,
    queries: list[dict],
    fetcher: Fetcher,
    tag: str = DEFAULT_TAG,
    dry_run: bool = False,
    limit: int | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Run every radar query through ``fetcher`` and content-hash-sync the listings.

    Returns ``{'inserted': n, 'updated': n, 'skipped': n, 'errors': [...], ...}``.
    ``skipped`` counts listings whose content hash was unchanged, that were
    duplicates within the run, or that could not be mapped (no http URL).
    """
    queries = [validate_query(query) for query in queries]
    now = datetime.now(timezone.utc).isoformat()
    summary = {
        "inserted": 0, "updated": 0, "skipped": 0, "errors": [],
        "queries_total": len(queries), "queries_ok": 0, "records_seen": 0,
        "dry_run": dry_run, "limit": limit, "tag": tag, "per_query": [],
    }
    seen_urls: set[str] = set()
    remaining = limit
    for index, query in enumerate(queries):
        if remaining is not None and remaining <= 0:
            break
        if index:
            sleep(MIN_SECONDS_BETWEEN_QUERIES)  # polite pacing between queries to the same sites
        js_query = to_job_sources_query(query)
        entry = {"keywords": query["keywords"], "location": query["location"], "role_kind": query["role_kind"],
                 "records": 0, "inserted": 0, "updated": 0, "skipped": 0, "error": ""}
        try:
            listings = fetcher(query)
        except Exception as error:  # network / anti-bot: report, never bypass
            message = f"{query['keywords']} @ {query['location']}: {type(error).__name__}: {error}"[:300]
            entry["error"] = message
            summary["errors"].append(message)
            summary["per_query"].append(entry)
            continue
        summary["queries_ok"] += 1
        entry["records"] = len(listings)
        summary["records_seen"] += len(listings)
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            for listing in listings:
                if remaining is not None and remaining <= 0:
                    break
                mapped = job_sources.map_job(listing_to_record(listing, query), js_query)
                if mapped is None or mapped["url"] in seen_urls:
                    entry["skipped"] += 1
                    summary["skipped"] += 1
                    continue
                seen_urls.add(mapped["url"])
                mapped["job_type"] = query["role_kind"]
                mapped["source_json"].update({
                    "radar": tag, "query": query["keywords"],
                    "role_kind": query["role_kind"], "role_family": query["role_family"],
                    "job_type": query["role_kind"],
                })
                outcome = job_sources.upsert_opportunity(conn, mapped, now)
                if outcome == "unchanged":
                    entry["skipped"] += 1
                    summary["skipped"] += 1
                    continue
                # role_family is not part of job_sources' insert/update; set it here.
                # This never touches status / archive_reason / notes.
                conn.execute("UPDATE opportunities SET role_family=? WHERE url=?",
                             (query["role_family"], mapped["url"]))
                entry[outcome] += 1
                summary[outcome] += 1
                if remaining is not None:
                    remaining -= 1
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        summary["per_query"].append(entry)
    return summary


def format_summary(summary: dict) -> str:
    lines = [
        f"radar={summary['tag']} queries ok={summary['queries_ok']}/{summary['queries_total']}"
        f" errors={len(summary['errors'])}  records={summary['records_seen']}"
        f"  inserted={summary['inserted']} updated={summary['updated']} skipped={summary['skipped']}"
        + ("  [DRY RUN]" if summary.get("dry_run") else ""),
    ]
    for entry in summary.get("per_query", []):
        lines.append(
            f"  - [{entry['role_kind']}] {entry['keywords']} @ {entry['location']}: records={entry['records']}"
            f" +{entry['inserted']} ~{entry['updated']} ={entry['skipped']}"
            + (f"  ({entry['error']})" if entry.get("error") else "")
        )
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True)
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES_PATH))
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument("--limit", type=int, default=None, help="max opportunities processed overall")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    queries = load_queries(args.queries)
    pipeline_v2.create_schema(args.db)
    with closing(pipeline_v2.connect(args.db)) as conn:
        summary = run_radar(conn, queries, default_fetcher, tag=args.tag, dry_run=args.dry_run, limit=args.limit)
    print(format_summary(summary))
    return 0 if summary["queries_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
