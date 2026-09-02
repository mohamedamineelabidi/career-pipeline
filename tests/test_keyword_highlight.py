import unittest
from contextlib import closing

import keyword_highlight
import pipeline_v2
from pipeline_v2 import NotFoundError
from resume_matcher_fixtures import INVENTED, JD_EN, OPP_ID, PortTestCase

CV_TEXT = "TEST CANDIDATE\nData Engineer. Python and Kafka streaming pipeline; FastAPI services. Data pipelines."


class KeywordHighlightTests(PortTestCase):
    def test_highlight_classifies_terms(self):
        opp_id, _ = self.insert_opportunity()
        self.insert_artifact(opp_id, CV_TEXT)
        result = keyword_highlight.highlight(self.db_path, opp_id, **self.sources)
        by_term = {row["term"]: row for row in result["jd_keywords"]}
        self.assertTrue(by_term["Python"]["in_cv"])
        self.assertEqual(by_term["Python"]["category"], "language")
        self.assertGreaterEqual(by_term["Kafka"]["count_jd"], 2)
        self.assertGreater(by_term["Kafka"]["count_cv"], 0)
        # Docker: in JD, evidenced via tailoring_knowledge, absent from CV -> actionable.
        self.assertIn("Docker", result["missing_but_evidenced"])
        # Kubernetes / Terraform: in JD, no evidence -> never to be added.
        self.assertIn("Kubernetes", result["missing_unevidenced"])
        self.assertIn("Terraform", result["missing_unevidenced"])
        self.assertNotIn("Kubernetes", result["missing_but_evidenced"])
        self.assertTrue(0 < result["cv_coverage_pct"] < 100)
        # Noun phrase proxy: "quality checks" is repeated in the JD, not in the CV.
        self.assertIn("quality checks", by_term)
        self.assertEqual(by_term["quality checks"]["category"], "phrase")
        self.assertFalse(by_term["quality checks"]["in_cv"])
        self.assertIn("quality checks", result["missing_unevidenced"])
        # Invented term never becomes evidenced.
        self.assertNotIn(INVENTED, result["missing_but_evidenced"])

    def test_404_without_cv_or_description(self):
        opp_id, _ = self.insert_opportunity()
        with self.assertRaises(NotFoundError):
            keyword_highlight.highlight(self.db_path, opp_id, **self.sources)
        other, _ = self.insert_opportunity(opportunity_id="opp_" + "b" * 24, description="")
        self.insert_artifact(other, CV_TEXT, name="cv_b")
        with self.assertRaises(NotFoundError):
            keyword_highlight.highlight(self.db_path, other, **self.sources)
        with self.assertRaises(NotFoundError):
            keyword_highlight.highlight(self.db_path, "opp_" + "c" * 24, **self.sources)

    def test_rejected_evidence_is_excluded_from_profile(self):
        profile = keyword_highlight.evidence_profile(
            self.master_path, self.evidence_path, self.knowledge_path)
        self.assertEqual(keyword_highlight.citations_for(profile, "Airflow"), [])
        self.assertTrue(keyword_highlight.citations_for(profile, "Kafka"))
        self.assertNotIn("Secret rejected", profile["text"])

    def test_http_endpoint_routes_and_404(self):
        opp_id, _ = self.insert_opportunity()
        self.insert_artifact(opp_id, CV_TEXT)
        self.start_server()
        status, body = self.request(f"/api/cvs/{opp_id}/highlight")
        self.assertEqual(status, 200)
        self.assertIn("jd_keywords", body)
        self.assertIn("cv_coverage_pct", body)
        status, _ = self.request("/api/cvs/opp_" + "z" * 24 + "/highlight")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
