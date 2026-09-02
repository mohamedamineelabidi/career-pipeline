"""Discovery layer: pull public job listings via JobSpy and sync them into the pipeline DB.

Inspired by speedyapply/JobSpy (multi-board scraper returning a DataFrame) and
cactus-commits/linkedin-scraper (LinkedIn public *guest* endpoints, no account).

Safety rules baked in:
- Read-only GETs of public listing pages only; no login, no CAPTCHA/anti-bot bypass, no proxies.
- Polite pacing: >= 2 s between queries hitting the same site (JobSpy already spaces requests).
- CONTENT-HASH SYNC: re-scans never touch user-owned fields (status, archive_reason, notes).
  Only description/title/company/location/deadline/publication_date/source_json + updated_at
  change, and only when the sha256 content hash differs.

CLI:
    uv run python job_sources.py --db career_pipeline_v2.sqlite3 [--dry-run] [--limit N]
        [--queries job_search_queries.json] [--only-query INDEX ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import time
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import pipeline_v2

DEFAULT_QUERIES_PATH = Path(__file__).with_name("job_search_queries.json")
SUPPORTED_SITES = ("linkedin", "indeed", "glassdoor", "google")
MIN_SECONDS_BETWEEN_QUERIES = 2.0
JOB_NAME = "job_sources_discovery"

# JobSpy `country_indeed` accepts these names (Indeed/Glassdoor subdomain lookup).
COUNTRY_ALIASES = {
    "ma": "morocco", "maroc": "morocco", "morocco": "morocco",
    "fr": "france", "france": "france",
    "ca": "canada", "canada": "canada", "quebec": "canada", "québec": "canada",
    "ae": "united arab emirates", "uae": "united arab emirates",
    "united arab emirates": "united arab emirates",
    "us": "usa", "usa": "usa", "united states": "usa",
    "uk": "uk", "united kingdom": "uk",
}

DEFAULT_QUERIES = [
    {"name": "Morocco — Data/AI engineer", "search_term": "data engineer OR machine learning engineer OR AI engineer",
     "location": "Rabat, Morocco", "country": "morocco", "sites": ["linkedin", "indeed"],
     "hours_old": 168, "job_type": None, "is_remote": False, "results_wanted": 25},
    {"name": "Morocco — GenAI / LLM", "search_term": "generative AI engineer OR LLM engineer OR NLP engineer",
     "location": "Casablanca, Morocco", "country": "morocco", "sites": ["linkedin", "indeed"],
     "hours_old": 168, "job_type": None, "is_remote": False, "results_wanted": 20},
    {"name": "Morocco — internship / PFE", "search_term": "stage PFE data science OR stage machine learning OR stage intelligence artificielle",
     "location": "Morocco", "country": "morocco", "sites": ["linkedin", "indeed"],
     "hours_old": 336, "job_type": "internship", "is_remote": False, "results_wanted": 20},
    {"name": "France — junior Data/ML engineer", "search_term": "ingénieur data junior OR machine learning engineer junior OR data engineer 2 ans",
     "location": "Paris, France", "country": "france", "sites": ["linkedin", "indeed"],
     "hours_old": 168, "job_type": None, "is_remote": False, "results_wanted": 25},
    {"name": "France — GenAI engineer", "search_term": "ingénieur IA générative OR LLM engineer OR NLP engineer",
     "location": "France", "country": "france", "sites": ["linkedin", "indeed"],
     "hours_old": 168, "job_type": None, "is_remote": False, "results_wanted": 20},
    {"name": "Quebec — Data/AI engineer", "search_term": "data engineer OR machine learning engineer OR AI engineer",
     "location": "Montreal, QC, Canada", "country": "canada", "sites": ["linkedin", "indeed"],
     "hours_old": 168, "job_type": None, "is_remote": False, "results_wanted": 25},
    {"name": "Canada — junior ML / GenAI", "search_term": "junior machine learning engineer OR generative AI engineer OR data scientist 1-3 years",
     "location": "Canada", "country": "canada", "sites": ["linkedin", "indeed"],
     "hours_old": 168, "job_type": None, "is_remote": False, "results_wanted": 20},
    {"name": "UAE — Data/AI engineer", "search_term": "data engineer OR machine learning engineer OR AI engineer",
     "location": "Dubai, United Arab Emirates", "country": "united arab emirates", "sites": ["linkedin", "indeed"],
     "hours_old": 168, "job_type": None, "is_remote": False, "results_wanted": 25},
    {"name": "Remote — Data/ML engineer", "search_term": "remote data engineer OR remote machine learning engineer",
     "location": "", "country": "france", "sites": ["linkedin", "indeed"],
     "hours_old": 168, "job_type": None, "is_remote": True, "results_wanted": 25},
    {"name": "Remote — GenAI / LLM engineer", "search_term": "remote generative AI engineer OR remote LLM engineer",
     "location": "", "country": "canada", "sites": ["linkedin", "indeed"],
     "hours_old": 168, "job_type": None, "is_remote": True, "results_wanted": 20},
]


# --------------------------------------------------------------------------- config

def default_config() -> dict:
    return {
        "version": 1,
        "notes": "Discovery queries for job_sources.py (JobSpy). Read-only public listings; never applies.",
        "queries": DEFAULT_QUERIES,
    }


def load_queries(path: Path | str = DEFAULT_QUERIES_PATH, *, create_default: bool = True) -> list[dict]:
    path = Path(path)
    if not path.exists():
        if not create_default:
            raise FileNotFoundError(path)
        path.write_text(json.dumps(default_config(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    payload = json.loads(path.read_text(encoding="utf-8"))
    queries = payload.get("queries") if isinstance(payload, dict) else payload
    if not isinstance(queries, list):
        raise ValueError("queries config must contain a list under 'queries'")
    return [normalize_query(query) for query in queries]


def normalize_query(query: dict) -> dict:
    sites = [str(site).casefold() for site in (query.get("sites") or ["linkedin", "indeed"])]
    unknown = [site for site in sites if site not in SUPPORTED_SITES]
    if unknown:
        raise ValueError(f"unsupported sites: {unknown}; allowed {SUPPORTED_SITES}")
    country = str(query.get("country") or "morocco").strip().casefold()
    return {
        "name": str(query.get("name") or query.get("search_term") or "query"),
        "search_term": str(query.get("search_term") or "").strip(),
        "location": str(query.get("location") or "").strip(),
        "country": COUNTRY_ALIASES.get(country, country),
        "sites": sites,
        "hours_old": int(query["hours_old"]) if query.get("hours_old") else None,
        "job_type": (str(query["job_type"]).casefold() if query.get("job_type") else None),
        "is_remote": bool(query.get("is_remote", False)),
        "results_wanted": max(1, int(query.get("results_wanted") or 15)),
    }


# --------------------------------------------------------------------------- scraping

def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def _number(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _iso_date(value) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    text = str(value).strip()
    return text[:10] if text and text.casefold() not in {"nat", "none", "nan"} else None


def default_scraper(query: dict) -> list[dict]:
    """Real JobSpy call. Returns list of plain dict records (DataFrame rows)."""
    from jobspy import scrape_jobs  # imported lazily so tests never need the network

    kwargs = {
        "site_name": query["sites"],
        "search_term": query["search_term"] or None,
        "location": query["location"] or None,
        "results_wanted": query["results_wanted"],
        "country_indeed": query["country"],
        "is_remote": query["is_remote"],
        "description_format": "markdown",
        "linkedin_fetch_description": True,
        "verbose": 0,
    }
    if query.get("hours_old"):
        kwargs["hours_old"] = query["hours_old"]
    if query.get("job_type"):
        kwargs["job_type"] = query["job_type"]
    if "google" in query["sites"]:
        kwargs["google_search_term"] = f"{query['search_term']} jobs {query['location']}".strip()
    frame = scrape_jobs(**kwargs)
    if frame is None or len(frame) == 0:
        return []
    frame = frame.astype(object).where(frame.notna(), None)
    return frame.to_dict(orient="records")


# --------------------------------------------------------------------------- mapping

def content_hash(title: str, company: str, location: str, description: str, deadline) -> str:
    canonical = "|".join(_clean(part) for part in (title, company, location, description, deadline))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def map_job(record: dict, query: dict) -> dict | None:
    """Map one JobSpy record -> opportunity-schema dict (keys mirror the opportunities table)."""
    url = _clean(record.get("job_url") or record.get("url"))
    if not url.startswith("http"):
        return None
    title = _clean(record.get("title")) or "Untitled"
    company = _clean(record.get("company") or record.get("company_name")) or "Unknown"
    location = _clean(record.get("location"))
    description = _clean(record.get("description"))
    site = _clean(record.get("site")).casefold() or "jobspy"
    emails = record.get("emails")
    if isinstance(emails, str):
        emails = [part.strip() for part in emails.split(",") if part.strip()]
    elif not isinstance(emails, list):
        emails = []
    is_remote_raw = record.get("is_remote")
    is_remote = None if is_remote_raw is None else int(bool(is_remote_raw))
    salary_min = _number(record.get("min_amount"))
    salary_max = _number(record.get("max_amount"))
    publication_date = _iso_date(record.get("date_posted"))
    deadline = None
    source_json = {
        "source": site,
        "source_id": _clean(record.get("id")),
        "job_url_direct": _clean(record.get("job_url_direct")) or None,
        "job_type": _clean(record.get("job_type")) or None,
        "is_remote": None if is_remote is None else bool(is_remote),
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": _clean(record.get("currency")) or None,
        "salary_interval": _clean(record.get("interval")) or None,
        "salary_source": _clean(record.get("salary_source")) or None,
        "company_url": _clean(record.get("company_url")) or None,
        "emails": emails,
        "job_level": _clean(record.get("job_level")) or None,
        "job_function": _clean(record.get("job_function")) or None,
        "company_industry": _clean(record.get("company_industry")) or None,
        "discovered_by_query": query.get("name"),
        "discovery_country": query.get("country"),
        "jd_fetch_status": "fetched" if len(description) >= 200 else "listing_only",
        "full_job_description": description,
        "publication_date": publication_date,
        "content_hash": content_hash(title, company, location, description, deadline),
    }
    return {
        "title": title, "company": company, "location": location, "url": url,
        "source": site, "publication_date": publication_date, "description": description,
        "deadline": deadline, "job_type": source_json["job_type"], "is_remote": is_remote,
        "salary_min": salary_min, "salary_max": salary_max,
        "salary_currency": source_json["salary_currency"], "source_json": source_json,
    }


# --------------------------------------------------------------------------- persistence

def _scoring_input(mapped: dict, preserved: dict | None) -> dict:
    job = dict(preserved or {})
    job.update({k: v for k, v in mapped["source_json"].items() if k != "content_hash"})
    job.setdefault("freshness_status", "recent" if mapped.get("publication_date") else "unknown")
    job.setdefault("source_verification_status", "unverified")
    job["full_job_description"] = mapped["description"]
    return job


def upsert_opportunity(connection: sqlite3.Connection, mapped: dict, now: str) -> str:
    """Insert or content-hash-sync one opportunity. Returns 'inserted' | 'updated' | 'unchanged'."""
    opportunity_id = pipeline_v2.opportunity_identity({"url": mapped["url"]})
    existing = connection.execute(
        "SELECT id, content_hash, source_json FROM opportunities WHERE id=? OR url=? LIMIT 1",
        (opportunity_id, mapped["url"]),
    ).fetchone()
    new_hash = mapped["source_json"]["content_hash"]
    if existing:
        preserved = pipeline_v2._source_fields(dict(existing))
        stored_hash = existing["content_hash"] or preserved.get("content_hash")
        if stored_hash == new_hash:
            return "unchanged"
        merged_source = {**preserved, **mapped["source_json"], "jd_fetched_at": now}
        connection.execute(
            """UPDATE opportunities SET title=?, company=?, location=?, description=?, deadline=?,
                   publication_date=?, role_kind=?, job_type=?, is_remote=?, salary_min=?, salary_max=?,
                   salary_currency=?, content_hash=?, source_json=?, updated_at=?
               WHERE id=?""",
            (
                mapped["title"], mapped["company"], mapped["location"], mapped["description"],
                mapped["deadline"], mapped["publication_date"],
                pipeline_v2.classify_opportunity({"full_job_description": mapped["description"]}),
                mapped["job_type"], mapped["is_remote"], mapped["salary_min"], mapped["salary_max"],
                mapped["salary_currency"], new_hash,
                json.dumps(merged_source, ensure_ascii=False, sort_keys=True), now, existing["id"],
            ),
        )
        return "updated"
    job = _scoring_input(mapped, None)
    scoring = pipeline_v2.compute_opportunity_score(job)
    source_json = {**mapped["source_json"], "jd_fetched_at": now}
    connection.execute(
        """INSERT INTO opportunities(
               id, title, company, location, url, source, publication_date, role_kind, role_family,
               description, requirements, deadline, source_verification_status, fit_score,
               eligibility_status, freshness_status, verification_confidence, priority_score,
               score_schema_version, score_breakdown_json, archive_reason, match_score, status,
               source_json, created_at, updated_at, content_hash, job_type, is_remote,
               salary_min, salary_max, salary_currency
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            opportunity_id, mapped["title"], mapped["company"], mapped["location"], mapped["url"],
            mapped["source"], mapped["publication_date"],
            pipeline_v2.classify_opportunity(job), "", mapped["description"], "", mapped["deadline"],
            scoring["source_verification_status"], scoring["fit_score"], scoring["eligibility_status"],
            scoring["freshness_status"], scoring["verification_confidence"], scoring["priority_score"],
            scoring["score_schema_version"], scoring["score_breakdown_json"], scoring["archive_reason"],
            scoring["fit_score"], "discovered",
            json.dumps(source_json, ensure_ascii=False, sort_keys=True), now, now, new_hash,
            mapped["job_type"], mapped["is_remote"], mapped["salary_min"], mapped["salary_max"],
            mapped["salary_currency"],
        ),
    )
    return "inserted"


