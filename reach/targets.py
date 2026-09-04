"""Seed and grow the `target_companies` table.

Two entry points, both idempotent so the Reach loop can call them every cycle:

* `seed_targets` — hand-picked employer names.
* `derive_targets_from_opportunities` — employers behind eligible/shortlisted
  Moroccan listings already in the pipeline.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

MOROCCO_LOCATION_PATTERNS = ("%Moroc%", "%Maroc%", "%Casablanca%", "%Rabat%")
DERIVE_STATUSES = ("eligible", "shortlisted")

_INSERT_SQL = """
INSERT INTO target_companies (id, name, intent, created_at, updated_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(name) DO NOTHING
"""


def _new_id() -> str:
    return f"tgt_{uuid.uuid4().hex}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_names(conn: sqlite3.Connection, names, intent: str) -> int:
    now = _now()
    inserted = 0
    seen: set[str] = set()
    for raw in names:
        name = (raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        cursor = conn.execute(_INSERT_SQL, (_new_id(), name, intent, now, now))
        inserted += cursor.rowcount
    conn.commit()
    return inserted


def seed_targets(conn: sqlite3.Connection, names: list[str], intent: str = "any") -> int:
    """Insert `names` as target companies; return how many were newly added."""
    return _insert_names(conn, names, intent)


def derive_targets_from_opportunities(conn: sqlite3.Connection) -> int:
    """Add employers from eligible/shortlisted Moroccan opportunities; return rows added."""
    status_marks = ", ".join("?" for _ in DERIVE_STATUSES)
    location_clause = " OR ".join("location LIKE ?" for _ in MOROCCO_LOCATION_PATTERNS)
    rows = conn.execute(
        f"SELECT DISTINCT company FROM opportunities "
        f"WHERE status IN ({status_marks}) AND ({location_clause}) "
        f"ORDER BY company",
        (*DERIVE_STATUSES, *MOROCCO_LOCATION_PATTERNS),
    ).fetchall()
    return _insert_names(conn, (row[0] for row in rows), "any")
