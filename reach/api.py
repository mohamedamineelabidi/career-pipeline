"""HTTP-facing logic for the /api/reach/* routes served by pipeline_v2.

Every function takes the database path plus a JSON payload or query mapping
and returns a JSON-able value. Sibling reach modules (targets, scoring,
drafts, morocco_radar) are imported lazily so the router keeps working even
while those modules are still being written. Nothing here sends anything.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import pipeline_v2
from pipeline_v2 import ConflictError, NotFoundError, ValidationError

TARGET_INTENTS = frozenset({"internship", "job", "referral", "any"})
DRAFT_LANGS = frozenset({"fr", "en"})
RADAR_TAG = "morocco_ai_cloud"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rows(cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


# --- Dispatch (called by the pipeline_v2 router) ---------------------------

def handle_get(db_path, endpoint: str, query: dict[str, str]) -> Any:
    """`endpoint` is the path after '/api/reach/'; returns a JSON-able body (status 200)."""
    if endpoint == "targets":
        return list_targets(db_path)
    if endpoint == "people":
        return list_people(db_path, query)
    if endpoint == "jobs":
        return list_jobs(db_path, query)
    if endpoint == "runs":
        return list_runs(db_path)
    raise NotFoundError("endpoint not found")


def handle_post(db_path, endpoint: str, payload: dict[str, Any], root=None) -> tuple[int, Any]:
    """`endpoint` is the path after '/api/reach/'; returns (status, body)."""
    if endpoint == "targets":
        return create_target(db_path, payload)
    if endpoint == "run":
        return start_run(db_path, payload)
    parts = endpoint.split("/")
    if len(parts) == 3 and parts[0] == "people" and parts[1]:
        candidate_id, action = parts[1], parts[2]
        if action == "confirm-role":
            return 200, confirm_role(db_path, candidate_id, payload)
        if action == "promote":
            return 200, promote_candidate(db_path, candidate_id)
        if action == "draft":
            # Legacy shape {lang, fact, channel: linkedin|email} keeps the old fact-based draft.
            if "fact" in payload and payload.get("channel") not in COMPOSE_CHANNELS:
                return 201, draft_candidate(db_path, candidate_id, payload)
            return smart_draft_candidate(db_path, candidate_id, payload)
    raise NotFoundError("endpoint not found")


# --- D1: targets -----------------------------------------------------------

def list_targets(db_path) -> list[dict[str, Any]]:
    connection = pipeline_v2.connect(db_path)
    try:
        return _rows(connection.execute(
            "SELECT * FROM target_companies ORDER BY priority DESC, name"
        ))
    finally:
        connection.close()


def create_target(db_path, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Return (status, row): 201 when created, 200 when the name already exists."""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValidationError("name is required")
    intent = str(payload.get("intent") or "any").strip()
    if intent not in TARGET_INTENTS:
        raise ValidationError("intent must be one of internship|job|referral|any")
    try:
        priority = int(payload.get("priority") or 0)
    except (TypeError, ValueError) as error:
        raise ValidationError("priority must be an integer") from error
    connection = pipeline_v2.connect(db_path)
    try:
        existing = connection.execute(
            "SELECT * FROM target_companies WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if existing is not None:
            return 200, dict(existing)
        now = _now()
        row_id = f"tgt_{uuid.uuid4().hex[:12]}"
        connection.execute(
            "INSERT INTO target_companies (id, name, aliases_json, sector, country, intent, priority, notes,"
            " created_at, updated_at) VALUES (?, ?, '[]', ?, ?, ?, ?, ?, ?, ?)",
            (row_id, name, str(payload.get("sector") or ""), str(payload.get("country") or ""),
             intent, priority, str(payload.get("notes") or ""), now, now),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM target_companies WHERE id = ?", (row_id,)).fetchone()
        return 201, dict(row)
    finally:
        connection.close()


# --- D2: people gates (confirm -> promote -> draft) --------------------------

def _default_promote(db_path, candidate_id: str) -> str:
    from reach.scoring import promote

    connection = pipeline_v2.connect(db_path)
    try:
        contact_id = promote(connection, candidate_id)
        connection.commit()
        return contact_id
    finally:
        connection.close()


def _default_draft_for(candidate: dict, lang: str, fact: str, channel: str = "linkedin",
                       opportunity: dict | None = None) -> str:
    from reach.drafts import draft_for

    contact = {
        "id": candidate.get("promoted_contact_id"),
        "name": candidate.get("name"),
        "company": candidate.get("company_seen") or candidate.get("target_name") or "",
        "role": candidate.get("role_seen") or candidate.get("headline") or "",
    }
    return draft_for(contact, opportunity, lang, fact, channel)


def _default_save_draft(db_path, candidate: dict, body: str, channel: str,
                        opportunity_id: str | None = None, lang: str = "fr") -> str:
    from reach.drafts import save_draft

    contact_id = candidate["promoted_contact_id"]
    connection = pipeline_v2.connect(db_path)
    try:
        contact = connection.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if contact is None:
            raise NotFoundError("promoted contact not found")
        route = connection.execute(
            "SELECT id FROM contact_routes WHERE contact_id = ? AND route_type = ?"
            " ORDER BY is_verified DESC, id LIMIT 1",
            (contact_id, "linkedin" if channel == "linkedin" else "email"),
        ).fetchone()
        draft_id = save_draft(connection, contact_id, opportunity_id,
                              route["id"] if route else None, channel, lang, body)
        connection.commit()
        return draft_id
    finally:
        connection.close()


def _default_compose(person: dict, channel: str, lang: str, kind: str = "internship",
                     company: str | None = None) -> dict:
    from reach.drafts import compose

    return compose(person, channel, lang, kind=kind, company=company)


# Indirections so tests can patch without the sibling modules being present.
PROMOTE = _default_promote
DRAFT_FOR = _default_draft_for
SAVE_DRAFT = _default_save_draft
COMPOSE = _default_compose
COMPOSE_CHANNELS = ("linkedin_note", "linkedin_message", "email")


def _get_candidate(connection, candidate_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT p.*, t.name AS target_name FROM people_candidates p"
        " LEFT JOIN target_companies t ON t.id = p.target_company_id WHERE p.id = ?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("candidate not found")
    return dict(row)


def list_people(db_path, query: dict[str, str]) -> list[dict[str, Any]]:
    clauses, params = [], []
    if query.get("target"):
        clauses.append("p.target_company_id = ?")
        params.append(query["target"])
    if query.get("min_score"):
        try:
            params.append(int(query["min_score"]))
        except ValueError as error:
            raise ValidationError("min_score must be an integer") from error
        clauses.append("p.score >= ?")
    if query.get("status"):
        clauses.append("p.verification_status = ?")
        params.append(query["status"])
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    connection = pipeline_v2.connect(db_path)
    try:
        return _rows(connection.execute(
            "SELECT p.*, t.name AS target_name FROM people_candidates p"
            f" LEFT JOIN target_companies t ON t.id = p.target_company_id{where}"
            " ORDER BY p.score DESC, p.name",
            params,
        ))
    finally:
        connection.close()


def confirm_role(db_path, candidate_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("confirmed") is not True:
        raise ValidationError("confirmed must be true")
    connection = pipeline_v2.connect(db_path)
    try:
        _get_candidate(connection, candidate_id)
        now = _now()
        connection.execute(
            "UPDATE people_candidates SET current_role_confirmed_at = ?, updated_at = ? WHERE id = ?",
            (now, now, candidate_id),
        )
        connection.commit()
        return _get_candidate(connection, candidate_id)
    finally:
        connection.close()


def promote_candidate(db_path, candidate_id: str) -> dict[str, Any]:
    connection = pipeline_v2.connect(db_path)
    try:
        _get_candidate(connection, candidate_id)
    finally:
        connection.close()
    try:
        contact_id = PROMOTE(db_path, candidate_id)
    except ValueError as error:
        raise ConflictError(str(error)) from error
    except LookupError as error:
        raise NotFoundError(str(error)) from error
    return {"contact_id": contact_id}


def draft_candidate(db_path, candidate_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    lang = str(payload.get("lang") or "").strip().lower()
    if lang not in DRAFT_LANGS:
        raise ValidationError("lang must be fr|en")
    fact = str(payload.get("fact") or "").strip()
    if not fact:
        raise ValidationError("fact is required")
    channel = str(payload.get("channel") or "linkedin").strip().lower()
    if channel not in {"linkedin", "email"}:
        raise ValidationError("channel must be linkedin|email")
    opportunity_id = payload.get("opportunity_id") or None
    connection = pipeline_v2.connect(db_path)
    try:
        candidate = _get_candidate(connection, candidate_id)
        if not candidate.get("promoted_contact_id"):
            raise ConflictError("candidate not promoted; confirm role and promote first")
        opportunity = None
        if opportunity_id:
            row = connection.execute("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
            if row is None:
                raise NotFoundError("opportunity not found")
            opportunity = dict(row)
    finally:
        connection.close()
    body = DRAFT_FOR(candidate, lang, fact, channel, opportunity)
    draft_id = SAVE_DRAFT(db_path, candidate, body, channel, opportunity_id)
    return {"draft_id": draft_id, "body": body}


def smart_draft_candidate(db_path, candidate_id: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST people/<id>/draft {lang, channel, kind?}: compose one draft from the
    fact sheet and the candidate's evidence, lint it, save it as draft_not_opened.

    Works before promotion (contact_id stays NULL; source_json keeps the
    candidate id). 400 on bad lang/channel, 422 with the lint list if the
    draft fails lint. Never sends anything.
    """
    lang = str(payload.get("lang") or "").strip().lower()
    if lang not in DRAFT_LANGS:
        raise ValidationError("lang must be fr|en")
    channel = str(payload.get("channel") or "").strip().lower()
    if channel not in COMPOSE_CHANNELS:
        raise ValidationError("channel must be linkedin_note|linkedin_message|email")
    kind = str(payload.get("kind") or "internship").strip().lower()
    if kind not in ("internship", "job"):
        raise ValidationError("kind must be internship|job")
    connection = pipeline_v2.connect(db_path)
    try:
        candidate = _get_candidate(connection, candidate_id)
        draft = COMPOSE(candidate, channel, lang, kind=kind, company=payload.get("company") or None)
        if draft.get("lint"):
            return 422, {"error": "draft failed lint", "lint": list(draft["lint"])}
        from reach.drafts import save_draft

        contact_id = candidate.get("promoted_contact_id") or None
        route_id = None
        if contact_id:
            route = connection.execute(
                "SELECT id FROM contact_routes WHERE contact_id = ? AND route_type = ?"
                " ORDER BY is_verified DESC, id LIMIT 1",
                (contact_id, "email" if channel == "email" else "linkedin"),
            ).fetchone()
            route_id = route["id"] if route else None
        db_channel = "email" if channel == "email" else "linkedin"
        draft_id = save_draft(connection, contact_id, None, route_id, db_channel, lang, draft["body"],
                              subject=draft.get("subject"),
                              extra={"candidate_id": candidate_id, "channel": channel, "kind": kind,
                                     "persona": draft.get("persona"), "proof_id": draft.get("proof_id")})
        return 200, {"draft_id": draft_id, "subject": draft.get("subject"), "body": draft["body"], "lint": []}
    finally:
        connection.close()


# --- D3: radar jobs ------------------------------------------------------------

JOB_COLUMNS = ("id", "title", "company", "location", "url", "source", "publication_date",
               "role_kind", "role_family", "status")


def list_jobs(db_path, query: dict[str, str]) -> list[dict[str, Any]]:
    clauses = ["json_extract(source_json, '$.radar') = ?"]
    params: list[Any] = [RADAR_TAG]
    if query.get("kind"):
        # The radar's internship|job lives in source_json.role_kind (the
        # opportunities.role_kind column is the classifier's, not ours).
        clauses.append("json_extract(source_json, '$.role_kind') = ?")
        params.append(query["kind"])
    if query.get("family"):
        clauses.append("role_family = ?")
        params.append(query["family"])
    if query.get("q"):
        like = f"%{query['q'].strip()}%"
        clauses.append("(title LIKE ? OR company LIKE ? OR location LIKE ?)")
        params.extend([like, like, like])
    connection = pipeline_v2.connect(db_path)
    try:
        return _rows(connection.execute(
            f"SELECT {', '.join(JOB_COLUMNS)} FROM opportunities WHERE {' AND '.join(clauses)}"
            " ORDER BY publication_date IS NULL, publication_date DESC LIMIT 200",
            params,
        ))
    finally:
        connection.close()


# --- D4: background stage runs -------------------------------------------

def _run_radar_stage(db_path, payload: dict[str, Any]) -> dict[str, Any]:
    from reach import morocco_radar

    connection = pipeline_v2.connect(db_path)
    try:
        return morocco_radar.run_radar(
            connection,
            morocco_radar.load_queries(),
            morocco_radar.default_fetcher,
            limit=payload.get("limit"),
        )
    finally:
        connection.close()


# Indirections so tests can swap the channels without touching the network.
def _search_available() -> bool:
    from reach.search_channel import available
    return available()


def _people_search_fn(query: str) -> list[dict[str, Any]]:
    from reach.search_channel import people_search_fn
    return people_search_fn(query)


def _read_url(url: str) -> tuple[str, str]:
    """Agent Reach reader channel (direct -> Jina -> blocked). Returns (text, backend)."""
    from agent_reach_channel import read_url
    result = read_url(url)
    return result.get("text", "") or "", result.get("backend", "blocked")


def _discover_public(conn, target_id, company, search_fn, read_fn, **kwargs) -> list[str]:
    from reach.people_discovery import discover_public
    return discover_public(conn, target_id, company, search_fn, read_fn, **kwargs)


def _run_people_public_stage(db_path, payload: dict[str, Any]) -> dict[str, Any]:
    """Find public-web people for ONE target through the Agent Reach channels:
    Exa search (mcporter) for discovery, direct/Jina reader for evidence pages.
    LinkedIn profile URLs are stored, never fetched. This is never a LinkedIn stage."""
    target_id = str(payload.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("people_public needs a target_id")
    if not _search_available():
        raise RuntimeError("search channel unavailable: mcporter not on PATH "
                           "(npm install -g mcporter; mcporter config add exa https://mcp.exa.ai/mcp --scope home)")
    connection = pipeline_v2.connect(db_path)
    try:
        row = connection.execute("SELECT name FROM target_companies WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown target {target_id}")
        company = row[0]

        def read_fn(url: str) -> str:
            text, _backend = _read_url(url)
            return text

        inserted = _discover_public(connection, target_id, company, _people_search_fn, read_fn)
        connection.commit()
    finally:
        connection.close()
    return {"target_id": target_id, "company": company, "inserted": len(inserted), "candidate_ids": inserted}


def _find_email(conn, candidate, search_fn, read_fn, verify_fn) -> dict[str, Any]:
    from reach import email_finder
    return email_finder.find_email(conn, candidate, search_fn, read_fn, verify_fn,
                                   company_search_fn=_company_search_fn)


def _company_search_fn(query: str) -> list[dict[str, Any]]:
    from reach.search_channel import exa_search
    return exa_search(query, category="company", num_results=5)


def _email_search_fn(query: str, category: str | None = None) -> list[dict[str, Any]]:
    from reach.search_channel import exa_search
    return exa_search(query, category=category, num_results=10)


def _verify_email(addr: str):
    from reach import email_finder
    return email_finder.verify_email(addr)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


EMAILS_PACING_S = 1.5
EMAILS_MAX_CANDIDATES = 60
EMAIL_TIERS = ("found_official", "found_public", "inferred", "rejected", "none")


def _run_emails_stage(db_path, payload: dict[str, Any]) -> dict[str, Any]:
    """Find emails for ONE target's candidates: evidence on a page first, then a
    pattern observed at the company, and record which tier each address came from.
    Nothing is sent; the SMTP probe only asks and quits."""
    target_id = str(payload.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("emails needs a target_id")
    connection = pipeline_v2.connect(db_path)
    try:
        row = connection.execute("SELECT name FROM target_companies WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown target {target_id}")
        company = row[0]
        candidates = _rows(connection.execute(
            "SELECT * FROM people_candidates WHERE target_company_id = ?"
            " ORDER BY score DESC, created_at ASC LIMIT ?", (target_id, EMAILS_MAX_CANDIDATES)))

        def read_fn(url: str) -> dict[str, str]:
            text, backend = _read_url(url)
            return {"text": text, "backend": backend}

        counts = {tier: 0 for tier in EMAIL_TIERS}
        for index, candidate in enumerate(candidates):
            if index:
                _sleep(EMAILS_PACING_S)
            result = _find_email(connection, candidate, _email_search_fn, read_fn, _verify_email)
            connection.commit()
            status = str(result.get("email_status") or "none")
            counts[status] = counts.get(status, 0) + 1
    finally:
        connection.close()
    return {"target_id": target_id, "company": company, "checked": len(candidates), **counts}


STAGE_RUNNERS: dict[str, Any] = {
    "radar": _run_radar_stage,
    "people_public": _run_people_public_stage,
    "emails": _run_emails_stage,
}


def _finish_run(db_path, run_id: str, status: str, details: dict[str, Any]) -> None:
    connection = pipeline_v2.connect(db_path)
    try:
        connection.execute(
            "UPDATE automation_runs SET status = ?, finished_at = ?, details = ? WHERE id = ?",
            (status, _now(), json.dumps(details, ensure_ascii=False, default=str), run_id),
        )
        connection.commit()
    finally:
        connection.close()


def _execute_run(db_path, run_id: str, stage: str, payload: dict[str, Any]) -> None:
    try:
        result = STAGE_RUNNERS[stage](db_path, payload)
        _finish_run(db_path, run_id, "ok", result if isinstance(result, dict) else {"result": result})
    except Exception as error:  # noqa: BLE001 - the row is the error report
        _finish_run(db_path, run_id, "failed", {"error": str(error)})


def start_run(db_path, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    stage = str(payload.get("stage") or "").strip()
    if stage not in STAGE_RUNNERS:
        raise ValidationError("stage must be one of radar|people_public|emails")
    if stage in ("people_public", "emails") and not str(payload.get("target_id") or "").strip():
        raise ValidationError(f"{stage} needs a target_id")
    run_type = f"reach_{stage}"
    connection = pipeline_v2.connect(db_path)
    try:
        running = connection.execute(
            "SELECT id FROM automation_runs WHERE run_type = ? AND status = 'running'", (run_type,)
        ).fetchone()
        if running is not None:
            return 429, {"error": f"{stage} is already running", "run_id": running["id"]}
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        connection.execute(
            "INSERT INTO automation_runs (id, run_type, status, started_at, finished_at, details)"
            " VALUES (?, ?, 'running', ?, NULL, ?)",
            (run_id, run_type, _now(), json.dumps({"payload": payload}, default=str)),
        )
        connection.commit()
    finally:
        connection.close()
    threading.Thread(
        target=_execute_run, args=(db_path, run_id, stage, payload), daemon=True, name=run_id
    ).start()
    return 202, {"run_id": run_id, "stage": stage}


def list_runs(db_path) -> list[dict[str, Any]]:
    connection = pipeline_v2.connect(db_path)
    try:
        return _rows(connection.execute(
            "SELECT id, run_type, status, started_at, finished_at, details FROM automation_runs"
            " WHERE run_type LIKE 'reach_%' ORDER BY started_at DESC LIMIT 20"
        ))
    finally:
        connection.close()