# --------------------------------------------------------------------------- orchestration

def discover(
    queries: list[dict],
    db_path,
    *,
    scraper: Callable[[dict], list[dict]] = default_scraper,
    sleep: Callable[[float], None] = time.sleep,
    limit: int | None = None,
    dry_run: bool = False,
    record_run: bool = True,
) -> dict:
    queries = [normalize_query(query) for query in queries]
    pipeline_v2.create_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    summary = {
        "queries_total": len(queries), "queries_ok": 0, "queries_blocked": 0,
        "records_seen": 0, "records_mapped": 0, "inserted": 0, "updated": 0, "unchanged": 0,
        "dry_run": dry_run, "limit": limit, "per_query": [],
    }
    seen_urls: set[str] = set()
    remaining = limit
    with closing(pipeline_v2.connect(db_path)) as connection:
        for index, query in enumerate(queries):
            if remaining is not None and remaining <= 0:
                break
            if index:
                sleep(MIN_SECONDS_BETWEEN_QUERIES)
            entry = {"name": query["name"], "sites": query["sites"], "status": "ok",
                     "records": 0, "inserted": 0, "updated": 0, "unchanged": 0, "error": ""}
            try:
                records = scraper(query)
            except Exception as error:  # network/anti-bot: report, never bypass
                entry.update(status="blocked", error=f"{type(error).__name__}: {error}"[:300])
                summary["queries_blocked"] += 1
                summary["per_query"].append(entry)
                continue
            summary["queries_ok"] += 1
            entry["records"] = len(records)
            summary["records_seen"] += len(records)
            connection.execute("BEGIN IMMEDIATE")
            try:
                for record in records:
                    if remaining is not None and remaining <= 0:
                        break
                    mapped = map_job(record, query)
                    if mapped is None or mapped["url"] in seen_urls:
                        continue
                    seen_urls.add(mapped["url"])
                    summary["records_mapped"] += 1
                    outcome = upsert_opportunity(connection, mapped, now)
                    entry[outcome] += 1
                    summary[outcome] += 1
                    if remaining is not None:
                        remaining -= 1
                if dry_run:
                    connection.rollback()
                else:
                    connection.commit()
            except Exception:
                connection.rollback()
                raise
            summary["per_query"].append(entry)
    if record_run and not dry_run:
        if summary["queries_ok"] == 0:
            status = "blocked"
        elif summary["queries_blocked"]:
            status = "partial"
        elif summary["inserted"] or summary["updated"]:
            status = "success"
        else:
            status = "no_change"
        pipeline_v2.record_automation_run(
            db_path, JOB_NAME, status,
            record_count=summary["inserted"] + summary["updated"],
            details=json.dumps({k: v for k, v in summary.items() if k != "per_query"}, sort_keys=True),
        )
        summary["automation_run_status"] = status
    return summary


