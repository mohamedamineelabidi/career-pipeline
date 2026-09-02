"""One-click, staged pipeline run (idea: ApplyPilot stage pipeline, career-ops nightly scan).

Stages: discover -> fetch_descriptions -> match -> llm_score -> digest.
Every stage is read/derive/draft only. This module never sends anything, never opens a
job portal form, never changes an opportunity status on the user's behalf.
Runs are persisted in ``pipeline_runs`` so the dashboard can show a timeline and the
latest digest; failures in one stage are recorded and the next stage still runs.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import traceback
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pipeline_v2
from pipeline_v2 import NotFoundError, PathLike, ValidationError, connect

STAGE_ORDER = ("discover", "fetch_descriptions", "match", "llm_score", "digest")
STAGE_LABELS = {
    "discover": "Discover new jobs (RSS/ATS feeds + saved searches)",
    "fetch_descriptions": "Fetch missing job descriptions",
    "match": "Refresh local semantic match",
    "llm_score": "Score new jobs with the Groq rubric",
    "digest": "Write the morning digest",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stages_json TEXT NOT NULL DEFAULT '[]',
    log TEXT NOT NULL DEFAULT '',
    digest TEXT NOT NULL DEFAULT ''
);
"""
STAGE_SLEEP_SECONDS = 1.0
_LOCK = threading.Lock()
_RUNNING: dict[str, threading.Thread] = {}

Log = Callable[[str], None]
Stage = Callable[[PathLike, Log], dict[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(db_path: PathLike) -> None:
    with closing(connect(db_path)) as connection:
        connection.executescript(SCHEMA)
        connection.commit()


# ---------------------------------------------------------------- real stages
def stage_discover(db_path: PathLike, log: Log) -> dict[str, Any]:
    import job_sources
    import rss_sources

    out: dict[str, Any] = {}
    feeds_path = Path(rss_sources.__file__).with_name("job_rss_feeds.json")
    if feeds_path.exists():
        rss = rss_sources.run(db_path, feeds=rss_sources.load_feeds(feeds_path), log=log, per_feed_limit=15)
        out["rss_new"] = rss.get("new", 0)
        out["rss_errors"] = len(rss.get("feed_errors", {}))
    queries = job_sources.load_queries(create_default=False)
    if queries:
        found = job_sources.discover(queries, db_path, limit=40, record_run=False)
        out["search_new"] = found.get("inserted", found.get("new", 0))
        out["queries_blocked"] = found.get("queries_blocked", 0)
    log(f"discover: {out}")
    return out


def stage_fetch_descriptions(db_path: PathLike, log: Log) -> dict[str, Any]:
    import fetch_job_descriptions

    result = fetch_job_descriptions.run(db_path, limit=25, log=log)
    return {key: value for key, value in result.items() if not isinstance(value, (list, dict))} or dict(result)


def stage_match(db_path: PathLike, log: Log) -> dict[str, Any]:
    import semantic_match

    result = semantic_match.recompute(db_path, all_opportunities=True)
    log(f"match: scored={result.get('scored', result.get('updated'))}")
    return {k: v for k, v in result.items() if not isinstance(v, (list, dict))}


def stage_llm_score(db_path: PathLike, log: Log) -> dict[str, Any]:
    import llm_client
    import llm_scoring

    if not llm_client.llm_available():
        log("llm_score: skipped, GROQ_API_KEY not configured")
        return {"skipped": "llm unavailable"}
    result = llm_scoring.score_all(db_path, limit=15, only_missing=True)
    log(f"llm_score: scored={result['scored']} stopped={result.get('stopped_reason')}")
    return {"scored": result["scored"], "failed": len(result["failed"]),
            "stopped_reason": result.get("stopped_reason"), "fit_distribution": result["fit_distribution"]}


def stage_digest(db_path: PathLike, log: Log) -> dict[str, Any]:
    text = build_digest(db_path)
    log("digest: written")
    return {"digest": text}


REAL_STAGES: dict[str, Stage] = {
    "discover": stage_discover,
    "fetch_descriptions": stage_fetch_descriptions,
    "match": stage_match,
    "llm_score": stage_llm_score,
    "digest": stage_digest,
}


# ---------------------------------------------------------------- digest
def build_digest(db_path: PathLike, since_hours: int = 24) -> str:
    """Plain-text morning digest. Read only; every line is something the user decides on."""
    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat(timespec="seconds")
    today = datetime.now(timezone.utc).date().isoformat()
    lines = [f"Career Pipeline digest, {today}", "Draft-only workflow: nothing below was sent or submitted.", ""]
    with closing(connect(db_path)) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        has_llm = "llm_scores" in tables

        new_rows = connection.execute(
            "SELECT company, title, url FROM opportunities WHERE created_at >= ? ORDER BY priority_score DESC LIMIT 10",
            (since,)).fetchall()
        lines.append(f"New this run ({len(new_rows)})")
        lines += [f"  - {r['company']}: {r['title']}" for r in new_rows] or ["  none"]
        lines.append("")

        fit_sql = ("SELECT o.company, o.title, o.fit_score, ls.fit AS llm_fit FROM opportunities o "
                   "LEFT JOIN llm_scores ls ON ls.opportunity_id = o.id "
                   if has_llm else
                   "SELECT o.company, o.title, o.fit_score, NULL AS llm_fit FROM opportunities o ")
        best = connection.execute(
            fit_sql + "WHERE o.status NOT IN ('closed','user_applied') "
            "ORDER BY COALESCE(ls.fit, 0) DESC, o.fit_score DESC LIMIT 5" if has_llm else
            fit_sql + "WHERE o.status NOT IN ('closed','user_applied') ORDER BY o.fit_score DESC LIMIT 5").fetchall()
        lines.append("Best fits to review")
        for r in best:
            score = f"AI {r['llm_fit']}" if r["llm_fit"] is not None else f"fit {r['fit_score']}"
            lines.append(f"  - {r['company']}: {r['title']} ({score})")
        if not best:
            lines.append("  none")
        lines.append("")

        follow = connection.execute(
            "SELECT company, title, updated_at FROM opportunities WHERE status='user_applied' "
            "AND updated_at < ? ORDER BY updated_at",
            ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec='seconds'),)).fetchall()
        lines.append(f"Follow up (applied more than 7 days ago) ({len(follow)})")
        lines += [f"  - {r['company']}: {r['title']} (applied {str(r['updated_at'])[:10]})" for r in follow] or ["  none"]
        lines.append("")

        due_count = 0
        if "outreach_steps" in tables:
            due_count = connection.execute(
                "SELECT COUNT(*) FROM outreach_steps WHERE state='draft' AND due_date <= ?", (today,)).fetchone()[0]
        lines.append(f"Due today: {due_count} outreach draft(s) to copy and send yourself")
        ready = 0
        if "application_preps" in tables:
            ready = connection.execute(
                "SELECT COUNT(*) FROM application_preps WHERE status='prepared_awaiting_user'").fetchone()[0]
        lines.append(f"Ready to submit: {ready} pre-filled form(s) waiting for your review")
    return "\n".join(lines)


