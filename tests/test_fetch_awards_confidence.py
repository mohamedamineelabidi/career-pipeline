"""A successful description fetch must raise the job's verification confidence.

Without this the new VERIFICATION_CONFIDENCE key is decorative: nothing ever
assigns 'description_fetched', so confidence stays 0 and the funnel stays frozen.
"""
import sqlite3
import unittest

import pipeline_v2


class FetchAwardsConfidenceTests(unittest.TestCase):
    def test_successful_fetch_sets_verification_status_and_confidence(self):
        import fetch_job_descriptions as fjd
        source = fjd.award_fetch_confidence({"source_verification_status": "unverified"}, "ok")
        self.assertEqual(source["source_verification_status"], "description_fetched")
        scoring = pipeline_v2.compute_opportunity_score(source)
        self.assertGreaterEqual(scoring["verification_confidence"], 80)

    def test_reader_fallback_also_counts(self):
        import fetch_job_descriptions as fjd
        source = fjd.award_fetch_confidence({}, "ok_reader")
        self.assertEqual(source["source_verification_status"], "description_fetched")

    def test_failed_fetch_awards_nothing(self):
        import fetch_job_descriptions as fjd
        for marker in ("login_wall", "blocked", "error"):
            source = fjd.award_fetch_confidence({"source_verification_status": "unverified"}, marker)
            self.assertEqual(source["source_verification_status"], "unverified", marker)

    def test_stronger_existing_evidence_is_never_downgraded(self):
        """An official canonical source must not be demoted to a mere fetch."""
        import fetch_job_descriptions as fjd
        source = fjd.award_fetch_confidence(
            {"source_verification_status": "verified_official_source"}, "ok")
        self.assertEqual(source["source_verification_status"], "verified_official_source")


if __name__ == "__main__":
    unittest.main()
