"""Application tracker: Kanban view, safe status moves, and per-opportunity timeline.

Read model over the existing opportunities table plus lifecycle_events,
outreach_events, applications, cv_artifacts and automation_runs. Moves reuse
pipeline_v2.update_opportunity so the allowed-transition rules, version token
(updated_at -> 409) and confirmation rules apply unchanged.
"""

from __future__ import annotations

import json
from contextlib import closing
from typing import Any

import pipeline_v2
from pipeline_v2 import (
    OPPORTUNITY_STATUSES,
    OPPORTUNITY_TRANSITIONS,
    NotFoundError,
    PathLike,
    ValidationError,
    connect,
)

COLUMN_ORDER = ("discovered", "verified_active", "eligible", "shortlisted", "user_applied", "closed")

NEXT_ACTION = {
    "discovered": "Verify the source and freshness, then mark verified_active.",
    "verified_active": "Check eligibility gates and move to eligible.",
    "eligible": "Generate a tailored CV and shortlist if the fit is strong.",
    "shortlisted": "Review the CV, then apply manually and confirm user_applied.",
    "user_applied": "Wait for a reply; record outcomes as they arrive.",
    "closed": "No action.",
}


def _next_action(row: dict[str, Any]) -> str:
    status = row["status"]
    if status == "eligible" and not row["has_cv"]:
        return "Generate a tailored CV (no CV yet)."
    if status == "shortlisted" and not row["has_cv"]:
        return "Generate a tailored CV before applying."
    return NEXT_ACTION.get(status, "")


def board(db_path: PathLike) -> dict[str, Any]:
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """
            SELECT o.id, o.title, o.company, o.status, o.priority_score AS priority,
                   o.updated_at, o.deadline, o.location, o.url,
                   (SELECT ls.fit FROM llm_scores ls WHERE ls.opportunity_id=o.id) AS llm_fit,
                   CAST(ROUND(ss.score) AS INTEGER) AS semantic_score,
                   EXISTS(SELECT 1 FROM cv_artifacts c WHERE c.opportunity_id=o.id) AS has_cv,
                   (SELECT MAX(occurred_at) FROM lifecycle_events l
                     WHERE l.entity_type='opportunity' AND l.entity_id=o.id) AS last_event_at
            FROM opportunities o
            LEFT JOIN semantic_scores ss ON ss.opportunity_id = o.id
            ORDER BY o.priority_score DESC, o.updated_at DESC, o.id
            """
        ).fetchall()
    columns: dict[str, list[dict[str, Any]]] = {status: [] for status in COLUMN_ORDER}
    for raw in rows:
        row = dict(raw)
        row["has_cv"] = bool(row["has_cv"])
        card = {
            "id": row["id"],
            "title": row["title"],
            "company": row["company"],
            "location": row["location"],
            "url": row["url"],
            "llm_fit": row["llm_fit"],
            "semantic_score": row["semantic_score"],
            "priority": row["priority"],
            "has_cv": row["has_cv"],
            "last_update": row["last_event_at"] or row["updated_at"],
            "version": row["updated_at"],
            "deadline": row["deadline"],
            "next_action": _next_action(row),
            "allowed_moves": sorted(OPPORTUNITY_TRANSITIONS.get(row["status"], set())),
        }
        columns.setdefault(row["status"], []).append(card)
    return {
        "column_order": list(COLUMN_ORDER),
        "columns": columns,
        "counts": {status: len(cards) for status, cards in columns.items()},
        "transitions": {k: sorted(v) for k, v in OPPORTUNITY_TRANSITIONS.items()},
    }


def move(db_path: PathLike, payload: dict[str, Any]) -> dict[str, Any]:
    unknown = set(payload) - {"opportunity_id", "to_status", "version", "confirmed_by_user"}
    if unknown:
        raise ValidationError("only opportunity_id, to_status, version and confirmed_by_user are accepted")
    opportunity_id = payload.get("opportunity_id")
    if not isinstance(opportunity_id, str) or not opportunity_id:
        raise ValidationError("opportunity_id is required")
    to_status = payload.get("to_status")
    if to_status not in OPPORTUNITY_STATUSES:
        raise ValidationError(f"invalid to_status: {to_status}")
    changes: dict[str, Any] = {"status": to_status, "version": payload.get("version")}
    if "confirmed_by_user" in payload:
        changes["confirmed_by_user"] = payload["confirmed_by_user"]
    record = pipeline_v2.update_opportunity(db_path, opportunity_id, changes)
    return {
        "opportunity": record,
        "version": record["updated_at"],
        "moved_to": record["status"],
    }


def timeline(db_path: PathLike, opportunity_id: str) -> dict[str, Any]:
    with closing(connect(db_path)) as connection:
        opp = connection.execute(
            "SELECT id, title, company, status, created_at, updated_at FROM opportunities WHERE id=?",
            (opportunity_id,),
        ).fetchone()
        if opp is None:
            raise NotFoundError("opportunity not found")
        events: list[dict[str, Any]] = [{
            "at": opp["created_at"], "kind": "created",
            "summary": f"Opportunity recorded ({opp['company']})", "details": {},
        }]
        for row in connection.execute(
            "SELECT * FROM lifecycle_events WHERE entity_type='opportunity' AND entity_id=? ORDER BY occurred_at",
            (opportunity_id,),
        ):
            events.append({
                "at": row["occurred_at"], "kind": "status_change",
                "summary": f"{row['from_status']} -> {row['to_status']}",
                "details": {"confirmed_by_user": bool(row["confirmed_by_user"])},
            })
        for row in connection.execute(
            "SELECT * FROM outreach_events WHERE opportunity_id=? ORDER BY occurred_at", (opportunity_id,)
        ):
            events.append({
                "at": row["occurred_at"], "kind": "outreach",
                "summary": row["event_type"], "details": {"notes": row["notes"], "created_by": row["created_by"]},
            })
        for row in connection.execute(
            "SELECT * FROM applications WHERE opportunity_id=? ORDER BY created_at", (opportunity_id,)
        ):
            events.append({
                "at": row["applied_at"] or row["created_at"], "kind": "application",
                "summary": f"application {row['status']}", "details": {"notes": row["notes"]},
            })
        for row in connection.execute(
            "SELECT * FROM automation_runs WHERE details LIKE ? ORDER BY started_at", (f"%{opportunity_id}%",)
        ):
            events.append({
                "at": row["started_at"], "kind": "automation_run",
                "summary": f"{row['run_type']} ({row['status']})", "details": {"finished_at": row["finished_at"]},
            })
        for table, kind in (("interview_preps", "interview_prep"), ("cover_letter_drafts", "cover_letter_draft"),
                            ("recruiter_reviews", "recruiter_review")):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            for row in connection.execute(
                f"SELECT created_at FROM {table} WHERE opportunity_id=? ORDER BY created_at", (opportunity_id,)
            ):
                events.append({"at": row["created_at"], "kind": kind, "summary": f"{kind} generated locally", "details": {}})
    events.sort(key=lambda e: str(e["at"] or ""))
    return {
        "opportunity_id": opp["id"],
        "title": opp["title"],
        "company": opp["company"],
        "status": opp["status"],
        "version": opp["updated_at"],
        "events": events,
    }
