"""Evidence scoring and the promotion gate for people candidates.

A candidate is a row in ``people_candidates``. ``score_candidate`` turns the
evidence we hold into a 0..100 number so the operator can rank who to verify
first. ``promote`` copies a candidate into ``contacts`` only once a human has
confirmed the person's current role; an email route is created only when the
address comes from an official or professional public source.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

OFFICIAL_LEVELS = (
    "official_role_contact",
    "official_company_public",
    "professional_public",
)

_RECRUITER_WORDS = ("recruit", "recrut", "talent acquisition", "talent")
_MANAGER_WORDS = ("manager", "lead", "head", "responsable", "directeur", "director", "chef")
_AI_WORDS = ("ai", "ia", "data", "ml", "machine learning", "intelligence artificielle")
_ENGINEER_WORDS = ("engineer", "ingénieur", "ingenieur", "developer", "développeur",
                   "scientist", "analyst", "consultant", "team")
_ALUMNI_WORDS = ("alumni", "ensah", "alumnus", "lauréat", "laureat")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _role_points(role: str) -> int:
    role = role.lower()
    if any(word in role for word in _RECRUITER_WORDS):
        return 45
    words = set(role.replace("/", " ").replace("-", " ").split())
    ai_hit = any(word in words or (len(word) > 3 and word in role) for word in _AI_WORDS)
    if ai_hit and any(word in role for word in _MANAGER_WORDS):
        return 35
    if any(word in role for word in _ENGINEER_WORDS):
        return 20
    return 0


def _company_matches(seen: str, target: str) -> bool:
    seen = (seen or "").strip().lower()
    target = (target or "").strip().lower()
    if not seen or not target:
        return False
    return seen in target or target in seen


def score_candidate(c: dict, target_company: str, now=None) -> int:
    """Return an integer score in 0..100 for a people_candidates row."""
    now_dt = _parse_dt(now) or datetime.now(timezone.utc)
    role = str(c.get("role_seen") or "")
    headline = str(c.get("headline") or "")
    score = _role_points(role) or _role_points(headline)
    text = (role + " " + headline).lower()
    if any(word in text for word in _ALUMNI_WORDS):
        score += 10
    if _company_matches(str(c.get("company_seen") or ""), target_company):
        score += 25
    evidence_dt = _parse_dt(c.get("evidence_at") or c.get("updated_at") or c.get("created_at"))
    if evidence_dt is not None:
        age = (now_dt - evidence_dt).days
        if age < 90:
            score += 15
        elif age <= 365:
            score += 8
    status = str(c.get("verification_status") or "")
    if c.get("email") and status in OFFICIAL_LEVELS:
        score += 15
    if c.get("profile_url"):
        score += 8
    return max(0, min(100, score))


def promote(conn: sqlite3.Connection, candidate_id: str) -> str:
    """Copy a confirmed candidate into contacts and return the contact id."""
    row = conn.execute("SELECT * FROM people_candidates WHERE id = ?",
                       (candidate_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown candidate {candidate_id}")
    cand = dict(row)
    if cand.get("promoted_contact_id"):
        return cand["promoted_contact_id"]
    if not (cand.get("current_role_confirmed_at") or "").strip():
        raise ValueError("current role not confirmed")

    target_row = conn.execute("SELECT name FROM target_companies WHERE id = ?",
                              (cand.get("target_company_id"),)).fetchone()
    company = cand.get("company_seen") or (target_row["name"] if target_row else "") or ""
    now = _now()
    contact_id = "ct_" + uuid.uuid4().hex
    source = {
        "generator": "reach",
        "people_candidate_id": candidate_id,
        "discovered_via": cand.get("discovered_via"),
        "evidence_url": cand.get("evidence_url"),
        "evidence_quote": cand.get("evidence_quote"),
        "verification_status": cand.get("verification_status"),
        "current_role_confirmed_at": cand.get("current_role_confirmed_at"),
    }
    conn.execute(
        "INSERT INTO contacts(id, name, company, role, source_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (contact_id, str(cand.get("name") or "Unknown"), str(company),
         str(cand.get("role_seen") or cand.get("headline") or ""),
         json.dumps(source, ensure_ascii=False, sort_keys=True), now, now),
    )
    if cand.get("profile_url"):
        conn.execute(
            "INSERT INTO contact_routes(id, contact_id, route_type, value, is_verified) "
            "VALUES (?, ?, 'linkedin', ?, 0)",
            ("cr_" + uuid.uuid4().hex, contact_id, cand["profile_url"]),
        )
    if cand.get("email") and cand.get("verification_status") in OFFICIAL_LEVELS:
        conn.execute(
            "INSERT INTO contact_routes(id, contact_id, route_type, value, is_verified) "
            "VALUES (?, ?, 'email', ?, 1)",
            ("cr_" + uuid.uuid4().hex, contact_id, cand["email"]),
        )
    conn.execute(
        "UPDATE people_candidates SET promoted_contact_id = ?, updated_at = ? WHERE id = ?",
        (contact_id, now, candidate_id),
    )
    conn.commit()
    return contact_id
