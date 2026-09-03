"""Normalize source names and flag duplicate vacancies on an existing database.

Duplicates are FLAGGED, never deleted: the user decides what to remove. The
newest row in each group is kept as primary; older siblings are marked in
source_json with duplicate_of, so nothing is destroyed and it is reversible.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline_v2


def clean(db_path: str, *, apply_changes: bool) -> dict:
    report = {"dry_run": not apply_changes}
    with closing(pipeline_v2.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT id, title, company, location, source, status, source_json, updated_at
                 FROM opportunities"""
        ).fetchall()

        report["sources_before"] = len({str(r["source"] or "") for r in rows})

        renamed = 0
        for row in rows:
            canonical = pipeline_v2.normalize_source(row["source"])
            if canonical != (row["source"] or ""):
                renamed += 1
                if apply_changes:
                    connection.execute(
                        "UPDATE opportunities SET source=? WHERE id=?", (canonical, row["id"])
                    )
        report["rows_renamed"] = renamed
        report["sources_after"] = len(
            {pipeline_v2.normalize_source(r["source"]) for r in rows}
        )

        groups = defaultdict(list)
        for row in rows:
            groups[pipeline_v2.duplicate_key(
                row["title"], row["company"], row["location"])].append(row)

        dupe_groups = {k: v for k, v in groups.items() if len(v) > 1}
        flagged = 0
        for members in dupe_groups.values():
            ordered = sorted(members, key=lambda r: str(r["updated_at"] or ""), reverse=True)
            primary, siblings = ordered[0], ordered[1:]
            for sibling in siblings:
                # Never touch a job the user already acted on.
                if sibling["status"] == "user_applied":
                    continue
                try:
                    source = json.loads(sibling["source_json"] or "{}")
                except json.JSONDecodeError:
                    source = {}
                if not isinstance(source, dict):
                    source = {}
                if source.get("duplicate_of") == primary["id"]:
                    continue
                source["duplicate_of"] = primary["id"]
                flagged += 1
                if apply_changes:
                    connection.execute(
                        "UPDATE opportunities SET source_json=? WHERE id=?",
                        (json.dumps(source, ensure_ascii=False), sibling["id"]),
                    )
        report["duplicate_groups"] = len(dupe_groups)
        report["rows_flagged_duplicate"] = flagged
        report["examples"] = [
            {"key": k, "titles": [m["title"] for m in v][:3]}
            for k, v in list(dupe_groups.items())[:3]
        ]
        if apply_changes:
            connection.commit()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(clean(args.db, apply_changes=args.apply), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
