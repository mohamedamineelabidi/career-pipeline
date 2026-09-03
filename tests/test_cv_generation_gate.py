"""Tailored CVs are expensive; build them for jobs you actually chose.

Measured on the real database: 104 tailored PDFs, 135 MB on disk, and 91 of them
were built for jobs still sitting in `discovered` that were never triaged. Two
corresponded to an actual application.

Generation is gated on the job having been chosen, not on it having been seen.
The gate is overridable, because sometimes you want a CV ready before deciding.
"""
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import cv_workspace
import pipeline_v2
from pipeline_v2 import ValidationError


class CvGenerationGateTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.db = Path(self._dir.name) / "pipeline.sqlite3"
        pipeline_v2.create_schema(self.db)

    def _insert(self, status):
        with closing(pipeline_v2.connect(self.db)) as connection:
            connection.execute(
                """INSERT INTO opportunities
                   (id, title, company, role_kind, url, source, status,
                    freshness_status, source_verification_status, eligibility_status,
                    verification_confidence, description, fit_score, priority_score,
                    match_score, score_schema_version, score_breakdown_json,
                    source_json, created_at, updated_at)
                   VALUES ('opp_gate', 'Data Engineer', 'Acme', 'exact_vacancy',
                           'https://example.com/j', 'test', ?, 'recent',
                           'description_fetched', 'unknown', 85, 'x', 70, 70, 70, 2,
                           '{}', '{}', '2026-01-01T00:00:00+00:00',
                           '2026-01-01T00:00:00+00:00')""",
                (status,),
            )
            connection.commit()
        return "2026-01-01T00:00:00+00:00"

    def test_untriaged_job_is_refused(self):
        version = self._insert("discovered")
        with self.assertRaises(ValidationError) as caught:
            cv_workspace.generate_cv(
                self.db, {"opportunity_id": "opp_gate", "version": version},
            )
        self.assertIn("shortlist", str(caught.exception).lower())

    def test_refusal_explains_how_to_proceed(self):
        version = self._insert("verified_active")
        with self.assertRaises(ValidationError) as caught:
            cv_workspace.generate_cv(
                self.db, {"opportunity_id": "opp_gate", "version": version},
            )
        message = str(caught.exception).lower()
        self.assertTrue(
            "force" in message or "shortlist" in message,
            f"refusal is not actionable: {message}",
        )

    def test_chosen_statuses_pass_the_gate(self):
        """The gate must not be what blocks a shortlisted job."""
        for status in ("shortlisted", "eligible", "user_applied"):
            with self.subTest(status=status):
                self.assertTrue(cv_workspace.may_generate_cv(status))

    def test_untriaged_statuses_are_gated(self):
        for status in ("discovered", "verified_active"):
            with self.subTest(status=status):
                self.assertFalse(cv_workspace.may_generate_cv(status))

    def test_force_overrides_the_gate(self):
        """Sometimes you want a CV before deciding; that stays possible."""
        version = self._insert("discovered")
        try:
            cv_workspace.generate_cv(
                self.db,
                {"opportunity_id": "opp_gate", "version": version, "force": True},
            )
        except ValidationError as error:
            self.assertNotIn("shortlist", str(error).lower(),
                             "force did not bypass the gate")
        except Exception:
            pass  # rendering needs the optional cv extra; the gate is what matters


if __name__ == "__main__":
    unittest.main()
