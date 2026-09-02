import json
import unittest
from contextlib import closing

import interview_prep
import pipeline_v2
from pipeline_v2 import ConflictError, NotFoundError, ValidationError
from resume_matcher_fixtures import INVENTED, PortTestCase

CV_TEXT = "TEST CANDIDATE\nData Engineer. Python and Kafka streaming pipeline; FastAPI services."


class InterviewPrepTests(PortTestCase):
    def _generate(self, opp_id, version):
        return interview_prep.generate(
            self.db_path, {"opportunity_id": opp_id, "version": version}, **self.sources)

    def test_generate_is_grounded_and_persisted(self):
        opp_id, version = self.insert_opportunity()
        self.insert_artifact(opp_id, CV_TEXT)
        record = self._generate(opp_id, version)
        prep = record["prep"]
        kinds = {q["kind"] for q in prep["likely_questions"]}
        self.assertEqual(kinds, {"technical", "behavioural", "gap"})
        technical = [q for q in prep["likely_questions"] if q["kind"] == "technical"]
        self.assertTrue(all(q["evidence"] for q in technical))
        self.assertIn("Python", [q["skill"] for q in technical])
        gaps = [q for q in prep["likely_questions"] if q["kind"] == "gap"]
        self.assertIn("Kubernetes", [q["skill"] for q in gaps])
        self.assertTrue(all("Do not claim" in q["honest_answer_note"] for q in gaps))
        self.assertTrue(prep["talking_points"])
        self.assertTrue(all(tp["evidence"] for tp in prep["talking_points"]))
        self.assertTrue(prep["questions_to_ask_them"])
        # Evidence-only guard: invented JD term never appears as a claim / talking point.
        dumped = json.dumps(prep["talking_points"]) + json.dumps(
            [q for q in prep["likely_questions"] if q["kind"] != "gap"])
        self.assertNotIn(INVENTED, dumped)
        self.assertNotIn("Airflow", dumped)  # rejected evidence excluded
        # Persisted with PK upsert.
        stored = interview_prep.get_prep(self.db_path, opp_id)
        self.assertEqual(stored["prep"]["opportunity_id"], opp_id)
        self._generate(opp_id, version)
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM interview_preps").fetchone()[0]
        self.assertEqual(count, 1)

    def test_errors(self):
        opp_id, version = self.insert_opportunity()
        with self.assertRaises(NotFoundError):
            interview_prep.get_prep(self.db_path, opp_id)
        with self.assertRaises(ConflictError):
            self._generate(opp_id, "stale")
        with self.assertRaises(ValidationError):
            interview_prep.generate(self.db_path, {"opportunity_id": opp_id}, **self.sources)
        with self.assertRaises(NotFoundError):
            self._generate("opp_" + "x" * 24, version)

    def test_http_endpoints_and_cross_origin(self):
        opp_id, version = self.insert_opportunity()
        self.start_server()
        status, _ = self.request(f"/api/interview/{opp_id}")
        self.assertEqual(status, 404)
        status, _ = self.request("/api/interview/generate", "POST",
                                 {"opportunity_id": opp_id, "version": version}, origin="https://evil.example")
        self.assertEqual(status, 403)
        status, _ = self.request("/api/interview/generate", "POST",
                                 {"opportunity_id": opp_id, "version": "stale"})
        self.assertEqual(status, 409)
        status, body = self.request("/api/interview/generate", "POST",
                                    {"opportunity_id": opp_id, "version": version})
        self.assertEqual(status, 201)
        self.assertIn("likely_questions", body["prep"])
        status, body = self.request(f"/api/interview/{opp_id}")
        self.assertEqual(status, 200)
        self.assertEqual(body["opportunity_id"], opp_id)


if __name__ == "__main__":
    unittest.main()