# ---------------------------------------------------------------- runner
def _save(db_path: PathLike, run: dict[str, Any]) -> None:
    with closing(connect(db_path)) as connection:
        connection.execute(
            "INSERT INTO pipeline_runs(id, status, started_at, finished_at, stages_json, log, digest) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
            "finished_at=excluded.finished_at, stages_json=excluded.stages_json, log=excluded.log, digest=excluded.digest",
            (run["id"], run["status"], run["started_at"], run.get("finished_at"),
             json.dumps(run["stages"]), run["log"], run.get("digest", "")))
        connection.commit()


def run(db_path: PathLike, *, stages: dict[str, Stage] | None = None, only: tuple[str, ...] | list[str] | None = None,
        sleep: Callable[[float], None] = time.sleep, run_id: str | None = None) -> dict[str, Any]:
    """Execute the stages in order; persist after every stage so the UI can poll progress."""
    ensure_schema(db_path)
    stages = stages or REAL_STAGES
    selected = tuple(only) if only else STAGE_ORDER
    unknown = [s for s in selected if s not in STAGE_ORDER]
    if unknown:
        raise ValidationError(f"unknown stage(s): {', '.join(unknown)}")
    log_lines: list[str] = []
    record: dict[str, Any] = {"id": run_id or f"run_{uuid.uuid4().hex[:12]}", "status": "running",
                              "started_at": _now(), "finished_at": None, "stages": [], "log": "", "digest": ""}

    def log(message: str) -> None:
        log_lines.append(f"{_now()} {message}")
        record["log"] = "\n".join(log_lines)

    _save(db_path, record)
    failures = 0
    for index, name in enumerate(s for s in STAGE_ORDER if s in selected):
        if index:
            sleep(STAGE_SLEEP_SECONDS)
        entry: dict[str, Any] = {"name": name, "label": STAGE_LABELS[name], "status": "running", "started_at": _now()}
        record["stages"].append(entry)
        _save(db_path, record)
        started = time.monotonic()
        try:
            summary = stages[name](db_path, log)
            entry["status"] = "ok"
            if name == "digest" and isinstance(summary, dict) and summary.get("digest"):
                record["digest"] = summary.pop("digest")
            entry["summary"] = summary
        except Exception as error:  # one broken stage must not hide the others
            failures += 1
            entry["status"] = "failed"
            entry["error"] = f"{type(error).__name__}: {error}"[:300]
            log(f"{name} failed: {entry['error']}")
            log(traceback.format_exc()[-600:])
        entry["seconds"] = round(time.monotonic() - started, 1)
        _save(db_path, record)
    record["status"] = "completed_with_errors" if failures else "completed"
    record["finished_at"] = _now()
    _save(db_path, record)
    return record


