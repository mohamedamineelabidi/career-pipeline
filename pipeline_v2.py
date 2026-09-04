"""Career Pipeline v2 normalized SQLite model and localhost API."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Union
from urllib.parse import unquote

PathLike = Union[str, Path]
MIGRATION_VERSION = 11

OPPORTUNITY_STATUSES = frozenset(
    {"discovered", "verified_active", "eligible", "shortlisted", "user_applied", "closed"}
)
DRAFT_STATUSES = frozenset(
    {"draft_local", "needs_verification", "reviewed", "approved_by_user", "sent_by_user", "replied", "closed"}
)
OUTCOME_TYPES = frozenset(
    {"application_submitted", "reply_received", "screening", "interview", "rejection", "offer", "withdrawn", "note", "other"}
)
AUTOMATION_RUN_STATUSES = frozenset({"success", "no_change", "partial", "blocked", "failed"})
VERIFICATION_CONFIDENCE = {
    # Fetching the description from the listing's own URL proves it resolved and
    # returned real content: strong evidence the vacancy is live, but weaker than
    # a canonical/official source, which is verified as well as authoritative.
    "description_fetched": 85,
    "verified_official_source": 95,
    "canonical_source_verified": 95,
    "official_canonical_active": 95,
    "official_canonical_filled": 95,
    "verified_official_email": 95,
    "verified_public_professional_email": 85,
    "professional_public": 85,
    "official_company_public": 90,
    "official_contact_route": 80,
    "profile_only_no_verified_email": 60,
    "profile_only": 60,
    "user_provided_unverified": 25,
    "user_provided_enrichment_unverified": 20,
    "enrichment_pending": 20,
    "rejected_unverified": 0,
    "unverified": 0,
}
DRAFT_TRANSITIONS = {
    "draft_local": {"needs_verification", "reviewed", "closed"},
    "needs_verification": {"draft_local", "reviewed", "closed"},
    "reviewed": {"draft_local", "approved_by_user", "closed"},
    "approved_by_user": {"reviewed", "sent_by_user", "closed"},
    "sent_by_user": {"replied", "closed"},
    "replied": {"closed"},
    "closed": set(),
}
# user_applied is reachable from every open status: the user may apply on the
# real website at any point; the app only records that fact after explicit confirmation.
OPPORTUNITY_TRANSITIONS = {
    "discovered": {"verified_active", "user_applied", "closed"},
    "verified_active": {"discovered", "eligible", "user_applied", "closed"},
    "eligible": {"verified_active", "shortlisted", "user_applied", "closed"},
    "shortlisted": {"eligible", "user_applied", "closed"},
    "user_applied": {"closed"},
    "closed": set(),
}


class ValidationError(ValueError):
    """Raised when a requested state transition violates the safe contract."""


class NotFoundError(LookupError):
    """Raised when a requested domain object does not exist."""


class ConflictError(RuntimeError):
    """Raised when a caller updates a stale record version."""


class ForbiddenError(PermissionError):
    """Raised when a browser origin is not the local API origin."""

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS opportunities (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    publication_date TEXT,
    role_kind TEXT NOT NULL CHECK(role_kind IN ('role_family', 'exact_vacancy')),
    role_family TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    requirements TEXT NOT NULL DEFAULT '',
    deadline TEXT,
    source_verification_status TEXT NOT NULL DEFAULT 'unverified',
    fit_score INTEGER NOT NULL CHECK(fit_score BETWEEN 0 AND 100),
    eligibility_status TEXT NOT NULL CHECK(eligibility_status IN ('eligible', 'blocked', 'unknown')),
    freshness_status TEXT NOT NULL CHECK(freshness_status IN ('active', 'recent', 'stale', 'expired', 'unknown')),
    verification_confidence INTEGER NOT NULL CHECK(verification_confidence BETWEEN 0 AND 100),
    priority_score INTEGER NOT NULL CHECK(priority_score BETWEEN 0 AND 100),
    score_schema_version INTEGER NOT NULL,
    score_breakdown_json TEXT NOT NULL,
    archive_reason TEXT NOT NULL DEFAULT '',
    match_score INTEGER NOT NULL CHECK(match_score BETWEEN 0 AND 100),
    status TEXT NOT NULL,
    source_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    source_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contact_routes (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    route_type TEXT NOT NULL CHECK(route_type IN ('email', 'linkedin', 'other')),
    value TEXT NOT NULL,
    is_verified INTEGER NOT NULL DEFAULT 0 CHECK(is_verified IN (0, 1)),
    UNIQUE(contact_id, route_type, value)
);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT REFERENCES opportunities(id) ON DELETE SET NULL,
    contact_id TEXT REFERENCES contacts(id) ON DELETE SET NULL,
    contact_route_id TEXT REFERENCES contact_routes(id) ON DELETE SET NULL,
    channel TEXT NOT NULL CHECK(channel IN ('email', 'linkedin', 'other')),
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    source_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cv_artifacts (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT REFERENCES opportunities(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    artifact_type TEXT NOT NULL CHECK(artifact_type IN ('base', 'tailored')),
    UNIQUE(opportunity_id, path)
);
CREATE UNIQUE INDEX IF NOT EXISTS cv_artifacts_one_type
ON cv_artifacts(opportunity_id, artifact_type);
CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    cv_artifact_id TEXT REFERENCES cv_artifacts(id) ON DELETE SET NULL,
    status TEXT NOT NULL,
    applied_at TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outreach_events (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT REFERENCES opportunities(id) ON DELETE SET NULL,
    contact_id TEXT REFERENCES contacts(id) ON DELETE SET NULL,
    draft_id TEXT REFERENCES drafts(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT 'user'
);
CREATE TABLE IF NOT EXISTS automation_runs (
    id TEXT PRIMARY KEY,
    run_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    details TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS lifecycle_events (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('opportunity', 'draft')),
    entity_id TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    confirmed_by_user INTEGER NOT NULL DEFAULT 0 CHECK(confirmed_by_user IN (0, 1))
);
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recruiter_reviews (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    cv_artifact_id TEXT NOT NULL REFERENCES cv_artifacts(id) ON DELETE CASCADE,
    recommendation TEXT NOT NULL CHECK(recommendation IN ('ready_to_send', 'needs_edits', 'regenerate')),
    ats_score REAL NOT NULL DEFAULT 0 CHECK(ats_score BETWEEN 0 AND 100),
    review_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(opportunity_id, cv_artifact_id)
);
-- The triage queue runs `WHERE status=? ORDER BY priority_score DESC` on every
-- keypress, and the dashboard filters by source. Both were full table scans.
CREATE INDEX IF NOT EXISTS opportunities_status_priority
    ON opportunities(status, priority_score DESC);
CREATE INDEX IF NOT EXISTS opportunities_source ON opportunities(source);
CREATE TRIGGER IF NOT EXISTS opportunities_immutable_id BEFORE UPDATE OF id ON opportunities
BEGIN SELECT RAISE(ABORT, 'immutable opportunity id'); END;
CREATE TRIGGER IF NOT EXISTS contacts_immutable_id BEFORE UPDATE OF id ON contacts
BEGIN SELECT RAISE(ABORT, 'immutable contact id'); END;
CREATE TRIGGER IF NOT EXISTS contact_routes_immutable_id BEFORE UPDATE OF id ON contact_routes
BEGIN SELECT RAISE(ABORT, 'immutable contact route id'); END;
CREATE TRIGGER IF NOT EXISTS drafts_immutable_id BEFORE UPDATE OF id ON drafts
BEGIN SELECT RAISE(ABORT, 'immutable draft id'); END;
CREATE TRIGGER IF NOT EXISTS cv_artifacts_immutable_id BEFORE UPDATE OF id ON cv_artifacts
BEGIN SELECT RAISE(ABORT, 'immutable CV artifact id'); END;
CREATE TRIGGER IF NOT EXISTS applications_immutable_id BEFORE UPDATE OF id ON applications
BEGIN SELECT RAISE(ABORT, 'immutable application id'); END;
CREATE TRIGGER IF NOT EXISTS outreach_events_immutable_id BEFORE UPDATE OF id ON outreach_events
BEGIN SELECT RAISE(ABORT, 'immutable outreach event id'); END;
CREATE TRIGGER IF NOT EXISTS automation_runs_immutable_id BEFORE UPDATE OF id ON automation_runs
BEGIN SELECT RAISE(ABORT, 'immutable automation run id'); END;
CREATE TRIGGER IF NOT EXISTS lifecycle_events_immutable_id BEFORE UPDATE OF id ON lifecycle_events
BEGIN SELECT RAISE(ABORT, 'immutable lifecycle event id'); END;
"""


SEMANTIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS semantic_scores (
    opportunity_id TEXT PRIMARY KEY REFERENCES opportunities(id) ON DELETE CASCADE,
    score REAL NOT NULL DEFAULT 0 CHECK(score BETWEEN 0 AND 100),
    model TEXT NOT NULL,
    skills_have_json TEXT NOT NULL DEFAULT '[]',
    skills_missing_json TEXT NOT NULL DEFAULT '[]',
    computed_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS opportunities_fts USING fts5(
    title, company, description, content='opportunities', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS opportunities_fts_ai AFTER INSERT ON opportunities BEGIN
  INSERT INTO opportunities_fts(rowid, title, company, description)
  VALUES (new.rowid, new.title, new.company, new.description);
END;
CREATE TRIGGER IF NOT EXISTS opportunities_fts_ad AFTER DELETE ON opportunities BEGIN
  INSERT INTO opportunities_fts(opportunities_fts, rowid, title, company, description)
  VALUES ('delete', old.rowid, old.title, old.company, old.description);
END;
CREATE TRIGGER IF NOT EXISTS opportunities_fts_au AFTER UPDATE OF title, company, description ON opportunities BEGIN
  INSERT INTO opportunities_fts(opportunities_fts, rowid, title, company, description)
  VALUES ('delete', old.rowid, old.title, old.company, old.description);
  INSERT INTO opportunities_fts(rowid, title, company, description)
  VALUES (new.rowid, new.title, new.company, new.description);
END;
"""


# v8: recruiter improvement loop rounds (recruiter_agent.improvement_loop).
IMPROVEMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS cv_improvement_rounds (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    round INTEGER NOT NULL,
    ats_before REAL NOT NULL DEFAULT 0,
    ats_after REAL NOT NULL DEFAULT 0,
    edits_json TEXT NOT NULL DEFAULT '[]',
    yaml_path TEXT,
    pdf_path TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(opportunity_id, round)
);
CREATE INDEX IF NOT EXISTS cv_improvement_rounds_opportunity
    ON cv_improvement_rounds(opportunity_id, round);
"""


RESUME_MATCHER_SCHEMA = """
CREATE TABLE IF NOT EXISTS interview_preps (
    opportunity_id TEXT PRIMARY KEY REFERENCES opportunities(id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cover_letter_drafts (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    language TEXT NOT NULL CHECK(language IN ('fr', 'en')),
    body TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft_local',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cover_letter_drafts_opportunity ON cover_letter_drafts(opportunity_id);
"""

# v9: LLM rubric scores (llm_scoring.py). Third signal next to rule + semantic scores.
LLM_SCORES_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_scores (
    opportunity_id TEXT PRIMARY KEY REFERENCES opportunities(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    fit INTEGER NOT NULL CHECK(fit BETWEEN 0 AND 100),
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""

# v11: outreach sequencer (outreach_sequences.py). Draft-only; nothing here sends.
OUTREACH_SCHEMA = """
CREATE TABLE IF NOT EXISTS outreach_sequences (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK(channel IN ('linkedin', 'email')),
    language TEXT NOT NULL CHECK(language IN ('fr', 'en')),
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'user_sent', 'replied', 'closed')),
    current_step INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outreach_steps (
    id TEXT PRIMARY KEY,
    sequence_id TEXT NOT NULL REFERENCES outreach_sequences(id) ON DELETE CASCADE,
    n INTEGER NOT NULL,
    due_date TEXT NOT NULL,
    template_id TEXT NOT NULL,
    body TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft', 'user_sent', 'replied', 'skipped')),
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    rephrased_by_llm INTEGER NOT NULL DEFAULT 0,
    marked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(sequence_id, n)
);
CREATE INDEX IF NOT EXISTS outreach_steps_due ON outreach_steps(state, due_date);
CREATE INDEX IF NOT EXISTS outreach_sequences_contact ON outreach_sequences(contact_id);
CREATE INDEX IF NOT EXISTS outreach_sequences_opportunity ON outreach_sequences(opportunity_id);
"""


def connect(db_path: PathLike) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if str(db_path) != ":memory:":
        connection.execute("PRAGMA journal_mode = WAL")
    return connection


APPLICATION_PREPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS application_preps (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    ats TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    filled_fields_json TEXT NOT NULL DEFAULT '{}',
    screenshot_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('prepared_awaiting_user', 'manual_only', 'failed')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS application_preps_opportunity ON application_preps(opportunity_id, created_at);
"""


def create_schema(db_path: PathLike) -> None:
    with closing(connect(db_path)) as connection:
        connection.executescript(SCHEMA)
        existing_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(opportunities)")
        }
        v2_columns = {
            "deadline": "ALTER TABLE opportunities ADD COLUMN deadline TEXT",
            "source_verification_status": "ALTER TABLE opportunities ADD COLUMN source_verification_status TEXT NOT NULL DEFAULT 'unverified'",
            "fit_score": "ALTER TABLE opportunities ADD COLUMN fit_score INTEGER NOT NULL DEFAULT 0",
            "eligibility_status": "ALTER TABLE opportunities ADD COLUMN eligibility_status TEXT NOT NULL DEFAULT 'unknown'",
            "freshness_status": "ALTER TABLE opportunities ADD COLUMN freshness_status TEXT NOT NULL DEFAULT 'unknown'",
            "verification_confidence": "ALTER TABLE opportunities ADD COLUMN verification_confidence INTEGER NOT NULL DEFAULT 0",
            "priority_score": "ALTER TABLE opportunities ADD COLUMN priority_score INTEGER NOT NULL DEFAULT 0",
            "score_schema_version": "ALTER TABLE opportunities ADD COLUMN score_schema_version INTEGER NOT NULL DEFAULT 2",
            "score_breakdown_json": "ALTER TABLE opportunities ADD COLUMN score_breakdown_json TEXT NOT NULL DEFAULT '{}'",
            "archive_reason": "ALTER TABLE opportunities ADD COLUMN archive_reason TEXT NOT NULL DEFAULT ''",
        }
        for column, statement in v2_columns.items():
            if column not in existing_columns:
                connection.execute(statement)
        draft_columns = {row[1] for row in connection.execute("PRAGMA table_info(drafts)")}
        if "contact_route_id" not in draft_columns:
            connection.execute("ALTER TABLE drafts ADD COLUMN contact_route_id TEXT")
        # v6: discovery layer (job_sources.py) — content-hash sync + structured job facts.
        v6_columns = {
            "content_hash": "ALTER TABLE opportunities ADD COLUMN content_hash TEXT",
            "job_type": "ALTER TABLE opportunities ADD COLUMN job_type TEXT",
            "is_remote": "ALTER TABLE opportunities ADD COLUMN is_remote INTEGER",
            "salary_min": "ALTER TABLE opportunities ADD COLUMN salary_min REAL",
            "salary_max": "ALTER TABLE opportunities ADD COLUMN salary_max REAL",
            "salary_currency": "ALTER TABLE opportunities ADD COLUMN salary_currency TEXT",
        }
        existing_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(opportunities)")
        }
        for column, statement in v6_columns.items():
            if column not in existing_columns:
                connection.execute(statement)
        # v7: semantic matching + skill gaps (semantic_match.py) and FTS5 search.
        connection.executescript(SEMANTIC_SCHEMA)
        # external-content FTS: rebuild the index from opportunities (idempotent, small table)
        connection.execute("INSERT INTO opportunities_fts(opportunities_fts) VALUES('rebuild')")
        # v8: improvement loop rounds table (idempotent).
        connection.executescript(IMPROVEMENT_SCHEMA)
        # Resume-Matcher port modules (idempotent, no version bump):
        # interview_preps + cover_letter_drafts (interview_prep.py / cover_letter.py).
        connection.executescript(RESUME_MATCHER_SCHEMA)
        # v9: LLM rubric scores (llm_scoring.py); idempotent, additive.
        connection.executescript(LLM_SCORES_SCHEMA)
        # v11: outreach sequencer tables (idempotent, draft-only).
        connection.executescript(OUTREACH_SCHEMA)
        # v10: application_preps (application_prep.py; pre-fill only, never submits). Idempotent.
        connection.executescript(APPLICATION_PREPS_SCHEMA)
        reconcile_contact_routes(connection)
        reconcile_draft_routes(connection)
        reconcile_opportunity_scores(connection)
        connection.execute(f"PRAGMA user_version = {MIGRATION_VERSION}")
        connection.commit()


def stable_id(kind: str, *parts: object) -> str:
    canonical = "\x1f".join(str(part or "").strip().casefold() for part in parts)
    digest = hashlib.sha256(f"career-pipeline-v2:{kind}:{canonical}".encode()).hexdigest()
    return f"{kind}_{digest[:24]}"


def normalize_company(value: object) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
    text = re.sub(r"\bs\s+a\s+r\s+l\b", "sarl", text)
    text = re.sub(r"\bs\s+a\s+s\b", "sas", text)
    text = re.sub(r"\bs\s+a\b", "sa", text)
    suffixes = {"inc", "incorporated", "ltd", "limited", "llc", "plc", "corp", "corporation", "company", "co", "sa", "sas", "sarl"}
    words = [part for part in text.split() if part not in suffixes]
    return " ".join(words)


def reconcile_contact_routes(connection: sqlite3.Connection) -> None:
    contacts = {
        row["id"]: _source_fields(dict(row)).get("verification_status", "unverified")
        for row in connection.execute("SELECT id, source_json FROM contacts")
    }
    for route in connection.execute("SELECT id, contact_id, route_type FROM contact_routes"):
        status = str(contacts.get(route["contact_id"], "unverified"))
        verified = int(
            (
                route["route_type"] == "email"
                and status in {
                    "verified_official_email", "verified_public_professional_email",
                    "professional_public", "official_company_public",
                }
            )
            or (
                route["route_type"] == "linkedin"
                and status in {"official_contact_route", "profile_only_no_verified_email", "profile_only"}
            )
        )
        connection.execute("UPDATE contact_routes SET is_verified=? WHERE id=?", (verified, route["id"]))


SOURCE_ALIASES = {
    "linkedin": "linkedin", "linked in": "linkedin", "linked-in": "linkedin",
    "weworkremotely": "weworkremotely", "we work remotely": "weworkremotely",
    "remoteok": "remoteok", "remote ok": "remoteok",
}


def normalize_source(value: object) -> str:
    """Collapse source spellings so one board is counted once.

    The dashboard listed 54 "sources" for roughly 20 real boards because
    'linkedin' and 'LinkedIn' were distinct strings, splitting 103 rows in two.
    """
    text = str(value or "").strip().casefold()
    if not text:
        return "unknown"
    collapsed = re.sub(r"[\s_-]+", " ", text).strip()
    squashed = collapsed.replace(" ", "")
    return SOURCE_ALIASES.get(collapsed) or SOURCE_ALIASES.get(squashed) or collapsed


# Hosts we can attribute with certainty. A company careers page is deliberately
# absent: guessing a board from an arbitrary domain would invent provenance.
KNOWN_JOB_HOSTS = {
    "linkedin.com": "linkedin",
    "weworkremotely.com": "weworkremotely",
    "remoteok.com": "remoteok",
    "remoteok.io": "remoteok",
    "welcometothejungle.com": "welcometothejungle",
    "indeed.com": "indeed",
    "glassdoor.com": "glassdoor",
    "wellfound.com": "wellfound",
    "angel.co": "wellfound",
    "greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "jobright.ai": "jobright",
}


def source_from_url(url: object) -> str | None:
    """Recover which board a listing came from, or None when it is not certain.

    Twenty jobs were stored as source='unknown' while their URL was plainly a
    linkedin.com job link, which understates where the pipeline actually finds
    work. Returning None for unrecognised hosts keeps 'unknown' honest.
    """
    text = str(url or "").strip()
    if not text:
        return None
    try:
        host = urllib.parse.urlsplit(text).hostname or ""
    except ValueError:
        return None
    host = host.casefold().removeprefix("www.")
    if not host:
        return None
    for known, name in KNOWN_JOB_HOSTS.items():
        if host == known or host.endswith("." + known):
            return name
    return None


_DUPLICATE_NOISE = re.compile(
    r"\((?:h/f|f/h|m/f|f/m|m/w|w/m)\)|\b(?:h/f|f/h|m/f|f/m)\b|[^\w\s]", re.IGNORECASE
)


def duplicate_key(title: object, company: object, location: object = "") -> str:
    """Identity of a vacancy independent of which URL it was posted under.

    content_hash only catches byte-identical rows, so the same job re-posted on a
    second board stayed as two entries. Title + company + location catches those.
    """
    def clean(value: object) -> str:
        text = str(value or "").strip().casefold()
        text = _DUPLICATE_NOISE.sub(" ", text)
        return re.sub(r"\s+", " ", text).strip()

    return "|".join((clean(title), clean(company), clean(location)))


def auto_advance_statuses(db_path: PathLike) -> list[dict]:
    """Advance `discovered` jobs that already satisfy the verification gate.

    Status is derived state, not manual bookkeeping. A job whose description was
    fetched from its own URL (confidence >= 80) and that is still fresh has already
    met every condition the transition guard asks for, so making a human click
    through it one by one only produced a 276-job backlog.

    Deliberately conservative:
      * only `discovered` -> `verified_active`, the one purely evidence-based hop;
      * `eligible`/`shortlisted` still need a human, because they encode judgement;
      * NEVER advances to `user_applied` -- applying is the user's action alone;
      * every move is logged to lifecycle_events with confirmed_by_user = 0 so a
        system decision is always distinguishable from the user's own, and
        reversible.

    Idempotent: a second run finds nothing left to move. Returns the moves made.
    """
    now = datetime.now(timezone.utc).isoformat()
    moved: list[dict] = []
    with closing(connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT id, status, verification_confidence, freshness_status
                 FROM opportunities
                WHERE status = 'discovered'
                  AND verification_confidence >= 80
                  AND freshness_status IN ('active', 'recent')"""
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE opportunities SET status='verified_active', updated_at=? WHERE id=?",
                (now, row["id"]),
            )
            connection.execute(
                """INSERT OR IGNORE INTO lifecycle_events(
                       id, entity_type, entity_id, from_status, to_status,
                       occurred_at, confirmed_by_user
                   ) VALUES (?, 'opportunity', ?, ?, ?, ?, 0)""",
                (
                    stable_id("life", "opportunity", row["id"],
                              row["status"], "verified_active", now),
                    row["id"], row["status"], "verified_active", now,
                ),
            )
            moved.append({
                "id": row["id"],
                "from_status": row["status"],
                "to_status": "verified_active",
            })
        connection.commit()
    return moved


TRIAGE_STATUSES = ("verified_active", "eligible")


def triage_next(db_path: PathLike) -> dict | None:
    """Serve the highest-priority job still awaiting a human judgement call.

    Evidence can prove a vacancy is live (auto_advance_statuses does that), but
    whether it is worth pursuing is a decision only the user can make. This serves
    one job at a time with everything needed to decide, so clearing a backlog is a
    short keyboard session instead of a drawer-click per job.

    Skipped jobs are held in source_json.triage_skipped rather than a status change,
    because "not now" is not a decision about the job.
    """
    marks = ", ".join("?" for _ in TRIAGE_STATUSES)
    with closing(connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""SELECT id, title, company, url, description, source, priority_score,
                       fit_score, verification_confidence, freshness_status,
                       eligibility_status, status, source_json
                  FROM opportunities
                 WHERE status IN ({marks})
                 ORDER BY priority_score DESC, updated_at DESC""",
            TRIAGE_STATUSES,
        ).fetchall()

    pending = []
    for row in rows:
        try:
            source = json.loads(row["source_json"] or "{}")
        except json.JSONDecodeError:
            source = {}
        if isinstance(source, dict) and source.get("triage_skipped"):
            continue
        pending.append(row)

    if not pending:
        return None

    row = pending[0]
    job = {key: row[key] for key in row.keys() if key != "source_json"}
    job["remaining"] = len(pending)
    return job


def triage_skip(db_path: PathLike, opportunity_id: str) -> dict:
    """Mark a job as 'not now' without deciding anything about it."""
    now = datetime.now(timezone.utc).isoformat()
    with closing(connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT source_json FROM opportunities WHERE id=?", (opportunity_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(f"unknown opportunity: {opportunity_id}")
        try:
            source = json.loads(row["source_json"] or "{}")
        except json.JSONDecodeError:
            source = {}
        if not isinstance(source, dict):
            source = {}
        source["triage_skipped"] = now
        connection.execute(
            "UPDATE opportunities SET source_json=?, updated_at=? WHERE id=?",
            (json.dumps(source, ensure_ascii=False), now, opportunity_id),
        )
        connection.commit()
    return {"id": opportunity_id, "triage_skipped": now}


def reconcile_opportunity_scores(connection: sqlite3.Connection) -> None:
    for row in connection.execute("SELECT * FROM opportunities"):
        source = _source_fields(dict(row))
        source.update({
            "fit_score": row["fit_score"],
            "score_schema_version": 2,
            "eligibility_status": row["eligibility_status"],
            "freshness_status": row["freshness_status"],
            "source_verification_status": row["source_verification_status"],
        })
        scoring = compute_opportunity_score(source)
        connection.execute(
            """UPDATE opportunities SET verification_confidence=?, priority_score=?,
                      score_breakdown_json=?, archive_reason=? WHERE id=?""",
            (
                scoring["verification_confidence"], scoring["priority_score"],
                scoring["score_breakdown_json"], scoring["archive_reason"], row["id"],
            ),
        )


def reconcile_draft_routes(connection: sqlite3.Connection) -> None:
    for draft in connection.execute(
        "SELECT id, contact_id, channel, contact_route_id FROM drafts WHERE contact_id IS NOT NULL"
    ):
        if draft["contact_route_id"]:
            continue
        routes = connection.execute(
            """SELECT id FROM contact_routes
               WHERE contact_id=? AND route_type=? AND is_verified=1""",
            (draft["contact_id"], draft["channel"]),
        ).fetchall()
        if len(routes) == 1:
            connection.execute(
                "UPDATE drafts SET contact_route_id=? WHERE id=?", (routes[0]["id"], draft["id"])
            )


def opportunity_identity(job: dict) -> str:
    supplied = str(job.get("stable_id") or "")
    if re.fullmatch(r"opp_[0-9a-f]{24}", supplied):
        return supplied
    url = str(job.get("link") or job.get("url") or "").strip()
    if url:
        return stable_id("opp", url)
    source_id = str(job.get("source_id") or job.get("id") or "").strip()
    if source_id:
        return stable_id("opp", job.get("source"), source_id)
    return stable_id("opp", job.get("company"), job.get("title"), job.get("location"))


def draft_identity(message: dict, linked_url: str) -> str:
    supplied = str(message.get("stable_id") or message.get("id") or "")
    if re.fullmatch(r"draft_[0-9a-f]{24}", supplied):
        return supplied
    return stable_id(
        "draft", linked_url, message.get("target"), message.get("company"),
        message.get("channel"), message.get("subject"),
    )


def normalize_score(value: object, schema_version: object = None) -> int:
    score = float(value or 0)
    if schema_version != 2 and 0 <= score <= 10:
        score *= 10
    return max(0, min(100, round(score)))


def classify_opportunity(job: dict) -> str:
    full_jd = next(
        (
            str(job.get(key) or "").strip()
            for key in ("full_job_description", "job_description")
            if str(job.get(key) or "").strip()
        ),
        "",
    )
    has_structure = bool(
        full_jd
        and any(
            marker in full_jd.casefold()
            for marker in ("responsibil", "require", "qualification", "mission")
        )
    )
    return "exact_vacancy" if len(full_jd) >= 200 and has_structure else "role_family"


def compute_opportunity_score(job: dict) -> dict:
    fit = normalize_score(
        job.get("fit_score", job.get("match", 0)), job.get("score_schema_version")
    )
    eligibility_raw = str(
        job.get("eligibility_status") or job.get("eligibility_verdict") or "unknown"
    ).strip().casefold()
    if eligibility_raw in {"eligible", "passed", "pass", "yes"}:
        eligibility = "eligible"
    elif eligibility_raw in {"blocked", "ineligible", "failed", "fail", "no"}:
        eligibility = "blocked"
    else:
        eligibility = "unknown"

    freshness_raw = str(job.get("freshness_status") or "unknown").strip().casefold()
    if freshness_raw not in {"active", "recent", "stale", "expired", "unknown"}:
        freshness_raw = "unknown"

    verification_status = str(
        job.get("source_verification_status") or job.get("verification_status") or "unverified"
    ).strip().casefold()
    explicit_confidence = job.get("verification_confidence")
    baseline_confidence = VERIFICATION_CONFIDENCE.get(verification_status, 0)
    if explicit_confidence not in (None, "") and baseline_confidence > 0:
        confidence = normalize_score(explicit_confidence, job.get("score_schema_version"))
    else:
        confidence = baseline_confidence

    freshness_score = {"active": 100, "recent": 80, "unknown": 40, "stale": 0, "expired": 0}[freshness_raw]
    archive_reason = ""
    if eligibility == "blocked":
        priority = 0
        archive_reason = "eligibility_blocked"
    elif freshness_raw in {"stale", "expired"}:
        priority = 0
        archive_reason = "deadline_expired" if freshness_raw == "expired" else "stale_source"
    else:
        priority = round(fit * 0.70 + confidence * 0.20 + freshness_score * 0.10)
    breakdown = {
        "fit_score": fit,
        "eligibility_status": eligibility,
        "freshness_status": freshness_raw,
        "freshness_score": freshness_score,
        "verification_confidence": confidence,
        "priority_score": priority,
        "weights": {"fit": 0.70, "verification_confidence": 0.20, "freshness": 0.10},
        "hard_gate_applied": bool(archive_reason),
    }
    return {
        **breakdown,
        "source_verification_status": verification_status,
        "score_schema_version": 2,
        "score_breakdown_json": json.dumps(breakdown, ensure_ascii=False, sort_keys=True),
        "archive_reason": archive_reason,
    }


def map_opportunity_status(job: dict) -> str:
    text = str(job.get("status") or "").strip().casefold()
    scoring = compute_opportunity_score(job)
    applied_legacy = text in {"applied", "applied by user", "user applied", "applied ✓", "applied ✔"} or bool(
        re.fullmatch(r"applied\s*[✓✔]\s*\([^)]*\)", text)
    )
    if applied_legacy:
        return "user_applied"
    closed_legacy = text in {"archived", "closed", "rejected", "filled"} or bool(
        re.fullmatch(r"(?:archived|closed|rejected|filled)\s*(?:[✓✔]|\([^)]*\))", text)
    )
    if scoring["archive_reason"] or closed_legacy:
        return "closed"
    if scoring["eligibility_status"] == "eligible" and scoring["freshness_status"] in {"active", "recent"}:
        return "shortlisted" if "priority" in text or "shortlist" in text else "eligible"
    if scoring["verification_confidence"] >= 80 and scoring["freshness_status"] in {"active", "recent"}:
        return "verified_active"
    return "discovered"


def map_draft_status(value: object) -> str:
    text = str(value or "").strip().casefold()
    if "unintentionally" in text or "unverified" in text or "enrichment" in text:
        return "needs_verification"
    if text == "sent_by_user":
        return "sent_by_user"
    if "composer" in text:
        return "needs_verification"
    if text in {"archived", "closed"}:
        return "closed"
    return "draft_local"


def lint_draft(channel: str, subject: str, body: str) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    clean_subject = str(subject or "").strip()
    clean_body = str(body or "").strip()
    words = re.findall(r"\b[\w'’-]+\b", clean_body, flags=re.UNICODE)
    if not clean_body:
        errors.append("body_missing")
    if channel == "email" and not clean_subject:
        errors.append("subject_missing")
    if len(clean_subject) > 60:
        errors.append("subject_too_long")
    if len(words) > 180:
        errors.append("body_too_long")
    if len(re.findall(r"https?://", clean_body, flags=re.IGNORECASE)) > 1:
        errors.append("too_many_links")
    if (clean_subject + clean_body).count("!") > 1:
        errors.append("excessive_exclamation")
    if re.search(r"<\s*(?:html|body|script|img|a)\b", clean_body, flags=re.IGNORECASE):
        errors.append("html_or_tracking_markup")
    spam_phrases = ("buy now", "click now", "free offer", "guaranteed", "act now", "limited time")
    if any(phrase in f"{clean_subject} {clean_body}".casefold() for phrase in spam_phrases):
        errors.append("promotional_or_spam_phrase")
    if channel == "email" and "reply" not in clean_body.casefold() and "répond" not in clean_body.casefold() and "réponse" not in clean_body.casefold():
        warnings.append("no_email_reply_cta")
    return {
        "status": "fail" if errors else "pass",
        "errors": errors,
        "warnings": warnings,
        "word_count": len(words),
        "subject_characters": len(clean_subject),
    }


def migrate(source_path: PathLike, db_path: PathLike) -> dict[str, int]:
    source = Path(source_path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("generated_read_only") is True:
        raise ValidationError("generated JSON snapshots are export-only and cannot be migrated")
    now = datetime.now(timezone.utc).isoformat()
    create_schema(db_path)
    with closing(connect(db_path)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            for incoming_job in payload.get("jobs", []):
                incoming_url = str(incoming_job.get("link") or incoming_job.get("url") or "").strip()
                existing_by_url = connection.execute(
                    "SELECT id FROM opportunities WHERE url=? LIMIT 1", (incoming_url,)
                ).fetchone() if incoming_url else None
                opportunity_id = existing_by_url["id"] if existing_by_url else opportunity_identity(incoming_job)
                job = dict(incoming_job)
                existing = connection.execute(
                    "SELECT source_json, status FROM opportunities WHERE id = ?", (opportunity_id,)
                ).fetchone()
                if existing:
                    try:
                        preserved = json.loads(existing["source_json"] or "{}")
                    except json.JSONDecodeError:
                        preserved = {}
                    if isinstance(preserved, dict):
                        job = {**preserved, **job}
                scoring = compute_opportunity_score(job)
                migrated_status = map_opportunity_status(job)
                if (
                    existing
                    and existing["status"] in {"shortlisted", "user_applied"}
                    and not scoring["archive_reason"]
                ):
                    migrated_status = existing["status"]
                connection.execute(
                    """
                    INSERT INTO opportunities(
                        id, title, company, location, url, source, publication_date,
                        role_kind, role_family, description, requirements, deadline,
                        source_verification_status, fit_score, eligibility_status,
                        freshness_status, verification_confidence, priority_score,
                        score_schema_version, score_breakdown_json, archive_reason,
                        match_score, status, source_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title=excluded.title, company=excluded.company,
                        location=excluded.location, url=excluded.url,
                        source=excluded.source, publication_date=excluded.publication_date,
                        role_kind=excluded.role_kind, role_family=excluded.role_family,
                        description=excluded.description,
                        requirements=excluded.requirements, deadline=excluded.deadline,
                        source_verification_status=excluded.source_verification_status,
                        fit_score=excluded.fit_score, eligibility_status=excluded.eligibility_status,
                        freshness_status=excluded.freshness_status,
                        verification_confidence=excluded.verification_confidence,
                        priority_score=excluded.priority_score,
                        score_schema_version=excluded.score_schema_version,
                        score_breakdown_json=excluded.score_breakdown_json,
                        archive_reason=excluded.archive_reason,
                        match_score=excluded.match_score, status=excluded.status,
                        source_json=excluded.source_json, updated_at=excluded.updated_at
                    """,
                    (
                        opportunity_id,
                        str(job.get("title") or "Untitled"),
                        str(job.get("company") or "Unknown"),
                        str(job.get("location") or ""),
                        str(job.get("link") or job.get("url") or ""),
                        str(job.get("source") or ""),
                        job.get("publication_date"),
                        classify_opportunity(job),
                        str(job.get("tailoring_archetype") or job.get("opportunity_track") or ""),
                        str(job.get("full_job_description") or job.get("job_description") or job.get("summary") or ""),
                        str(job.get("requirements") or ""),
                        job.get("deadline") or job.get("application_deadline"),
                        scoring["source_verification_status"],
                        scoring["fit_score"],
                        scoring["eligibility_status"],
                        scoring["freshness_status"],
                        scoring["verification_confidence"],
                        scoring["priority_score"],
                        scoring["score_schema_version"],
                        scoring["score_breakdown_json"],
                        scoring["archive_reason"],
                        scoring["fit_score"],
                        migrated_status,
                        json.dumps(job, ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )
            opportunity_by_url = {
                row["url"]: row["id"]
                for row in connection.execute("SELECT id, url FROM opportunities")
                if row["url"]
            }
            contact_by_name_company = {}
            for person in payload.get("people", []):
                normalized_person = dict(person)
                verification_status = str(
                    normalized_person.get("verification_status") or "unverified"
                )
                normalized_person["verification_status"] = verification_status
                contact_id = stable_id(
                    "con",
                    person.get("profile") or person.get("email") or "",
                    person.get("name"),
                    person.get("company"),
                )
                connection.execute(
                    """
                    INSERT INTO contacts(id, name, company, role, source_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name, company=excluded.company,
                        role=excluded.role, source_json=excluded.source_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        contact_id,
                        str(person.get("name") or "Unknown"),
                        str(person.get("company") or ""),
                        str(person.get("role") or person.get("contact_type") or ""),
                        json.dumps(normalized_person, ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )
                contact_by_name_company[
                    (str(person.get("name") or "").casefold(), str(person.get("company") or "").casefold())
                ] = contact_id
                for route_type, key in (("email", "email"), ("linkedin", "profile")):
                    value = str(person.get(key) or "").strip()
                    if value:
                        route_id = stable_id("route", contact_id, route_type, value)
                        connection.execute(
                            """
                            INSERT INTO contact_routes(id, contact_id, route_type, value, is_verified)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(id) DO UPDATE SET is_verified=excluded.is_verified
                            """,
                            (
                                route_id, contact_id, route_type, value,
                                int(
                                    (
                                        route_type == "email"
                                        and verification_status in {
                                            "verified_official_email",
                                            "verified_public_professional_email",
                                            "professional_public",
                                            "official_company_public",
                                        }
                                    )
                                    or (
                                        route_type == "linkedin"
                                        and verification_status in {
                                            "official_contact_route",
                                            "profile_only_no_verified_email",
                                            "profile_only",
                                        }
                                    )
                                ),
                            ),
                        )
            for job in payload.get("jobs", []):
                opportunity_id = opportunity_by_url.get(str(job.get("link") or job.get("url") or ""))
                if not opportunity_id:
                    continue
                for artifact_type, path_key, label_key in (
                    ("base", "cv", "cv_label"),
                    ("tailored", "tailored_cv", "tailored_cv_label"),
                ):
                    artifact_path = str(job.get(path_key) or "").strip()
                    if artifact_path:
                        artifact_id = stable_id("cv", opportunity_id, artifact_type, artifact_path)
                        connection.execute(
                            """
                            INSERT INTO cv_artifacts(id, opportunity_id, path, label, artifact_type)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(opportunity_id, artifact_type)
                            DO UPDATE SET path=excluded.path, label=excluded.label
                            """,
                            (artifact_id, opportunity_id, artifact_path, str(job.get(label_key) or ""), artifact_type),
                        )
                if map_opportunity_status(job) == "user_applied":
                    application_id = stable_id("app", opportunity_id)
                    connection.execute(
                        """
                        INSERT INTO applications(id, opportunity_id, status, created_at, updated_at)
                        VALUES (?, ?, 'user_applied', ?, ?)
                        ON CONFLICT(id) DO UPDATE SET status='user_applied', updated_at=excluded.updated_at
                        """,
                        (application_id, opportunity_id, now, now),
                    )
            for message in payload.get("messages", []):
                linked_url = str(message.get("linked_job") or message.get("related_job_link") or "")
                opportunity_id = opportunity_by_url.get(linked_url)
                contact_id = contact_by_name_company.get(
                    (str(message.get("target") or "").casefold(), str(message.get("company") or "").casefold())
                )
                body = str(message.get("text") or "")
                created_at = str(message.get("created_at") or now)
                draft_id = draft_identity(message, linked_url)
                channel = str(message.get("channel") or "").casefold()
                if channel not in {"email", "linkedin"}:
                    channel = "email" if message.get("email") else "linkedin" if message.get("profile") else "other"
                route_id = str(message.get("contact_route_id") or "") or None
                if not route_id and contact_id:
                    routes = connection.execute(
                        """SELECT id FROM contact_routes
                           WHERE contact_id=? AND route_type=? AND is_verified=1""",
                        (contact_id, channel),
                    ).fetchall()
                    if len(routes) == 1:
                        route_id = routes[0]["id"]
                draft_status = map_draft_status(message.get("status"))
                if draft_status == "sent_by_user" and message.get("confirmed_by_user") is not True:
                    draft_status = "draft_local"
                connection.execute(
                    """
                    INSERT INTO drafts(id, opportunity_id, contact_id, contact_route_id, channel, subject, body,
                                       status, source_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (
                        draft_id, opportunity_id, contact_id, route_id, channel,
                        str(message.get("subject") or ""), body,
                        draft_status,
                        json.dumps(message, ensure_ascii=False, sort_keys=True),
                        created_at, now,
                    ),
                )
                if draft_status == "sent_by_user":
                    connection.execute(
                        """UPDATE drafts SET status='sent_by_user', updated_at=?
                           WHERE id=? AND status IN ('draft_local','needs_verification','reviewed','approved_by_user')""",
                        (now, draft_id),
                    )
                    persisted = connection.execute(
                        "SELECT status FROM drafts WHERE id=?", (draft_id,)
                    ).fetchone()
                else:
                    persisted = connection.execute(
                        "SELECT status FROM drafts WHERE id=?", (draft_id,)
                    ).fetchone()
                if persisted and persisted["status"] == "sent_by_user":
                    event_id = stable_id("event", draft_id, "message_sent")
                    connection.execute(
                        """INSERT OR IGNORE INTO outreach_events(
                               id, opportunity_id, contact_id, draft_id, event_type, occurred_at, notes, created_by
                           ) VALUES (?, ?, ?, ?, 'message_sent', ?, 'Imported confirmed manual send', 'user')""",
                        (event_id, opportunity_id, contact_id, draft_id, created_at),
                    )
            connection.execute(
                """
                INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                ("source_updated", str(payload.get("updated") or ""), now),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
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
        }
        counts = {
            table: connection.execute(query).fetchone()[0]
            for table, query in count_queries.items()
        }
        return counts


def _row_dict(row: sqlite3.Row | None) -> dict:
    if row is None:
        raise NotFoundError("record not found")
    return dict(row)


def update_opportunity(db_path: PathLike, opportunity_id: str, changes: dict) -> dict:
    unknown = set(changes) - {"status", "version", "confirmed_by_user"}
    if unknown or "status" not in changes:
        raise ValidationError("only status, version, and confirmation may be changed")
    status = changes.get("status")
    if status not in OPPORTUNITY_STATUSES:
        raise ValidationError(f"invalid opportunity status: {status}")
    expected_version = changes.get("version")
    if not isinstance(expected_version, str) or not expected_version:
        raise ValidationError("version is required for every opportunity mutation")
    if status == "user_applied" and changes.get("confirmed_by_user") is not True:
        raise ValidationError("user_applied requires confirmed_by_user=true")
    now = datetime.now(timezone.utc).isoformat()
    with closing(connect(db_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT status, updated_at, eligibility_status, freshness_status, verification_confidence FROM opportunities WHERE id = ?",
            (opportunity_id,),
        ).fetchone()
        if current is None:
            connection.rollback()
            raise NotFoundError("opportunity not found")
        if expected_version != current["updated_at"]:
            connection.rollback()
            raise ConflictError("opportunity changed; reload before retrying")
        if status != current["status"] and status not in OPPORTUNITY_TRANSITIONS[current["status"]]:
            connection.rollback()
            raise ValidationError(f"invalid opportunity transition: {current['status']} -> {status}")
        if status in {"eligible", "shortlisted"} and (
            current["eligibility_status"] != "eligible"
            or current["freshness_status"] not in {"active", "recent"}
        ):
            connection.rollback()
            raise ValidationError("eligibility and freshness gates must pass before this status")
        if status == "verified_active" and (
            current["freshness_status"] not in {"active", "recent"}
            or current["verification_confidence"] < 80
        ):
            connection.rollback()
            raise ValidationError("verified source and freshness gates must pass before this status")
        connection.execute(
            "UPDATE opportunities SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, opportunity_id),
        )
        if status != current["status"]:
            connection.execute(
                """INSERT INTO lifecycle_events(
                       id, entity_type, entity_id, from_status, to_status, occurred_at, confirmed_by_user
                   ) VALUES (?, 'opportunity', ?, ?, ?, ?, ?)""",
                (
                    stable_id("life", "opportunity", opportunity_id, current["status"], status, now),
                    opportunity_id, current["status"], status, now,
                    int(changes.get("confirmed_by_user") is True),
                ),
            )
        if status == "user_applied" and status != current["status"]:
            application_id = stable_id("app", opportunity_id)
            connection.execute(
                """INSERT INTO applications(id, opportunity_id, status, applied_at, notes, created_at, updated_at)
                   VALUES (?, ?, 'user_applied', ?, 'Confirmed manually by user', ?, ?)
                   ON CONFLICT(id) DO UPDATE SET status='user_applied', applied_at=excluded.applied_at,
                       notes=excluded.notes, updated_at=excluded.updated_at""",
                (application_id, opportunity_id, now, now, now),
            )
        connection.commit()
        return _row_dict(connection.execute(
            "SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)
        ).fetchone())


def update_draft(db_path: PathLike, draft_id: str, changes: dict) -> dict:
    unknown = set(changes) - {"status", "body", "subject", "confirmed_by_user", "version"}
    if unknown or not (set(changes) & {"status", "body", "subject"}):
        raise ValidationError("only status, body, subject, version, and confirmation may be changed")
    expected_version = changes.get("version")
    if not isinstance(expected_version, str) or not expected_version:
        raise ValidationError("version is required for every draft mutation")
    status = changes.get("status")
    if status is not None and status not in DRAFT_STATUSES:
        raise ValidationError(f"invalid draft status: {status}")
    if status == "sent_by_user" and changes.get("confirmed_by_user") is not True:
        raise ValidationError("sent_by_user requires confirmed_by_user=true")
    now = datetime.now(timezone.utc).isoformat()
    with closing(connect(db_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            """SELECT d.*, c.company AS contact_company
               FROM drafts d LEFT JOIN contacts c ON c.id=d.contact_id WHERE d.id=?""",
            (draft_id,),
        ).fetchone()
        if current is None:
            connection.rollback()
            raise NotFoundError("draft not found")
        if expected_version != current["updated_at"]:
            connection.rollback()
            raise ConflictError("draft changed; reload before retrying")
        next_status = status or current["status"]
        if next_status != current["status"] and next_status not in DRAFT_TRANSITIONS[current["status"]]:
            connection.rollback()
            raise ValidationError(f"invalid draft transition: {current['status']} -> {next_status}")
        if current["status"] in {"approved_by_user", "sent_by_user", "replied", "closed"} and (
            "body" in changes or "subject" in changes
        ):
            connection.rollback()
            raise ValidationError("approved, sent, replied, or closed draft content is immutable")
        next_subject = changes.get("subject", current["subject"])
        next_body = changes.get("body", current["body"])
        if not isinstance(next_subject, str) or not isinstance(next_body, str):
            connection.rollback()
            raise ValidationError("subject and body must be strings")
        if next_status == "approved_by_user":
            verified_route = connection.execute(
                """SELECT 1 FROM contact_routes
                   WHERE id=? AND contact_id=? AND route_type=? AND is_verified=1""",
                (current["contact_route_id"], current["contact_id"], current["channel"]),
            ).fetchone()
            if not verified_route:
                connection.rollback()
                raise ValidationError("a verified route matching the draft channel is required before approval")
            lint = lint_draft(current["channel"], next_subject, next_body)
            if lint["status"] != "pass":
                connection.rollback()
                raise ValidationError("draft lint must pass before approval: " + ", ".join(lint["errors"]))
            company_key = normalize_company(current["contact_company"])
            if not company_key:
                connection.rollback()
                raise ValidationError("a verified non-empty company is required before approval")
            active_companies = connection.execute(
                """SELECT other.id, oc.company FROM drafts other
                   JOIN contacts oc ON oc.id=other.contact_id
                   WHERE other.id<>? AND other.status IN ('approved_by_user','sent_by_user','replied')""",
                (draft_id,),
            ).fetchall()
            if any(normalize_company(row["company"]) == company_key for row in active_companies):
                connection.rollback()
                raise ValidationError("company outreach collision: close the existing sequence first")
        connection.execute(
            "UPDATE drafts SET status=?, body=?, subject=?, updated_at=? WHERE id=?",
            (next_status, next_body, next_subject, now, draft_id),
        )
        if next_status != current["status"]:
            connection.execute(
                """INSERT INTO lifecycle_events(
                       id, entity_type, entity_id, from_status, to_status, occurred_at, confirmed_by_user
                   ) VALUES (?, 'draft', ?, ?, ?, ?, ?)""",
                (
                    stable_id("life", "draft", draft_id, current["status"], next_status, now),
                    draft_id, current["status"], next_status, now,
                    int(changes.get("confirmed_by_user") is True),
                ),
            )
        if next_status == "sent_by_user" and next_status != current["status"]:
            event_id = stable_id("event", draft_id, "message_sent", now)
            connection.execute(
                """INSERT INTO outreach_events(
                       id, opportunity_id, contact_id, draft_id, event_type, occurred_at, notes, created_by
                   ) VALUES (?, ?, ?, ?, 'message_sent', ?, 'Confirmed manually by user', 'user')""",
                (event_id, current["opportunity_id"], current["contact_id"], draft_id, now),
            )
        connection.commit()
        return _row_dict(connection.execute(
            "SELECT * FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone())


def record_outcome(db_path: PathLike, payload: dict) -> dict:
    allowed = {"opportunity_id", "contact_id", "draft_id", "event_type", "occurred_at", "notes"}
    if set(payload) - allowed:
        raise ValidationError("unknown outcome fields")
    event_type = payload.get("event_type")
    if event_type not in OUTCOME_TYPES:
        raise ValidationError(f"invalid outcome event_type: {event_type}")
    payload = dict(payload)
    occurred_at = str(payload.get("occurred_at") or datetime.now(timezone.utc).isoformat())
    with closing(connect(db_path)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            if payload.get("draft_id"):
                draft = connection.execute(
                    "SELECT opportunity_id, contact_id FROM drafts WHERE id=?", (payload["draft_id"],)
                ).fetchone()
                if draft is None:
                    raise ValidationError("outcome references an unknown draft")
                for field in ("opportunity_id", "contact_id"):
                    if payload.get(field) and payload[field] != draft[field]:
                        raise ValidationError(f"outcome {field} does not match the referenced draft")
                    payload[field] = payload.get(field) or draft[field]
            elif payload.get("opportunity_id") and payload.get("contact_id"):
                raise ValidationError("opportunity/contact outcomes require a linking draft")
            if not any(payload.get(field) for field in ("opportunity_id", "contact_id", "draft_id")):
                raise ValidationError("at least one outcome entity is required")
            event_id = stable_id(
                "event", payload.get("opportunity_id"), payload.get("contact_id"),
                payload.get("draft_id"), event_type, occurred_at,
            )
            connection.execute(
                """
                INSERT INTO outreach_events(
                    id, opportunity_id, contact_id, draft_id, event_type,
                    occurred_at, notes, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'user')
                """,
                (
                    event_id, payload.get("opportunity_id"), payload.get("contact_id"),
                    payload.get("draft_id"), event_type, occurred_at,
                    str(payload.get("notes") or ""),
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise ValidationError("outcome references an unknown record") from error
        return _row_dict(connection.execute(
            "SELECT * FROM outreach_events WHERE id = ?", (event_id,)
        ).fetchone())


def record_automation_run(
    db_path: PathLike,
    job_name: str,
    status: str,
    record_count: int = 0,
    details: str = "",
) -> dict:
    job_name = str(job_name or "").strip()
    if not job_name:
        raise ValidationError("job_name is required")
    if status not in AUTOMATION_RUN_STATUSES:
        raise ValidationError(f"invalid automation run status: {status}")
    if not isinstance(record_count, int) or record_count < 0:
        raise ValidationError("record_count must be a non-negative integer")
    now = datetime.now(timezone.utc).isoformat()
    run_id = stable_id("run", job_name, now)
    encoded_details = json.dumps(
        {"record_count": record_count, "details": str(details or "")},
        ensure_ascii=False,
        sort_keys=True,
    )
    with closing(connect(db_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO automation_runs(
                   id, run_type, status, started_at, finished_at, details
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, job_name, status, now, now, encoded_details),
        )
        connection.commit()
        result = _row_dict(connection.execute(
            "SELECT * FROM automation_runs WHERE id = ?", (run_id,)
        ).fetchone())
    result["record_count"] = record_count
    result["details"] = str(details or "")
    return result


def register_cv_artifact(
    db_path: PathLike, opportunity_id: str, artifact_path: str, label: str = "Tailored CV"
) -> dict:
    artifact_path = str(artifact_path or "").strip()
    if not artifact_path:
        raise ValidationError("artifact_path is required")
    artifact_id = stable_id("cv", opportunity_id, "tailored", artifact_path)
    with closing(connect(db_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if not connection.execute("SELECT 1 FROM opportunities WHERE id=?", (opportunity_id,)).fetchone():
            connection.rollback()
            raise NotFoundError("opportunity not found")
        existing = connection.execute(
            "SELECT id FROM cv_artifacts WHERE opportunity_id=? AND artifact_type='tailored'",
            (opportunity_id,),
        ).fetchone()
        if existing:
            artifact_id = existing["id"]
            connection.execute(
                "UPDATE cv_artifacts SET path=?, label=? WHERE id=?",
                (artifact_path, label, artifact_id),
            )
        else:
            connection.execute(
                """INSERT INTO cv_artifacts(id, opportunity_id, path, label, artifact_type)
                   VALUES (?, ?, ?, ?, 'tailored')""",
                (artifact_id, opportunity_id, artifact_path, label),
            )
        connection.commit()
        return _row_dict(connection.execute(
            "SELECT * FROM cv_artifacts WHERE id=?", (artifact_id,)
        ).fetchone())


def _fetch_all(db_path: PathLike, sql: str, parameters: tuple = ()) -> list[dict]:
    with closing(connect(db_path)) as connection:
        return [dict(row) for row in connection.execute(sql, parameters)]


def _source_fields(row: dict) -> dict:
    try:
        return json.loads(row.get("source_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def opportunity_description(db_path: PathLike, opportunity_id: str) -> dict:
    """Read-only detail: full description plus JD fetch metadata from source_json."""
    with closing(connect(db_path)) as connection:
        row = _row_dict(connection.execute(
            "SELECT id, description, source_json FROM opportunities WHERE id = ?", (opportunity_id,)
        ).fetchone())
    source = _source_fields(row)
    return {
        "id": row["id"],
        "description": str(row.get("description") or ""),
        "jd_fetch_status": str(source.get("jd_fetch_status") or ""),
        "jd_fetched_at": str(source.get("jd_fetched_at") or ""),
    }


def api_data(db_path: PathLike, endpoint: str):
    if endpoint == "opportunities":
        rows = _fetch_all(
            db_path,
            """
            SELECT o.*,
                   o.priority_score AS score,
                   COALESCE(
                     (SELECT path FROM cv_artifacts c WHERE c.opportunity_id=o.id
                      ORDER BY CASE c.artifact_type WHEN 'tailored' THEN 0 ELSE 1 END, c.id LIMIT 1),
                     ''
                   ) AS cv_path,
                   CASE WHEN EXISTS(SELECT 1 FROM cv_artifacts c WHERE c.opportunity_id=o.id)
                        THEN o.role_kind ELSE 'missing' END AS cv_status,
                   CAST(ROUND(ss.score) AS INTEGER) AS semantic_score,
                   CASE WHEN ss.skills_missing_json IS NULL THEN NULL
                        ELSE json_array_length(ss.skills_missing_json) END AS skills_missing_count,
                   ls.fit AS llm_fit,
                   (SELECT a.applied_at FROM applications a WHERE a.opportunity_id=o.id ORDER BY a.updated_at DESC LIMIT 1) AS applied_at
            FROM opportunities o
            LEFT JOIN semantic_scores ss ON ss.opportunity_id = o.id
            LEFT JOIN llm_scores ls ON ls.opportunity_id = o.id
            ORDER BY o.priority_score DESC, o.fit_score DESC, o.id
            """,
        )
        for row in rows:
            source = _source_fields(row)
            row["opportunity_track"] = str(source.get("opportunity_track") or "")
            row["jd_fetch_status"] = str(source.get("jd_fetch_status") or "")
            # publication_date / deadline are already columns; keep them additive and explicit
            row["publication_date"] = row.get("publication_date")
            row["deadline"] = row.get("deadline")
        return rows
    if endpoint == "contacts":
        rows = _fetch_all(db_path, "SELECT * FROM contacts ORDER BY company, name, id")
        with closing(connect(db_path)) as connection:
            for row in rows:
                source = _source_fields(row)
                routes = connection.execute(
                    "SELECT route_type, value, is_verified FROM contact_routes WHERE contact_id=? ORDER BY is_verified DESC, route_type",
                    (row["id"],),
                ).fetchall()
                row["verification_status"] = str(source.get("verification_status") or "unverified")
                row["has_verified_route"] = any(bool(route["is_verified"]) for route in routes)
                row["channel"] = ", ".join(route["route_type"] for route in routes) or "profile_only"
                row["evidence_url"] = str(
                    source.get("evidence_url") or source.get("profile") or source.get("source_url") or ""
                )
        return rows
    if endpoint == "drafts":
        rows = _fetch_all(
            db_path,
            """
            SELECT d.*, c.name AS contact_name, c.company AS contact_company,
                   c.source_json AS contact_source_json
            FROM drafts d LEFT JOIN contacts c ON c.id=d.contact_id
            ORDER BY d.created_at DESC, d.id
            """,
        )
        for row in rows:
            source = _source_fields(row)
            try:
                contact_source = json.loads(row.pop("contact_source_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                contact_source = {}
            row["recipient"] = row.get("contact_name") or source.get("target") or "unverified recipient"
            row["verification_status"] = str(contact_source.get("verification_status") or "unverified")
            with closing(connect(db_path)) as connection:
                row["has_verified_route"] = bool(connection.execute(
                    """SELECT 1 FROM contact_routes
                       WHERE contact_id=? AND route_type=? AND is_verified=1 LIMIT 1""",
                    (row.get("contact_id"), row["channel"]),
                ).fetchone())
            row["lint"] = lint_draft(row["channel"], row["subject"], row["body"])
            row["company_collision"] = False
        active_companies: dict[str, int] = {}
        for row in rows:
            if row["status"] in {"approved_by_user", "sent_by_user", "replied"}:
                company_key = str(row.get("contact_company") or "").strip().casefold()
                if company_key:
                    active_companies[company_key] = active_companies.get(company_key, 0) + 1
        for row in rows:
            company_key = str(row.get("contact_company") or "").strip().casefold()
            row["company_collision"] = bool(company_key and active_companies.get(company_key, 0))
        return rows
    if endpoint == "summary":
        with closing(connect(db_path)) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            source_updated = connection.execute(
                "SELECT value FROM metadata WHERE key='source_updated'"
            ).fetchone()
            latest_run = connection.execute(
                "SELECT status, finished_at FROM automation_runs ORDER BY finished_at DESC, id DESC LIMIT 1"
            ).fetchone()
            latest_success = connection.execute(
                "SELECT finished_at FROM automation_runs WHERE status='success' ORDER BY finished_at DESC, id DESC LIMIT 1"
            ).fetchone()
            return {
                "actionable": connection.execute(
                    "SELECT COUNT(*) FROM opportunities WHERE status IN ('eligible','shortlisted')"
                ).fetchone()[0],
                "drafts_ready": connection.execute(
                    "SELECT COUNT(*) FROM drafts WHERE status='approved_by_user'"
                ).fetchone()[0],
                "contacts_verified": connection.execute(
                    "SELECT COUNT(DISTINCT contact_id) FROM contact_routes WHERE is_verified=1"
                ).fetchone()[0],
                "cvs_ready": connection.execute(
                    "SELECT COUNT(DISTINCT opportunity_id) FROM cv_artifacts"
                ).fetchone()[0],
                "run_health": {
                    "status": (latest_run["status"] if latest_run else "unknown") if integrity == "ok" else "error",
                    "integrity": integrity,
                    "last_success_at": latest_success["finished_at"] if latest_success else "",
                    "freshness": (
                        latest_run["finished_at"] if latest_run and latest_run["finished_at"]
                        else source_updated[0] if source_updated else "unknown"
                    ),
                },
            }
    if endpoint == "funnel":
        with closing(connect(db_path)) as connection:
            opportunity_counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM opportunities GROUP BY status"
                )
            }
            event_counts = {
                row["event_type"]: row["count"]
                for row in connection.execute(
                    "SELECT event_type, COUNT(*) AS count FROM outreach_events GROUP BY event_type"
                )
            }
            approved = connection.execute(
                "SELECT COUNT(*) FROM drafts WHERE status='approved_by_user'"
            ).fetchone()[0]
        values = [
            ("discovered", opportunity_counts.get("discovered", 0)),
            ("verified_active", opportunity_counts.get("verified_active", 0)),
            ("eligible", opportunity_counts.get("eligible", 0)),
            ("shortlisted", opportunity_counts.get("shortlisted", 0)),
            ("approved_by_user", approved),
            ("user_applied", opportunity_counts.get("user_applied", 0)),
            ("response_received", event_counts.get("reply_received", 0)),
            ("screening", event_counts.get("screening", 0)),
            ("interview", event_counts.get("interview", 0)),
            ("offer", event_counts.get("offer", 0)),
            ("rejection", event_counts.get("rejection", 0)),
        ]
        return [{"stage": stage, "count": count} for stage, count in values]
    if endpoint == "health":
        with closing(connect(db_path)) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()[0]
        return {"status": "ok" if result == "ok" else "error", "database": result}
    raise NotFoundError("endpoint not found")


def export_json_snapshot(db_path: PathLike, output_path: PathLike) -> dict[str, int]:
    """Generate a legacy-compatible, non-operational JSON snapshot from SQLite."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    jobs = []
    people = []
    messages = []
    with closing(connect(db_path)) as connection:
        for row in connection.execute("SELECT * FROM opportunities ORDER BY priority_score DESC, id"):
            record = dict(row)
            try:
                item = json.loads(record.pop("source_json") or "{}")
            except json.JSONDecodeError:
                item = {}
            item.update({
                "stable_id": record["id"],
                "title": record["title"],
                "company": record["company"],
                "location": record["location"],
                "link": record["url"],
                "match": record["fit_score"],
                "fit_score": record["fit_score"],
                "eligibility_status": record["eligibility_status"],
                "freshness_status": record["freshness_status"],
                "verification_confidence": record["verification_confidence"],
                "priority_score": record["priority_score"],
                "score_schema_version": record["score_schema_version"],
                "score_breakdown": json.loads(record["score_breakdown_json"]),
                "lifecycle_status": record["status"],
                "archive_reason": record["archive_reason"],
                "role_kind": record["role_kind"],
            })
            for artifact in connection.execute(
                "SELECT artifact_type, path, label FROM cv_artifacts WHERE opportunity_id = ?",
                (record["id"],),
            ):
                if artifact["artifact_type"] == "base":
                    item["cv"] = artifact["path"]
                    if artifact["label"]:
                        item["cv_label"] = artifact["label"]
                elif artifact["artifact_type"] == "tailored":
                    item["tailored_cv"] = artifact["path"]
                    if artifact["label"]:
                        item["tailored_cv_label"] = artifact["label"]
                    artifact_path = Path(artifact["path"])
                    manifest_path = artifact_path.with_suffix(".manifest.json")
                    resolved_manifest = manifest_path if manifest_path.is_absolute() else output.parent / manifest_path
                    if resolved_manifest.is_file():
                        item["tailoring_manifest"] = str(manifest_path).replace("\\", "/")
            jobs.append(item)
        for row in connection.execute("SELECT * FROM contacts ORDER BY company, name, id"):
            record = dict(row)
            try:
                item = json.loads(record.pop("source_json") or "{}")
            except json.JSONDecodeError:
                item = {}
            item.setdefault("verification_status", "unverified")
            item["stable_id"] = record["id"]
            people.append(item)
        for row in connection.execute("SELECT * FROM drafts ORDER BY created_at, id"):
            record = dict(row)
            try:
                item = json.loads(record.pop("source_json") or "{}")
            except json.JSONDecodeError:
                item = {}
            item.update({
                "stable_id": record["id"], "subject": record["subject"],
                "text": record["body"], "channel": record["channel"],
                "status": record["status"], "created_at": record["created_at"],
            })
            messages.append(item)
    snapshot = {
        "generated_read_only": True,
        "operational_source": str(Path(db_path).name),
        "score_schema_version": 2,
        "updated": datetime.now(timezone.utc).isoformat(),
        "jobs": jobs,
        "people": people,
        "messages": messages,
    }
    temporary = output.with_suffix(output.suffix + ".tmp")
    if output.exists():
        output.chmod(0o644)
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o444)
    temporary.replace(output)
    return {"jobs": len(jobs), "people": len(people), "messages": len(messages)}


CV_PDF_ALLOWED_DIRS = ("cv_output", "reference_cv_2027/out", "reference_cv_2027/output")


def cv_preview_png(db_path: PathLike, opportunity_id: str, project_root: PathLike, scale: float = 1.6) -> bytes:
    """Render page 1 of the tailored CV to PNG (pypdfium2). Same allow-list as cv_pdf_bytes.

    Used by the dashboard instead of an iframe: the PDF plugin is blocked by CSP and
    freezes headless browsers, an image never does.
    """
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(cv_pdf_bytes(db_path, opportunity_id, project_root))
    try:
        bitmap = pdf[0].render(scale=scale, rev_byteorder=True)  # RGB(A), row-major
        width, height, stride = bitmap.width, bitmap.height, bitmap.stride
        channels = 4 if bitmap.format in (pdfium.raw.FPDFBitmap_BGRA, pdfium.raw.FPDFBitmap_BGRx) else 3
        raw = bytes(bitmap.buffer)
    finally:
        pdf.close()
    return _encode_png(raw, width, height, stride, channels)


def _encode_png(raw: bytes, width: int, height: int, stride: int, channels: int) -> bytes:
    """Minimal stdlib PNG encoder (RGB or RGBA, 8-bit) so the preview needs no Pillow."""
    import struct
    import zlib

    color_type = 6 if channels == 4 else 2
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter: none
        rows += raw[y * stride:y * stride + width * channels]

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    signature = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
    return (signature
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
            + chunk(b"IEND", b""))


def cv_pdf_bytes(db_path: PathLike, opportunity_id: str, project_root: PathLike) -> bytes:
    """Return the tailored CV PDF for an opportunity, restricted to allow-listed folders.

    Same-origin read-only stream used by the dashboard iframe preview. Any path that
    escapes ``cv_output/`` or ``reference_cv_2027/output/`` is rejected with 400.
    """
    record_id = str(opportunity_id or "").strip()
    if not record_id or "/" in record_id or "\\" in record_id or ".." in record_id:
        raise ValidationError("invalid opportunity id")
    root = Path(project_root).resolve()
    with closing(connect(db_path)) as connection:
        if not connection.execute("SELECT 1 FROM opportunities WHERE id=?", (record_id,)).fetchone():
            raise NotFoundError("opportunity not found")
        rows = connection.execute(
            "SELECT path, artifact_type FROM cv_artifacts WHERE opportunity_id=? "
            "ORDER BY CASE artifact_type WHEN 'tailored' THEN 0 ELSE 1 END, id",
            (record_id,),
        ).fetchall()
    if not rows:
        raise NotFoundError("no CV artifact registered")
    raw = str(rows[0]["path"] or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/") or ".." in raw.split("/") or ":" in raw:
        raise ValidationError("artifact path is not allowed")
    if raw.casefold().rsplit(".", 1)[-1] != "pdf":
        raise ValidationError("artifact is not a PDF")
    resolved = (root / raw).resolve()
    allowed = [(root / folder).resolve() for folder in CV_PDF_ALLOWED_DIRS]
    if not any(base == resolved or base in resolved.parents for base in allowed):
        raise ValidationError("artifact path is outside the allowed CV folders")
    if not resolved.is_file():
        raise NotFoundError("CV file not found on disk")
    return resolved.read_bytes()


def make_handler(db_path: PathLike, static_root: PathLike):
    database = Path(db_path)
    root = Path(static_root).resolve()

    class PipelineHandler(BaseHTTPRequestHandler):
        server_version = "CareerPipelineV2/1.0"

        def log_message(self, format, *args):
            return

        def _json(self, status: int, body: object) -> None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _payload(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValidationError("invalid Content-Length") from error
            if length <= 0 or length > 1_000_000:
                raise ValidationError("JSON body required and limited to 1 MB")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValidationError("invalid JSON") from error
            if not isinstance(value, dict):
                raise ValidationError("JSON body must be an object")
            return value

        def _error(self, error: Exception) -> None:
            if isinstance(error, ConflictError):
                status = 409
            elif isinstance(error, ForbiddenError):
                status = 403
            elif isinstance(error, NotFoundError):
                status = 404
            elif type(error).__name__ == "ServiceUnavailable":
                status = 503
            else:
                status = 400
            self._json(status, {"error": str(error)})

        def _enforce_same_origin(self) -> None:
            origin = self.headers.get("Origin")
            if not origin:
                return
            host = self.headers.get("Host", "")
            if origin != f"http://{host}":
                raise ForbiddenError("cross-origin state changes are forbidden")

        def _enforce_local_host(self) -> None:
            host = self.headers.get("Host", "").split(":", 1)[0].casefold()
            if host not in {"127.0.0.1", "localhost"}:
                raise ForbiddenError("only local Host headers are accepted")

        def do_GET(self):
            try:
                self._enforce_local_host()
                if self.path.startswith("/api/"):
                    raw = self.path.split("?", 1)
                    endpoint = raw[0].removeprefix("/api/")
                    query = raw[1] if len(raw) > 1 else ""
                    # Reach (targets / public people / Morocco radar / runs): read-only here.
                    if endpoint.startswith("reach/"):
                        from reach import api as reach_api

                        self._json(200, reach_api.handle_get(
                            database, endpoint.removeprefix("reach/"), dict(urllib.parse.parse_qsl(query))
                        ))
                        return
                    # Resume-Matcher port (read-only endpoints).
                    if endpoint.startswith("cvs/") and endpoint.endswith("/highlight"):
                        import keyword_highlight

                        record_id = unquote(endpoint.removeprefix("cvs/").removesuffix("/highlight"))
                        self._json(200, keyword_highlight.highlight(database, record_id, root=root))
                        return
                    if endpoint.startswith("interview/"):
                        import interview_prep

                        record_id = unquote(endpoint.removeprefix("interview/"))
                        self._json(200, interview_prep.get_prep(database, record_id))
                        return
                    if endpoint == "triage/next":
                        self._json(200, {"job": triage_next(database)})
                        return
                    if endpoint == "cover-letters":
                        import cover_letter
                        from urllib.parse import parse_qs

                        params = parse_qs(query)
                        self._json(200, cover_letter.list_drafts(
                            database, (params.get("opportunity_id") or [None])[0]
                        ))
                        return
                    if endpoint == "tracker" or endpoint.startswith("tracker/timeline/"):
                        import application_tracker

                        if endpoint == "tracker":
                            self._json(200, application_tracker.board(database))
                        else:
                            record_id = unquote(endpoint.removeprefix("tracker/timeline/"))
                            self._json(200, application_tracker.timeline(database, record_id))
                        return
                    if endpoint.startswith("cvs/") and endpoint.endswith("/preview.png"):
                        record_id = unquote(endpoint.removeprefix("cvs/").removesuffix("/preview.png"))
                        data = cv_preview_png(database, record_id, root)
                        self.send_response(200)
                        self.send_header("Content-Type", "image/png")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    if endpoint.startswith("cvs/") and endpoint.endswith("/pdf"):
                        record_id = unquote(endpoint.removeprefix("cvs/").removesuffix("/pdf"))
                        data = cv_pdf_bytes(database, record_id, root)
                        self.send_response(200)
                        self.send_header("Content-Type", "application/pdf")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.send_header("Content-Disposition", "inline")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    if endpoint.startswith("analytics/"):
                        import analytics
                        from urllib.parse import parse_qs

                        name = endpoint.removeprefix("analytics/")
                        function = analytics.ENDPOINTS.get(name)
                        if function is None:
                            raise NotFoundError(f"unknown analytics endpoint: {name}")
                        params = parse_qs(query)
                        kwargs: dict = {}
                        if name == "weekly" and params.get("weeks"):
                            kwargs["weeks"] = max(1, min(52, int(params["weeks"][0])))
                        if name == "skills" and params.get("top"):
                            kwargs["top"] = max(1, min(200, int(params["top"][0])))
                        self._json(200, function(database, **kwargs))
                        return
                    if endpoint == "applications/preps" or endpoint.startswith("applications/prep/"):
                        import application_prep

                        if endpoint == "applications/preps":
                            self._json(200, application_prep.list_preps(database))
                        else:
                            record_id = unquote(endpoint.removeprefix("applications/prep/"))
                            self._json(200, application_prep.latest_prep(database, record_id))
                        return
                    if endpoint == "cvs" or endpoint.startswith("cvs/"):
                        import cv_workspace

                        if endpoint == "cvs":
                            self._json(200, cv_workspace.list_cvs(
                                database, project_root=root,
                                filters=cv_workspace.parse_filters(query),
                            ))
                        else:
                            record_id = unquote(endpoint.removeprefix("cvs/"))
                            self._json(200, cv_workspace.cv_detail(
                                database, record_id, project_root=root
                            ))
                        return
                    if endpoint == "recruiter/reviews" or endpoint.startswith("recruiter/reviews/"):
                        import recruiter_agent

                        if endpoint == "recruiter/reviews":
                            self._json(200, recruiter_agent.list_reviews(database))
                        else:
                            record_id = unquote(endpoint.removeprefix("recruiter/reviews/"))
                            self._json(200, recruiter_agent.reviews_for_opportunity(database, record_id))
                        return
                    if endpoint.startswith("recruiter/improvements/"):
                        import recruiter_agent

                        record_id = unquote(endpoint.removeprefix("recruiter/improvements/"))
                        self._json(200, recruiter_agent.improvements_for_opportunity(database, record_id))
                        return
                    if endpoint.startswith("opportunities/") and endpoint.endswith("/description"):
                        record_id = unquote(endpoint.removeprefix("opportunities/").removesuffix("/description"))
                        self._json(200, opportunity_description(database, record_id))
                        return
                    if endpoint.startswith("llm-score/"):
                        import llm_scoring

                        record_id = unquote(endpoint.removeprefix("llm-score/"))
                        self._json(200, llm_scoring.get_score(database, record_id))
                        return
                    if endpoint == "pipeline/latest" or endpoint == "pipeline/runs" or endpoint.startswith("pipeline/runs/"):
                        import pipeline_runner

                        if endpoint == "pipeline/latest":
                            self._json(200, pipeline_runner.latest(database) or {"status": "never_run", "stages": []})
                        elif endpoint == "pipeline/runs":
                            self._json(200, pipeline_runner.list_runs(database))
                        else:
                            self._json(200, pipeline_runner.get_run(database, unquote(endpoint.removeprefix("pipeline/runs/"))))
                        return
                    if endpoint == "match/gaps" or endpoint.startswith("match/") or endpoint == "search":
                        import semantic_match
                        from urllib.parse import parse_qs

                        params = parse_qs(query)
                        if endpoint == "match/gaps":
                            self._json(200, semantic_match.skill_gaps(
                                database,
                                limit=int((params.get("limit") or ["25"])[0]),
                                open_only=(params.get("open_only") or ["1"])[0] not in {"0", "false"},
                            ))
                        elif endpoint == "search":
                            self._json(200, semantic_match.search(
                                database, (params.get("q") or [""])[0],
                                limit=int((params.get("limit") or ["25"])[0]),
                            ))
                        else:
                            record_id = unquote(endpoint.removeprefix("match/"))
                            self._json(200, semantic_match.match_detail(database, record_id))
                        return
                    # Workstream D: outreach sequencer (read-only GETs) + applied picker.
                    if endpoint in {"outreach/sequences", "outreach/due"} or endpoint == "opportunities/search-lite":
                        import outreach_sequences

                        params = outreach_sequences.parse_query(query)
                        if endpoint == "outreach/sequences":
                            self._json(200, outreach_sequences.list_sequences(
                                database, params.get("contact_id"), params.get("opportunity_id")
                            ))
                        elif endpoint == "outreach/due":
                            self._json(200, outreach_sequences.due(database, params.get("date")))
                        else:
                            self._json(200, outreach_sequences.search_lite(database, params.get("q", "")))
                        return
                    self._json(200, api_data(database, endpoint))
                    return
                request_path = unquote(self.path.split("?", 1)[0])
                if request_path in {"/", "/pipeline_v2.html"}:
                    page = root / "pipeline_v2.html"
                    content_type = "text/html; charset=utf-8"
                elif request_path == "/reach.html":
                    page = root / "reach.html"
                    content_type = "text/html; charset=utf-8"
                else:
                    page = (root / request_path.lstrip("/")).resolve()
                    if root not in page.parents or page.suffix.casefold() != ".pdf":
                        raise NotFoundError("file not found")
                    content_type = "application/pdf"
                data = page.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Content-Type-Options", "nosniff")
                if content_type.startswith("text/html"):
                    self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                self.wfile.write(data)
            except (ValidationError, NotFoundError, ForbiddenError, OSError) as error:
                self._error(error)

        def do_PATCH(self):
            try:
                self._enforce_local_host()
                self._enforce_same_origin()
                path = self.path.split("?", 1)[0]
                payload = self._payload()
                if path.startswith("/api/opportunities/"):
                    record_id = path.removeprefix("/api/opportunities/")
                    result = update_opportunity(database, record_id, payload)
                elif path.startswith("/api/drafts/"):
                    record_id = path.removeprefix("/api/drafts/")
                    result = update_draft(database, record_id, payload)
                else:
                    raise NotFoundError("endpoint not found")
                self._json(200, result)
            except (ValidationError, NotFoundError, ConflictError, ForbiddenError) as error:
                self._error(error)

        def do_POST(self):
            try:
                self._enforce_local_host()
                self._enforce_same_origin()
                path = self.path.split("?", 1)[0]
                if path == "/api/triage/skip":
                    payload = self._payload()
                    self._json(200, triage_skip(database, str(payload.get("id") or "")))
                    return
                if path == "/api/applications/prepare":
                    import application_prep

                    self._json(200, application_prep.prepare_endpoint(
                        database, self._payload(), project_root=root
                    ))
                    return
                if path == "/api/cvs/generate":
                    import cv_workspace

                    self._json(201, cv_workspace.generate_cv(
                        database, self._payload(), project_root=root
                    ))
                    return
                # Resume-Matcher port (local drafts only; never sends).
                if path == "/api/interview/generate":
                    import interview_prep

                    self._json(201, interview_prep.generate(database, self._payload(), root=root))
                    return
                if path == "/api/cover-letters/generate":
                    import cover_letter

                    self._json(201, cover_letter.generate(database, self._payload(), root=root))
                    return
                if path == "/api/tracker/move":
                    import application_tracker

                    self._json(200, application_tracker.move(database, self._payload()))
                    return
                if path == "/api/recruiter/review":
                    import recruiter_agent

                    self._json(201, recruiter_agent.run_review(
                        database, self._payload(), root=root
                    ))
                    return
                if path == "/api/recruiter/improve":
                    import recruiter_agent

                    self._json(201, recruiter_agent.run_improvement(
                        database, self._payload(), root=root
                    ))
                    return
                if path == "/api/llm-score/recompute":
                    import llm_scoring

                    self._json(200, llm_scoring.recompute_endpoint(database, self._payload()))
                    return
                if path == "/api/pipeline/run":
                    import pipeline_runner

                    self._json(202, pipeline_runner.start_background(database, self._payload()))
                    return
                if path.startswith("/api/llm-score/"):
                    import llm_scoring

                    record_id = unquote(path.removeprefix("/api/llm-score/"))
                    self._json(201, llm_scoring.score_endpoint(database, record_id, self._payload()))
                    return
                if path == "/api/match/recompute":
                    import semantic_match

                    options = semantic_match.parse_recompute_payload(self._payload())
                    self._json(200, semantic_match.recompute(database, **options))
                    return
                if path == "/api/opportunities/paste":
                    import paste_import

                    self._json(201, paste_import.paste_opportunity(database, self._payload()))
                    return
                if path.startswith("/api/opportunities/") and path.endswith("/description"):
                    import paste_import

                    record_id = unquote(
                        path.removeprefix("/api/opportunities/").removesuffix("/description")
                    )
                    self._json(200, paste_import.attach_description(
                        database, record_id, self._payload()
                    ))
                    return
                # Workstream D: outreach sequencer (draft-only; user_sent needs confirmed:true).
                if path == "/api/outreach/sequences":
                    import outreach_sequences

                    self._json(201, outreach_sequences.create_from_payload(
                        database, self._payload(), llm=outreach_sequences.default_llm
                    ))
                    return
                if path.startswith("/api/outreach/steps/") and path.endswith(("/mark", "/regenerate")):
                    import outreach_sequences

                    tail = path.removeprefix("/api/outreach/steps/")
                    if tail.endswith("/mark"):
                        step_id = unquote(tail.removesuffix("/mark"))
                        self._json(200, outreach_sequences.mark_from_payload(database, step_id, self._payload()))
                    else:
                        step_id = unquote(tail.removesuffix("/regenerate"))
                        self._json(200, outreach_sequences.regenerate_from_payload(
                            database, step_id, self._payload(), llm=outreach_sequences.default_llm
                        ))
                    return
                if path.startswith("/api/opportunities/") and path.endswith("/applied"):
                    import outreach_sequences

                    record_id = unquote(path.removeprefix("/api/opportunities/").removesuffix("/applied"))
                    self._json(200, outreach_sequences.mark_applied(database, record_id, self._payload()))
                    return
                # Reach: targets / people gates / draft-only outreach / stage runs. Never sends.
                if path.startswith("/api/reach/"):
                    from reach import api as reach_api

                    status, body = reach_api.handle_post(
                        database, path.removeprefix("/api/reach/"), self._payload(), root=root
                    )
                    self._json(status, body)
                    return
                if path != "/api/outcomes":
                    raise NotFoundError("endpoint not found")
                self._json(201, record_outcome(database, self._payload()))
            except (ValidationError, NotFoundError, ConflictError, ForbiddenError) as error:
                self._error(error)

        def do_OPTIONS(self):
            self._json(405, {"error": "CORS is disabled"})

    return PipelineHandler


def make_server(db_path: PathLike, static_root: PathLike, port: int = 8787) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(
        ("127.0.0.1", port), make_handler(db_path, static_root), bind_and_activate=True
    )
