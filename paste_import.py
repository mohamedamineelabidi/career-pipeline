"""Paste import: let the user attach a job description they copied manually.

Used when automated JD fetching is blocked (login wall, anti-bot) — the user opens the page
in their own browser and pastes the text. Nothing here ever submits anything anywhere.

Endpoints (dispatched from pipeline_v2.make_handler, same-origin + local Host enforced):
  POST /api/opportunities/paste
       {url, title?, company?, location?, text, version: "new"}  -> 201 opportunity row
  POST /api/opportunities/<OPP_ID>/description
       {text, version: <updated_at>}                              -> 200 opportunity row, 409 stale
"""
from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone

import pipeline_v2
from pipeline_v2 import ConflictError, NotFoundError, ValidationError

MAX_TEXT = 200_000
SOURCE = "pasted_by_user"


def _text(payload: dict) -> str:
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("text is required")
    text = text.replace("\r\n", "\n").strip()
    if len(text) > MAX_TEXT:
        raise ValidationError(f"text is limited to {MAX_TEXT} characters")
    return text


def _content_hash(title, company, location, description, deadline) -> str:
    import job_sources

    return job_sources.content_hash(title, company, location, description, deadline)


def _row(connection, opportunity_id: str) -> dict:
    return pipeline_v2._row_dict(connection.execute(
        "SELECT * FROM opportunities WHERE id=?", (opportunity_id,)
    ).fetchone())


def paste_opportunity(db_path, payload: dict) -> dict:
    """Create (or update, if the URL is already known) an opportunity from pasted text."""
    if not isinstance(payload, dict):
        raise ValidationError("JSON body must be an object")
    if payload.get("version") != "new":
        raise ValidationError("version must be 'new' for paste creation")
    url = str(payload.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValidationError("url must be an http(s) URL")
    text = _text(payload)
    now = datetime.now(timezone.utc).isoformat()
    pipeline_v2.create_schema(db_path)
    with closing(pipeline_v2.connect(db_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT id, title, company, location, deadline, source_json FROM opportunities WHERE url=? LIMIT 1",
            (url,),
        ).fetchone()
        if existing:
            title = str(payload.get("title") or existing["title"])
            company = str(payload.get("company") or existing["company"])
            location = str(payload.get("location") or existing["location"])
            source = pipeline_v2._source_fields(dict(existing))
            source.update({
                "jd_fetch_status": "pasted", "jd_fetched_at": now, "full_job_description": text,
                "content_hash": _content_hash(title, company, location, text, existing["deadline"]),
            })
            connection.execute(
                """UPDATE opportunities SET title=?, company=?, location=?, description=?, role_kind=?,
                       content_hash=?, source_json=?, updated_at=? WHERE id=?""",
                (
                    title, company, location, text,
                    pipeline_v2.classify_opportunity({"full_job_description": text}),
                    source["content_hash"], json.dumps(source, ensure_ascii=False, sort_keys=True),
                    now, existing["id"],
                ),
            )
            connection.commit()
            return _row(connection, existing["id"])
        title = str(payload.get("title") or "").strip() or "Untitled"
        company = str(payload.get("company") or "").strip() or "Unknown"
        location = str(payload.get("location") or "").strip()
        opportunity_id = pipeline_v2.opportunity_identity({"url": url})
        source = {
            "source": SOURCE, "jd_fetch_status": "pasted", "jd_fetched_at": now,
            "full_job_description": text, "freshness_status": "unknown",
            "source_verification_status": "unverified",
            "content_hash": _content_hash(title, company, location, text, None),
        }
        scoring = pipeline_v2.compute_opportunity_score(source)
        connection.execute(
            """INSERT INTO opportunities(
                   id, title, company, location, url, source, publication_date, role_kind, role_family,
                   description, requirements, deadline, source_verification_status, fit_score,
                   eligibility_status, freshness_status, verification_confidence, priority_score,
                   score_schema_version, score_breakdown_json, archive_reason, match_score, status,
                   source_json, created_at, updated_at, content_hash
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                opportunity_id, title, company, location, url, SOURCE, None,
                pipeline_v2.classify_opportunity(source), "", text, "", None,
                scoring["source_verification_status"], scoring["fit_score"], scoring["eligibility_status"],
                scoring["freshness_status"], scoring["verification_confidence"], scoring["priority_score"],
                scoring["score_schema_version"], scoring["score_breakdown_json"], scoring["archive_reason"],
                scoring["fit_score"], "discovered",
                json.dumps(source, ensure_ascii=False, sort_keys=True), now, now, source["content_hash"],
            ),
        )
        connection.commit()
        return _row(connection, opportunity_id)


def attach_description(db_path, opportunity_id: str, payload: dict) -> dict:
    """Attach pasted text to an existing opportunity. Optimistic lock on updated_at."""
    if not isinstance(payload, dict):
        raise ValidationError("JSON body must be an object")
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        raise ValidationError("version is required for every opportunity mutation")
    text = _text(payload)
    now = datetime.now(timezone.utc).isoformat()
    with closing(pipeline_v2.connect(db_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT * FROM opportunities WHERE id=?", (opportunity_id,)
        ).fetchone()
        if current is None:
            connection.rollback()
            raise NotFoundError("opportunity not found")
        if version != current["updated_at"]:
            connection.rollback()
            raise ConflictError("opportunity changed; reload before retrying")
        source = pipeline_v2._source_fields(dict(current))
        source.update({
            "jd_fetch_status": "pasted", "jd_fetched_at": now, "full_job_description": text,
            "content_hash": _content_hash(
                current["title"], current["company"], current["location"], text, current["deadline"]
            ),
        })
        connection.execute(
            """UPDATE opportunities SET description=?, role_kind=?, content_hash=?, source_json=?, updated_at=?
               WHERE id=?""",
            (
                text, pipeline_v2.classify_opportunity({"full_job_description": text}),
                source["content_hash"], json.dumps(source, ensure_ascii=False, sort_keys=True),
                now, opportunity_id,
            ),
        )
        connection.commit()
        return _row(connection, opportunity_id)