def _row_to_run(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "status": row["status"], "started_at": row["started_at"],
            "finished_at": row["finished_at"], "stages": json.loads(row["stages_json"] or "[]"),
            "log": row["log"], "digest": row["digest"]}


def latest(db_path: PathLike) -> dict[str, Any] | None:
    ensure_schema(db_path)
    with closing(connect(db_path)) as connection:
        row = connection.execute("SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    return _row_to_run(row) if row else None


def get_run(db_path: PathLike, run_id: str) -> dict[str, Any]:
    ensure_schema(db_path)
    with closing(connect(db_path)) as connection:
        row = connection.execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        raise NotFoundError("run not found")
    return _row_to_run(row)


def list_runs(db_path: PathLike, limit: int = 20) -> dict[str, Any]:
    ensure_schema(db_path)
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            "SELECT id, status, started_at, finished_at, stages_json FROM pipeline_runs "
            "ORDER BY started_at DESC LIMIT ?", (max(1, min(int(limit), 100)),)).fetchall()
    runs = []
    for row in rows:
        stages = json.loads(row["stages_json"] or "[]")
        runs.append({"id": row["id"], "status": row["status"], "started_at": row["started_at"],
                     "finished_at": row["finished_at"],
                     "stages": [{"name": s["name"], "status": s["status"], "seconds": s.get("seconds")} for s in stages]})
    return {"runs": runs, "stage_order": list(STAGE_ORDER), "labels": STAGE_LABELS}


def start_background(db_path: PathLike, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /api/pipeline/run: start a run in a thread, return 202 with the id."""
    only = payload.get("only")
    if only is not None and not (isinstance(only, list) and all(isinstance(s, str) for s in only)):
        raise ValidationError("only must be a list of stage names")
    with _LOCK:
        alive = [rid for rid, t in _RUNNING.items() if t.is_alive()]
        if alive:
            raise ValidationError(f"a pipeline run is already in progress: {alive[0]}")
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        ensure_schema(db_path)
        _save(db_path, {"id": run_id, "status": "running", "started_at": _now(), "stages": [], "log": ""})
        thread = threading.Thread(target=run, kwargs={"db_path": db_path, "only": only, "run_id": run_id},
                                  daemon=True, name=run_id)
        _RUNNING[run_id] = thread
        thread.start()
    return {"id": run_id, "status": "running", "stages": list(only or STAGE_ORDER)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Run the draft-only career pipeline once (for cron).")
    parser.add_argument("--db", default=str(Path(__file__).with_name("career_pipeline_v2.sqlite3")))
    parser.add_argument("--only", nargs="*", choices=STAGE_ORDER)
    parser.add_argument("--print-digest", action="store_true")
    args = parser.parse_args(argv)
    result = run(args.db, only=args.only or None, sleep=time.sleep)
    print(json.dumps({"id": result["id"], "status": result["status"],
                      "stages": [{k: s.get(k) for k in ("name", "status", "seconds", "error")} for s in result["stages"]]},
                     indent=1))
    if args.print_digest:
        print(result["digest"])
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
