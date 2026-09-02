import unittest
from contextlib import closing

import cover_letter
import pipeline_v2
from pipeline_v2 import ConflictError, ValidationError
from resume_matcher_fixtures import INVENTED, JD_FR, PortTestCase


class CoverLetterTests(PortTestCase):
    def _generate(self, opp_id, version, **extra):
        payload = {"opportunity_id": opp_id, "version": version, **extra}
        return cover_letter.generate(self.db_path, payload, **self.sources)

    def test_english_draft_is_evidence_only_and_linted(self):
        opp_id, version = self.insert_opportunity()
        record = self._generate(opp_id, version)
        body = record["body"]
        self.assertEqual(record["language"], "en")
        self.assertEqual(record["status"], "draft_local")
        self.assertTrue(record["is_draft"])
        self.assertLessEqual(len(body.split()), 250)
        self.assertNotIn("\u2014", body)
        self.assertNotIn(INVENTED, body)
        self.assertNotIn("Kubernetes", body)  # unevidenced JD skill never claimed
        self.assertNotIn("Airflow", body)  # rejected evidence never used
        self.assertIn("Kafka", body)
        self.assertIn("Acme Corp", body)
        self.assertTrue(record["evidence_ids"])
        self.assertTrue(all(e.startswith(("experience.", "projects.", "leadership")) for e in record["evidence_ids"]))
        self.assertIn("never sends", record["send_policy"])
        listed = cover_letter.list_drafts(self.db_path, opp_id)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["drafts"][0]["id"], record["id"])

    def test_french_auto_detected(self):
        opp_id, version = self.insert_opportunity(description=JD_FR)
        record = self._generate(opp_id, version)
        self.assertEqual(record["language"], "fr")
        self.assertIn("Madame, Monsieur", record["body"])
        self.assertNotIn("Kubernetes", record["body"])
        explicit = self._generate(opp_id, version, language="en")
        self.assertEqual(explicit["language"], "en")

    def test_errors(self):
        opp_id, version = self.insert_opportunity()
        with self.assertRaises(ConflictError):
            self._generate(opp_id, "stale")
        with self.assertRaises(ValidationError):
            self._generate(opp_id, version, language="de")
        empty, v2 = self.insert_opportunity(opportunity_id="opp_" + "b" * 24, description="")
        with self.assertRaises(ValidationError):
            self._generate(empty, v2)

    def test_http_endpoints(self):
        opp_id, version = self.insert_opportunity()
        self.start_server()
        status, _ = self.request("/api/cover-letters/generate", "POST",
                                 {"opportunity_id": opp_id, "version": version}, origin="https://evil.example")
        self.assertEqual(status, 403)
        status, _ = self.request("/api/cover-letters/generate", "POST",
                                 {"opportunity_id": opp_id, "version": "stale"})
        self.assertEqual(status, 409)
        status, body = self.request("/api/cover-letters/generate", "POST",
                                    {"opportunity_id": opp_id, "version": version})
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "draft_local")
        status, body = self.request(f"/api/cover-letters?opportunity_id={opp_id}")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        status, body = self.request("/api/cover-letters")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)


if __name__ == "__main__":
    unittest.main()
