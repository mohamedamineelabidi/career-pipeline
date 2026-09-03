"""Backfill verification confidence for jobs whose description was already fetched.

The 276-job backlog predates the fix: those rows were fetched successfully but
stored with source_verification_status="unverified" (confidence 0), so they can
never pass the >=80 advance gate.

This awards the confidence they already earned, re-scores, then lets
auto_advance_statuses move whatever now qualifies.

Safe by construction: idempotent, never touches user_applied, reports before/after.
Run with --db pointing at a COPY first.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline_v2


def _counts(connection) -> dict:
    q = lambda sql: connection.execute(sql).fetchone()[0]
    return {
        "total": q("SELECT COUNT(*) FROM opportunities"),
        "discovered": q("SELECT COUNT(*) FROM opportunities WHERE status='discovered'"),
        "verified_active": q("SELECT COUNT(*) FROM opportunities WHERE status='verified_active'"),
        "confidence_ok": q("SELECT COUNT(*) FROM opportunities WHERE verification_confidence>=80"),
        "user_applied": q("SELECT COUNT(*) FROM opportunities WHERE status='user_applied'"),
    }


def backfill(db_path: str, *, apply_changes: bool) -> dict:
    with closing(pipeline_v2.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        before = _counts(connection)

        # A stored description IS the evidence: it was fetched from the listing URL.
        rows = connection.execute(
            """SELECT id, source_json FROM opportunities
                WHERE description IS NOT NULL AND TRIM(description) != ''
                  AND verification_confidence < 80
                  AND status != 'user_applied'"""
        ).fetchall()

        awarded = 0
        for row in rows:
            try:
                source = json.loads(row["source_json"] or "{}")
            except json.JSONDecodeError:
                source = {}
            if not isinstance(source, dict):
                source = {}
            current = str(source.get("source_verification_status") or "").strip().casefold()
            earned = pipeline_v2.VERIFICATION_CONFIDENCE.get("description_fetched", 0)
            if pipeline_v2.VERIFICATION_CONFIDENCE.get(current, 0) >= earned:
                continue
            source["source_verification_status"] = "description_fetched"
            if apply_changes:
                connection.execute(
                    """UPDATE opportunities
                          SET source_verification_status='description_fetched',
                              source_json=?
                        WHERE id=?""",
                    (json.dumps(source, ensure_ascii=False), row["id"]),
                )
            awarded += 1

        if apply_changes:
            pipeline_v2.reconcile_opportunity_scores(connection)
            connection.commit()
        after_scoring = _counts(connection)

    moved = pipeline_v2.auto_advance_statuses(db_path) if apply_changes else []

    with closing(pipeline_v2.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        after = _counts(connection)

    return {
        "dry_run": not apply_changes,
        "candidates_awarded": awarded,
        "auto_advanced": len(moved),
        "before": before,
        "after_scoring": after_scoring,
        "after": after,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="write changes; without it the run is a dry run")
    args = parser.parse_args(argv)

    report = backfill(args.db, apply_changes=args.apply)
    print(json.dumps(report, indent=2))

    if report["before"]["user_applied"] != report["after"]["user_applied"]:
        print("ABORT: user_applied count changed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
