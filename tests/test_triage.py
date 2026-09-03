"""Triage queue: review the backlog one job at a time.

160 verified jobs need a human judgement call (shortlist or not) that no amount of
evidence can make. The queue serves the highest-priority undecided job with
everything needed to decide, so a session is keystrokes rather than drawer clicks.

Safety: triage can shortlist or close. It can NEVER apply or send.
"""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import pipeline_v2


class TriageQueueTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.db = Path(self._dir.name) / "pipeline.sqlite3"
        pipeline_v2.create_schema(self.db)

    def _insert(self, opp_id, **kw):
        fields = {
            "id": opp_id, "title": "Data Engineer", "company": "Acme",
            "role_kind": "exact_vacancy", "url": "https://example.com/job",
            "source": "test", "status": "verified_active", "freshness_status": "recent",
            "source_verification_status": "description_fetched",
            "eligibility_status": "eligible", "verification_confidence": 85,
            "description": "Build pipelines. " * 20, "fit_score": 70,
            "priority_score": 70, "match_score": 70, "score_schema_version": 2,
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

    def test_returns_highest_priority_job_first(self):
        self._insert("opp_low", priority_score=30)
        self._insert("opp_high", priority_score=95)
        job = pipeline_v2.triage_next(self.db)
        self.assertEqual(job["id"], "opp_high")

    def test_includes_what_is_needed_to_decide(self):
        self._insert("opp_1")
        job = pipeline_v2.triage_next(self.db)
        for key in ("id", "title", "company", "url", "description",
                    "priority_score", "fit_score", "remaining"):
            self.assertIn(key, job)

    def test_reports_how_many_remain(self):
        for i in range(3):
            self._insert(f"opp_{i}")
        self.assertEqual(pipeline_v2.triage_next(self.db)["remaining"], 3)

    def test_already_decided_jobs_are_not_served(self):
        self._insert("opp_done", status="user_applied")
        self._insert("opp_closed", status="closed")
        self.assertIsNone(pipeline_v2.triage_next(self.db))

    def test_empty_queue_returns_none(self):
        self.assertIsNone(pipeline_v2.triage_next(self.db))

    def test_skipped_job_is_not_served_again(self):
        self._insert("opp_1")
        pipeline_v2.triage_skip(self.db, "opp_1")
        self.assertIsNone(pipeline_v2.triage_next(self.db))

    def test_skip_does_not_change_status(self):
        """Skipping means 'not now', not a decision about the job."""
        self._insert("opp_1")
        pipeline_v2.triage_skip(self.db, "opp_1")
        with closing(sqlite3.connect(self.db)) as c:
            status = c.execute("SELECT status FROM opportunities WHERE id='opp_1'").fetchone()[0]
        self.assertEqual(status, "verified_active")


if __name__ == "__main__":
    unittest.main()
