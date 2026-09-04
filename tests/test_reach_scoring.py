"""Scoring rules and the promotion gate for people candidates."""
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import migrate_pipeline_v2
import pipeline_v2
from reach import scoring

STAGING_DDL = """
CREATE TABLE IF NOT EXISTS target_companies (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, aliases_json TEXT, sector TEXT,
    country TEXT, intent TEXT, priority INTEGER, notes TEXT,
    created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS people_candidates (
    id TEXT PRIMARY KEY, target_company_id TEXT, name TEXT, headline TEXT,
    company_seen TEXT, role_seen TEXT, profile_url TEXT, email TEXT,
    evidence_url TEXT, evidence_quote TEXT, discovered_via TEXT, score INTEGER,
    verification_status TEXT, current_role_confirmed_at TEXT,
    promoted_contact_id TEXT, created_at TEXT, updated_at TEXT);
"""

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)


def _iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


class ScoreTests(unittest.TestCase):
    def score(self, **c):
        return scoring.score_candidate(c, "OCP", now=NOW)

    def test_recruiter_role(self):
        self.assertEqual(self.score(role_seen="Talent Acquisition Specialist"), 45)
        self.assertEqual(self.score(role_seen="Chargée de recrutement"), 45)

    def test_ai_manager_role(self):
        self.assertEqual(self.score(role_seen="Head of Data"), 35)
        self.assertEqual(self.score(role_seen="AI Lead"), 35)

    def test_engineer_role(self):
        self.assertEqual(self.score(role_seen="Data Engineer"), 20)

    def test_unknown_role_zero(self):
        self.assertEqual(self.score(role_seen="Accountant"), 0)

    def test_alumni_bonus(self):
        self.assertEqual(self.score(role_seen="Data Engineer, ENSAH alumni"), 30)

    def test_company_fuzzy_match(self):
        self.assertEqual(self.score(company_seen="ocp group"), 25)
        self.assertEqual(scoring.score_candidate({"company_seen": "OCP"}, "OCP Group", now=NOW), 25)
        self.assertEqual(self.score(company_seen="Managem"), 0)

    def test_evidence_age(self):
        self.assertEqual(self.score(evidence_at=_iso(10)), 15)
        self.assertEqual(self.score(evidence_at=_iso(200)), 8)
        self.assertEqual(self.score(evidence_at=_iso(400)), 0)

    def test_route_points(self):
        self.assertEqual(self.score(email="a@b.ma", verification_status="official_company_public"), 15)
        self.assertEqual(self.score(email="a@b.ma", verification_status="unverified"), 0)
        self.assertEqual(self.score(profile_url="https://www.linkedin.com/in/x"), 8)

    def test_cap_100(self):
        s = self.score(role_seen="Recruiter ENSAH alumni", company_seen="OCP",
                       evidence_at=_iso(1), email="a@ocp.ma",
                       verification_status="official_role_contact",
                       profile_url="https://www.linkedin.com/in/x")
        self.assertEqual(s, 100)


class PromoteTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        db = Path(self._dir.name) / "pipeline.sqlite3"
        pipeline_v2.create_schema(db)
        if hasattr(migrate_pipeline_v2, "ensure_reach_schema"):
            migrate_pipeline_v2.ensure_reach_schema(db)
        self.conn = pipeline_v2.connect(db)
        self.addCleanup(self.conn.close)
        self.conn.executescript(STAGING_DDL)
        self.conn.execute(
            "INSERT INTO target_companies(id, name, created_at, updated_at) VALUES "
            "('tgt_1', 'OCP', ?, ?)", (_iso(0), _iso(0)))
        self.conn.commit()

    def _candidate(self, **kw):
        uid = uuid.uuid4().hex
        row = {
            "id": "pc_" + uid, "target_company_id": "tgt_1",
            "name": "Sara Alami", "headline": "Recruiter at OCP",
            "company_seen": "OCP", "role_seen": "Recruiter",
            "profile_url": "https://www.linkedin.com/in/sara-alami-" + uid[:8], "email": None,
            "evidence_url": None, "evidence_quote": None,
            "discovered_via": "public_web", "score": 0,
            "verification_status": "unverified",
            "current_role_confirmed_at": _iso(0), "promoted_contact_id": None,
            "created_at": _iso(0), "updated_at": _iso(0),
        }
        row.update(kw)
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.conn.execute(f"INSERT INTO people_candidates ({cols}) VALUES ({marks})",
                          tuple(row.values()))
        self.conn.commit()
        return row["id"]

    def _routes(self, contact_id):
        return {r["route_type"]: r["value"] for r in self.conn.execute(
            "SELECT route_type, value FROM contact_routes WHERE contact_id = ?", (contact_id,))}

    def test_gate_raises_when_not_confirmed(self):
        for value in (None, ""):
            cid = self._candidate(current_role_confirmed_at=value)
            with self.assertRaises(ValueError) as ctx:
                scoring.promote(self.conn, cid)
            self.assertEqual(str(ctx.exception), "current role not confirmed")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 0)

    def test_promote_creates_contact_and_linkedin_route(self):
        cid = self._candidate()
        contact_id = scoring.promote(self.conn, cid)
        self.assertTrue(contact_id.startswith("ct_"))
        contact = self.conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        self.assertEqual(contact["name"], "Sara Alami")
        self.assertEqual(contact["company"], "OCP")
        self.assertEqual(contact["role"], "Recruiter")
        self.assertIn('"generator": "reach"', contact["source_json"])
        self.assertEqual(self._routes(contact_id), {"linkedin": self.conn.execute(
            "SELECT profile_url FROM people_candidates WHERE id = ?", (cid,)).fetchone()[0]})
        promoted = self.conn.execute(
            "SELECT promoted_contact_id FROM people_candidates WHERE id = ?", (cid,)).fetchone()[0]
        self.assertEqual(promoted, contact_id)

    def test_email_route_only_for_official_levels(self):
        for status in scoring.OFFICIAL_LEVELS:
            cid = self._candidate(email="s.alami@ocpgroup.ma", verification_status=status)
            routes = self._routes(scoring.promote(self.conn, cid))
            self.assertEqual(routes.get("email"), "s.alami@ocpgroup.ma", status)
        for status in ("profile_only", "unverified"):
            cid = self._candidate(email="s.alami@ocpgroup.ma", verification_status=status)
            routes = self._routes(scoring.promote(self.conn, cid))
            self.assertNotIn("email", routes, status)
            self.assertIn("linkedin", routes)

    def test_promote_is_idempotent(self):
        cid = self._candidate()
        first = scoring.promote(self.conn, cid)
        second = scoring.promote(self.conn, cid)
        self.assertEqual(first, second)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM contact_routes").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
