import json
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pipeline_v2
import recruiter_agent

KNOWLEDGE = {
    "evidence_linked_skills": {
        "Python": {"strongest_status": "verified_from_local_repository", "sources": ["casamotion"]},
        "Kafka": {"strongest_status": "verified_from_local_repository", "sources": ["casamotion"]},
        "FastAPI": {"strongest_status": "verified_from_local_repository", "sources": ["litflow"]},
    },
    "safe_keyword_synonyms": {
        "equivalents": [
            {"canonical": "Kafka", "safe_terms": ["Apache Kafka"]},
        ]
    },
}


class RecruiterAgentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "test.sqlite3"
        pipeline_v2.create_schema(self.db_path)
        self.knowledge_path = self.root / "knowledge.yaml"
        import yaml

        self.knowledge_path.write_text(yaml.safe_dump(KNOWLEDGE), encoding="utf-8")
        self.evidence_path = self.root / "evidence_register.yaml"
        self.evidence_path.write_text("version: 1\n", encoding="utf-8")

    def _insert_opportunity(self, opportunity_id="opp_" + "a" * 24, description="Python and Apache Kafka role"):
        now = datetime.now(timezone.utc).isoformat()
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            connection.execute(
                """INSERT INTO opportunities(
                       id, title, company, location, url, source, role_kind, role_family,
                       description, requirements, fit_score, eligibility_status,
                       freshness_status, verification_confidence, priority_score,
                       score_schema_version, score_breakdown_json, match_score, status,
                       source_json, created_at, updated_at
                   ) VALUES (?, 'Data Engineer', 'Acme', 'Remote', 'https://a.test/1', 'test',
                             'exact_vacancy', 'data', ?, '', 80, 'eligible', 'active', 90, 80,
                             2, '{"fit_score": 80}', 80, 'shortlisted', '{}', ?, ?)""",
                (opportunity_id, description, now, now),
            )
            connection.commit()
        return opportunity_id, now

    def _insert_artifact(self, opportunity_id, cv_text, manifest=None, name="cv_test"):
        pdf = self.root / f"{name}.pdf"
        pdf.write_bytes(b"%PDF-1.4 test")
        pdf.with_suffix(".txt").write_text(cv_text, encoding="utf-8")
        if manifest is not None:
            pdf.with_suffix(".manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        artifact_id = pipeline_v2.stable_id("cv", opportunity_id, "tailored", pdf.name)
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO cv_artifacts(id, opportunity_id, path, label, artifact_type) VALUES (?, ?, ?, '', 'tailored')",
                (artifact_id, opportunity_id, pdf.name),
            )
            connection.commit()
        return artifact_id

    def _run(self, opportunity_id, version):
        return recruiter_agent.run_review(
            self.db_path,
            {"opportunity_id": opportunity_id, "version": version},
            root=self.root,
            knowledge_path=self.knowledge_path,
            evidence_register_path=self.evidence_path,
        )

    GOOD_CV = (
        "JORDAN RIVERA\n"
        "you@example.com +351 912345678\n"
        "Data Engineer at Acme target. Python, Kafka, FastAPI pipelines.\n"
    )

    def test_migration_v5_creates_recruiter_reviews_table_idempotently(self):
        pipeline_v2.create_schema(self.db_path)  # second run must be safe
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("recruiter_reviews", tables)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], pipeline_v2.MIGRATION_VERSION)
        self.assertGreaterEqual(pipeline_v2.MIGRATION_VERSION, 5)

    def test_ready_to_send_review_with_strengths_and_ats_score(self):
        opp_id, version = self._insert_opportunity()
        self._insert_artifact(opp_id, self.GOOD_CV, manifest={"page_count": 1, "layout": "morocco_photo_one_page"})
        result = self._run(opp_id, version)
        review = result["review"]
        self.assertEqual(review["recommendation"], "ready_to_send")
        self.assertEqual(result["recommendation"], "ready_to_send")
        self.assertEqual(review["ats_keyword_coverage_percent"], 100.0)
        self.assertTrue(review["strengths"])
        self.assertEqual(review["red_flags"], [])
        self.assertIn("Review only", review["truthfulness_policy"])

    def test_two_pages_without_exception_is_red_flag_and_regenerate(self):
        opp_id, version = self._insert_opportunity()
        self._insert_artifact(opp_id, self.GOOD_CV, manifest={"page_count": 2, "layout": "international_one_page"})
        review = self._run(opp_id, version)["review"]
        self.assertTrue(any("without an approved" in flag for flag in review["red_flags"]))
        self.assertEqual(review["recommendation"], "regenerate")

    def test_two_pages_with_approved_exception_is_not_flagged(self):
        opp_id, version = self._insert_opportunity()
        self._insert_artifact(
            opp_id, self.GOOD_CV,
            manifest={"page_count": 2, "layout": "international_two_page_approved_exception"},
        )
        review = self._run(opp_id, version)["review"]
        self.assertFalse(any("without an approved" in flag for flag in review["red_flags"]))

    def test_missing_contact_info_is_red_flag(self):
        opp_id, version = self._insert_opportunity()
        self._insert_artifact(opp_id, "Python Kafka FastAPI, Acme, no contact block here")
        review = self._run(opp_id, version)["review"]
        self.assertTrue(any("Missing contact info" in flag for flag in review["red_flags"]))
        self.assertEqual(review["recommendation"], "needs_edits")

    def test_unbacked_claims_are_red_flag_and_regenerate(self):
        opp_id, version = self._insert_opportunity()
        self._insert_artifact(opp_id, self.GOOD_CV + "\nExpert in Kubernetes and Terraform.")
        review = self._run(opp_id, version)["review"]
        self.assertTrue(any("Claims not backed" in flag for flag in review["red_flags"]))
        self.assertEqual(review["recommendation"], "regenerate")
        # Truthfulness: actions must never suggest adding unbacked skills.
        for action in review["improvement_actions"]:
            self.assertNotIn("Add 'Kubernetes'", action)

    def test_gaps_report_unevidenced_vacancy_requirements(self):
        opp_id, version = self._insert_opportunity(
            description="Python, Apache Kafka and Kubernetes and Snowflake required"
        )
        self._insert_artifact(opp_id, self.GOOD_CV)
        review = self._run(opp_id, version)["review"]
        self.assertTrue(any("Kubernetes" in gap for gap in review["gaps"]))
        self.assertTrue(any("Do NOT add 'Kubernetes'" in a for a in review["improvement_actions"]))
        self.assertLess(review["ats_keyword_coverage_percent"], 100.0)
        self.assertEqual(review["recommendation"], "needs_edits")

    def test_version_rules_and_validation(self):
        opp_id, version = self._insert_opportunity()
        self._insert_artifact(opp_id, self.GOOD_CV)
        with self.assertRaises(pipeline_v2.ValidationError):
            self._run(opp_id, "")
        with self.assertRaises(pipeline_v2.ConflictError):
            self._run(opp_id, "stale-version")
        with self.assertRaises(pipeline_v2.NotFoundError):
            self._run("opp_" + "f" * 24, version)
        with self.assertRaises(pipeline_v2.ValidationError):
            recruiter_agent.run_review(self.db_path, {"opportunity_id": opp_id, "version": version, "send": True})

    def test_review_persistence_is_one_current_row_per_pair(self):
        opp_id, version = self._insert_opportunity()
        self._insert_artifact(opp_id, self.GOOD_CV)
        first = self._run(opp_id, version)
        second = self._run(opp_id, version)
        self.assertEqual(first["id"], second["id"])
        rows = recruiter_agent.list_reviews(self.db_path)
        self.assertEqual(len(rows), 1)
        by_opp = recruiter_agent.reviews_for_opportunity(self.db_path, opp_id)
        self.assertEqual(len(by_opp), 1)
        with self.assertRaises(pipeline_v2.NotFoundError):
            recruiter_agent.reviews_for_opportunity(self.db_path, "opp_" + "e" * 24)

    def test_http_endpoints_review_only_same_origin(self):
        opp_id, version = self._insert_opportunity()
        self._insert_artifact(opp_id, self.GOOD_CV, manifest={"page_count": 1})
        server = pipeline_v2.make_server(self.db_path, self.root, port=0)
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        def request(path, method="GET", payload=None, origin=None):
            headers = {"Content-Type": "application/json"}
            if origin:
                headers["Origin"] = origin
            data = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())

        # Cross-origin POST is forbidden.
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request("/api/recruiter/review", "POST",
                    {"opportunity_id": opp_id, "version": version}, origin="https://evil.example")
        self.assertEqual(ctx.exception.code, 403)

        status, body = request(
            "/api/recruiter/review", "POST",
            {"opportunity_id": opp_id, "version": version}, origin=f"http://127.0.0.1:{server.server_port}",
        )
        self.assertEqual(status, 201)
        self.assertIn(body["recommendation"], ("ready_to_send", "needs_edits", "regenerate"))

        status, rows = request("/api/recruiter/reviews")
        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 1)

        status, rows = request(f"/api/recruiter/reviews/{opp_id}")
        self.assertEqual(status, 200)
        self.assertEqual(rows[0]["opportunity_id"], opp_id)

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request("/api/recruiter/reviews/opp_" + "9" * 24)
        self.assertEqual(ctx.exception.code, 404)

        # Stale version -> 409.
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request("/api/recruiter/review", "POST",
                    {"opportunity_id": opp_id, "version": "stale"})
        self.assertEqual(ctx.exception.code, 409)

    def test_no_send_or_apply_capability_exposed(self):
        exported = {name for name in dir(recruiter_agent) if not name.startswith("_")}
        self.assertFalse({"send_application", "apply", "send_cv", "submit_application"} & exported)


if __name__ == "__main__":
    unittest.main()