def format_summary(summary: dict) -> str:
    lines = [
        f"queries ok={summary['queries_ok']} blocked={summary['queries_blocked']} / {summary['queries_total']}"
        f"  records={summary['records_seen']} mapped={summary['records_mapped']}"
        f"  inserted={summary['inserted']} updated={summary['updated']} unchanged={summary['unchanged']}"
        + ("  [DRY RUN]" if summary.get("dry_run") else ""),
    ]
    for entry in summary.get("per_query", []):
        lines.append(
            f"  - {entry['name']} [{','.join(entry['sites'])}]: {entry['status']} records={entry['records']}"
            f" +{entry['inserted']} ~{entry['updated']} ={entry['unchanged']}"
            + (f"  ({entry['error']})" if entry.get("error") else "")
        )
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True)
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES_PATH))
    parser.add_argument("--only-query", type=int, action="append", default=None,
                        help="0-based index into the queries list; repeatable")
    parser.add_argument("--limit", type=int, default=None, help="max opportunities processed overall")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    queries = load_queries(args.queries)
    if args.only_query:
        queries = [queries[i] for i in args.only_query if 0 <= i < len(queries)]
    summary = discover(queries, args.db, limit=args.limit, dry_run=args.dry_run)
    print(format_summary(summary))
    return 0 if summary["queries_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
