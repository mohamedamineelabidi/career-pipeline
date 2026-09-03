"""Status must be derived from evidence, not manual bookkeeping.

276 of 295 jobs sat in `discovered` forever because advancing was a human chore
nobody performed. A job that already satisfies the existing gate should advance
itself, and say so in lifecycle_events.

Safety: the system NEVER advances to user_applied. Applying is the user's action.
"""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import pipeline_v2


class AutoAdvanceTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.db = Path(self._dir.name) / "pipeline.sqlite3"
        pipeline_v2.create_schema(self.db)

    def _insert(self, **kw):
        fields = {
            "id": "opp_auto_1", "title": "Data Engineer", "company": "Acme",
            "role_kind": "exact_vacancy", "url": "https://example.com/job",
            "source": "test", "status": "discovered", "freshness_status": "recent",
            "source_verification_status": "description_fetched",
            "eligibility_status": "unknown", "verification_confidence": 85,
            "description": "x" * 300, "fit_score": 70, "priority_score": 70,
            "match_score": 70, "score_schema_version": 2,
            "score_breakdown_json": "{}", "source_json": "{}",
            "created_at": "2026-09-01T00:00:00+00:00",
            "updated_at": "2026-09-01T00:00:00+00:00",
        }
        fields.update(kw)
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with closing(sqlite3.connect(self.db)) as c:
            c.execute(f"INSERT INTO opportunities ({cols}) VALUES ({marks})",
                      tuple(fields.values()))
            c.commit()
        return fields["id"]

    def _status(self, opp_id):
        with closing(sqlite3.connect(self.db)) as c:
            return c.execute("SELECT status FROM opportunities WHERE id=?",
                             (opp_id,)).fetchone()[0]

    def test_job_with_evidence_advances_to_verified_active(self):
        opp = self._insert()
        moved = pipeline_v2.auto_advance_statuses(self.db)
        self.assertEqual(len(moved), 1)
        self.assertEqual(self._status(opp), "verified_active")

    def test_advance_is_recorded_as_a_system_event(self):
        opp = self._insert()
        pipeline_v2.auto_advance_statuses(self.db)
        with closing(sqlite3.connect(self.db)) as c:
            row = c.execute(
                """SELECT from_status, to_status, confirmed_by_user FROM lifecycle_events
                   WHERE entity_id=? ORDER BY occurred_at DESC LIMIT 1""", (opp,)).fetchone()
        self.assertEqual(row[0], "discovered")
        self.assertEqual(row[1], "verified_active")
        self.assertEqual(row[2], 0, "system advance must not be marked user-confirmed")

    def test_job_without_confidence_stays_put(self):
        opp = self._insert(source_verification_status="unverified",
                           verification_confidence=0)
        self.assertEqual(pipeline_v2.auto_advance_statuses(self.db), [])
        self.assertEqual(self._status(opp), "discovered")

    def test_stale_job_stays_put(self):
        opp = self._insert(freshness_status="stale")
        self.assertEqual(pipeline_v2.auto_advance_statuses(self.db), [])
        self.assertEqual(self._status(opp), "discovered")

    def test_never_auto_advances_to_user_applied(self):
        """Applying is the user's action alone. No evidence may trigger it."""
        self._insert(status="eligible", eligibility_status="eligible")
        moved = pipeline_v2.auto_advance_statuses(self.db)
        for m in moved:
            self.assertNotEqual(m["to_status"], "user_applied")

    def test_user_applied_is_never_touched(self):
        opp = self._insert(status="user_applied")
        self.assertEqual(pipeline_v2.auto_advance_statuses(self.db), [])
        self.assertEqual(self._status(opp), "user_applied")

    def test_is_idempotent(self):
        self._insert()
        pipeline_v2.auto_advance_statuses(self.db)
        self.assertEqual(pipeline_v2.auto_advance_statuses(self.db), [],
                         "second run must be a no-op")


if __name__ == "__main__":
    unittest.main()
