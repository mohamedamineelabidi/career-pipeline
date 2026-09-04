"""Migration, validation and localhost server CLI for Career Pipeline v2."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pipeline_v2

TABLES = (
    "opportunities",
    "contacts",
    "contact_routes",
    "drafts",
    "cv_artifacts",
    "applications",
    "outreach_events",
    "automation_runs",
    "lifecycle_events",
    "metadata",
    "recruiter_reviews",
)

# Reach system tables (see reach/DESIGN.md). Applied with IF NOT EXISTS on every
# migrate/serve so existing databases gain them on next start.
REACH_SCHEMA = """
CREATE TABLE IF NOT EXISTS target_companies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    aliases_json TEXT DEFAULT '[]',
    sector TEXT,
    country TEXT,
    intent TEXT CHECK(intent IN ('internship', 'job', 'referral', 'any')) DEFAULT 'any',
    priority INTEGER DEFAULT 50,
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS people_candidates (
    id TEXT PRIMARY KEY,
    target_company_id TEXT REFERENCES target_companies(id),
    name TEXT NOT NULL,
    headline TEXT,
    company_seen TEXT,
    role_seen TEXT,
    profile_url TEXT,
    email TEXT,
    evidence_url TEXT,
    evidence_quote TEXT,
    discovered_via TEXT,
    score INTEGER DEFAULT 0,
    verification_status TEXT DEFAULT 'unverified',
    current_role_confirmed_at TEXT,
    promoted_contact_id TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS people_candidates_profile_url
    ON people_candidates(profile_url)
    WHERE profile_url IS NOT NULL AND profile_url != '';
"""

# Columns added after the first Reach release. Applied with ALTER TABLE only
# when missing, so old and new databases end up with the same shape.
PEOPLE_CANDIDATES_EXTRA_COLUMNS = (
    ("email_status",
     "TEXT DEFAULT 'none' CHECK(email_status IN "
     "('none','found_official','found_public','inferred','rejected'))"),
    ("email_evidence_url", "TEXT"),
    ("email_checked_at", "TEXT"),
)


def existing_columns(connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def ensure_reach_schema(db_path: Path) -> None:
    with closing(pipeline_v2.connect(db_path)) as connection:
        connection.executescript(REACH_SCHEMA)
        present = existing_columns(connection, "people_candidates")
        for column, ddl in PEOPLE_CANDIDATES_EXTRA_COLUMNS:
            if column not in present:
                connection.execute(f"ALTER TABLE people_candidates ADD COLUMN {column} {ddl}")
        connection.commit()


def migrate_with_backup(source_path: Path, db_path: Path) -> dict[str, int]:
    source = Path(source_path)
    target = Path(db_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    source_backup = source.with_name(f"{source.stem}.{timestamp}.bak{source.suffix}")
    shutil.copy2(source, source_backup)
    if target.exists():
        database_backup = target.with_name(f"{target.stem}.{timestamp}.bak{target.suffix}")
        shutil.copy2(target, database_backup)
    return pipeline_v2.migrate(source, target)


def validate_integrity(db_path: Path) -> dict:
    database = Path(db_path)
    errors = []
    counts = {}
    if not database.is_file():
        return {"ok": False, "counts": counts, "errors": ["database does not exist"]}
    with closing(pipeline_v2.connect(database)) as connection:
        existing = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = sorted(set(TABLES) - existing)
        if missing:
            errors.append("missing tables: " + ", ".join(missing))
        count_queries = {
            "opportunities": "SELECT COUNT(*) FROM opportunities",
            "contacts": "SELECT COUNT(*) FROM contacts",
            "contact_routes": "SELECT COUNT(*) FROM contact_routes",
            "drafts": "SELECT COUNT(*) FROM drafts",
            "cv_artifacts": "SELECT COUNT(*) FROM cv_artifacts",
            "applications": "SELECT COUNT(*) FROM applications",
            "outreach_events": "SELECT COUNT(*) FROM outreach_events",
            "automation_runs": "SELECT COUNT(*) FROM automation_runs",
            "lifecycle_events": "SELECT COUNT(*) FROM lifecycle_events",
            "metadata": "SELECT COUNT(*) FROM metadata",
            "recruiter_reviews": "SELECT COUNT(*) FROM recruiter_reviews",
        }
        for table in TABLES:
            if table in existing:
                counts[table] = connection.execute(count_queries[table]).fetchone()[0]
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            errors.append(f"quick_check: {quick_check}")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            errors.append(f"foreign key violations: {len(foreign_key_errors)}")
        if "opportunities" in existing:
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if schema_version != pipeline_v2.MIGRATION_VERSION:
                errors.append(f"schema version: {schema_version}")
            invalid_scores = connection.execute(
                """SELECT COUNT(*) FROM opportunities
                   WHERE match_score NOT BETWEEN 0 AND 100
                      OR fit_score NOT BETWEEN 0 AND 100
                      OR verification_confidence NOT BETWEEN 0 AND 100
                      OR priority_score NOT BETWEEN 0 AND 100
                      OR score_schema_version != 2"""
            ).fetchone()[0]
            invalid_statuses = connection.execute(
                "SELECT DISTINCT status FROM opportunities"
            ).fetchall()
            unknown = sorted(
                row[0] for row in invalid_statuses
                if row[0] not in pipeline_v2.OPPORTUNITY_STATUSES
            )
            if invalid_scores:
                errors.append(f"invalid opportunity scores: {invalid_scores}")
            if unknown:
                errors.append("invalid opportunity statuses: " + ", ".join(unknown))
            for row in connection.execute("SELECT id, score_breakdown_json FROM opportunities"):
                try:
                    breakdown = json.loads(row[1])
                except json.JSONDecodeError:
                    breakdown = None
                if not isinstance(breakdown, dict) or "fit_score" not in breakdown:
                    errors.append(f"invalid score breakdown: {row[0]}")
        if "drafts" in existing:
            unknown = sorted(
                row[0]
                for row in connection.execute("SELECT DISTINCT status FROM drafts")
                if row[0] not in pipeline_v2.DRAFT_STATUSES
            )
            if unknown:
                errors.append("invalid draft statuses: " + ", ".join(unknown))
    return {"ok": not errors, "counts": counts, "errors": errors}


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate_parser = subparsers.add_parser("migrate", help="backup and migrate jobs_digest.json")
    migrate_parser.add_argument("--source", type=Path, default=root / "jobs_digest.json")
    migrate_parser.add_argument("--db", type=Path, default=root / "career_pipeline_v2.sqlite3")

    validate_parser = subparsers.add_parser("validate", help="validate database integrity")
    validate_parser.add_argument("--db", type=Path, default=root / "career_pipeline_v2.sqlite3")

    serve_parser = subparsers.add_parser("serve", help="serve API on 127.0.0.1 only")
    serve_parser.add_argument("--db", type=Path, default=root / "career_pipeline_v2.sqlite3")
    serve_parser.add_argument("--port", type=int, default=8787)
    serve_parser.add_argument("--static-root", type=Path, default=root)

    export_parser = subparsers.add_parser("export", help="generate a read-only JSON compatibility snapshot")
    export_parser.add_argument("--db", type=Path, default=root / "career_pipeline_v2.sqlite3")
    export_parser.add_argument("--output", type=Path, default=root / "jobs_digest.json")

    run_parser = subparsers.add_parser("record-run", help="record a scheduled job outcome")
    run_parser.add_argument("--db", type=Path, default=root / "career_pipeline_v2.sqlite3")
    run_parser.add_argument("--job-name", required=True)
    run_parser.add_argument("--status", choices=sorted(pipeline_v2.AUTOMATION_RUN_STATUSES), required=True)
    run_parser.add_argument("--record-count", type=int, default=0)
    run_parser.add_argument("--details", default="")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        counts = migrate_with_backup(args.source, args.db)
        ensure_reach_schema(args.db)
        report = validate_integrity(args.db)
        print(json.dumps({"counts": counts, "integrity": report}, indent=2))
        return 0 if report["ok"] else 1
    if args.command == "validate":
        report = validate_integrity(args.db)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    if args.command == "serve":
        if Path(args.db).exists():
            ensure_reach_schema(args.db)
        report = validate_integrity(args.db)
        if not report["ok"]:
            print(json.dumps(report, indent=2))
            return 1
        server = pipeline_v2.make_server(args.db, args.static_root, args.port)
        print(f"Serving http://127.0.0.1:{server.server_port}/pipeline_v2.html")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    if args.command == "export":
        report = validate_integrity(args.db)
        if not report["ok"]:
            print(json.dumps(report, indent=2))
            return 1
        counts = pipeline_v2.export_json_snapshot(args.db, args.output)
        print(json.dumps(counts, indent=2))
        return 0
    if args.command == "record-run":
        try:
            result = pipeline_v2.record_automation_run(
                args.db, args.job_name, args.status, args.record_count, args.details
            )
        except pipeline_v2.ValidationError as error:
            print(json.dumps({"error": str(error)}, indent=2))
            return 1
        print(json.dumps(result, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
