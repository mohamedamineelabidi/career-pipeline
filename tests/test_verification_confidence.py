"""Verification confidence must be earned from real evidence, not hardcoded to zero.

Root cause this pins: both ingesters set source_verification_status="unverified",
which maps to confidence 0, so the >=80 gate in the transition guard could never
open and 276/295 jobs froze in `discovered` forever.
"""
import unittest

import pipeline_v2


class VerificationConfidenceTests(unittest.TestCase):
    def test_unverified_still_scores_zero(self):
        """A job we know nothing about earns nothing."""
        scoring = pipeline_v2.compute_opportunity_score({
            "title": "Data Engineer", "company": "Acme",
            "source_verification_status": "unverified",
        })
        self.assertEqual(scoring["verification_confidence"], 0)

    def test_fetched_description_earns_confidence(self):
        """Successfully fetching the description from the canonical URL is evidence
        the listing is real and live, so it must clear the >=80 gate."""
        scoring = pipeline_v2.compute_opportunity_score({
            "title": "Data Engineer", "company": "Acme",
            "source_verification_status": "description_fetched",
        })
        self.assertGreaterEqual(scoring["verification_confidence"], 80)

    def test_fetched_description_can_pass_the_advance_gate(self):
        """The exact condition pipeline_v2 uses to allow verified_active."""
        scoring = pipeline_v2.compute_opportunity_score({
            "title": "Data Engineer", "company": "Acme",
            "source_verification_status": "description_fetched",
            "freshness_status": "recent",
        })
        self.assertTrue(
            scoring["verification_confidence"] >= 80
            and scoring["freshness_status"] in {"active", "recent"}
        )

    def test_official_source_still_outranks_a_fetch(self):
        """A canonical/official source remains stronger evidence than a mere fetch."""
        fetched = pipeline_v2.compute_opportunity_score({
            "title": "X", "company": "Y",
            "source_verification_status": "description_fetched"})["verification_confidence"]
        official = pipeline_v2.compute_opportunity_score({
            "title": "X", "company": "Y",
            "source_verification_status": "verified_official_source"})["verification_confidence"]
        self.assertGreater(official, fetched)


if __name__ == "__main__":
    unittest.main()
