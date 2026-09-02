import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

import pipeline_v2
import semantic_match

os.environ["SEMANTIC_MATCH_BACKEND"] = "tfidf"

CAREER_MASTER = {
    "targets": {"primary_identity": "AI & Data Engineer", "headline": "GenAI Agents, Cloud"},
    "summary_evidence": ["Builds AI agents, RAG applications and data services with Python and FastAPI."],
    "skills": {
        "ai_and_agents": ["RAG", "LangGraph", "LangChain"],
        "data_and_ml": ["Python", "SQL", "Spark", "Kafka"],
        "cloud_and_delivery": ["Docker", "Azure"],
    },
    "experience": [
        {
            "id": "exp1", "company": "Orange", "title": "Applied AI Engineer", "status": "verified",
            "bullets": [
                {"statement": "Built a RAG assistant with FastAPI and PostgreSQL.", "evidence_status": "verified",
                 "technologies": ["FastAPI", "PostgreSQL"]},
                {"statement": "Trained Kubernetes-based Terraform platform.", "evidence_status": "unconfirmed",
                 "technologies": ["Terraform"]},
            ],
        },
        {"id": "exp_bad", "company": "Ghost", "title": "Snowflake Architect", "status": "excluded_until_confirmed",
         "bullets": [{"statement": "Snowflake dbt", "evidence_status": "verified", "technologies": ["Snowflake", "dbt"]}]},
    ],
    "projects": [],
    "education": [{"degree": "Engineering Degree", "field": "Data Engineering", "institution": "ENSAH"}],
    "certifications": [{"name": "AWS Certified Cloud Practitioner", "status": "user_confirmed"}],
    "languages": [{"name": "English", "level": "C1"}],
}

LONG_JD = (
    "We are hiring a Data & AI Engineer to build RAG pipelines with LangChain and LangGraph, "
    "deploy services with FastAPI and Docker on Azure, orchestrate ETL with Airflow and dbt, "
    "model data in Snowflake and use PyTorch for deep learning. Strong SQL and Python required. "
    "Familiarity with Kubernetes and Terraform is a plus."
)
SHORT_JD = "Short posting."


class SemanticMatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / "pipeline.sqlite3"
        pipeline_v2.create_schema(self.db)
        import yaml

        self.master = self.root / "career_master.yaml"
        self.master.write_text(yaml.safe_dump(CAREER_MASTER), encoding="utf-8")
        self.evidence = self.root / "evidence_register.yaml"
        self.evidence.write_text("version: 1\nrepository_evidence: {}\n", encoding="utf-8")
        self.kw = dict(career_master_path=self.master, evidence_register_path=self.evidence, backend="tfidf")
        self.tax = semantic_match.taxonomy()

    def _insert(self, opp_id, title, company, description, status="eligible"):
        with closing(pipeline_v2.connect(self.db)) as connection:
            connection.execute(
                """INSERT INTO opportunities(id, title, company, location, url, source, status, role_kind,
                   role_family, description, requirements, created_at, updated_at, source_json,
                   fit_score, eligibility_status, freshness_status, verification_confidence,
                   priority_score, score_schema_version, score_breakdown_json, match_score)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 50,'eligible','active',50,50,2,'{}',50)""",
                (opp_id, title, company, "Rabat", f"https://x/{opp_id}", "test", status, "exact_vacancy",
                 "data", description, "", "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z", "{}"),
            )
            connection.commit()

    # (1) taxonomy extraction
    def test_taxonomy_loads_and_extracts_aliases(self):
        self.assertGreaterEqual(len(self.tax.skills), 150)
        found = self.tax.extract("Experience with pyspark, k8s, retrieval-augmented generation and vector db.")
        self.assertIn("Spark", found)
        self.assertIn("Kubernetes", found)
        self.assertIn("RAG", found)
        self.assertIn("Vector Database", found)
        self.assertNotIn("Go", self.tax.extract("we go above and beyond"))
        self.assertNotIn("R", self.tax.extract("for r&d we hire"))

    # (2) have/missing uses only evidence sources
    def test_have_skills_come_only_from_evidence(self):
        profile = semantic_match.build_candidate_profile(self.master, self.evidence)
        have = semantic_match.candidate_skill_names(profile, self.tax)
        for skill in ("Python", "RAG", "LangGraph", "FastAPI", "Docker", "Azure", "AWS"):
            self.assertIn(skill, have)
        # unconfirmed bullet and excluded experience must not leak in
        for skill in ("Terraform", "Snowflake", "dbt", "Kubernetes", "PyTorch", "Airflow"):
            self.assertNotIn(skill, have)
        jd_have, jd_missing = semantic_match.gap_analysis(LONG_JD, have, self.tax)
        self.assertIn("LangChain", jd_have)
        self.assertIn("PyTorch", jd_missing)
        self.assertIn("Snowflake", jd_missing)
        self.assertFalse(set(jd_have) & set(jd_missing))

    # (3) score bounds + short descriptions skipped
    def test_scores_bounded_and_short_skipped(self):
        self._insert("opp_" + "a" * 24, "Data & AI Engineer", "Acme", LONG_JD)
        self._insert("opp_" + "b" * 24, "Dev", "Acme", SHORT_JD)
        result = semantic_match.recompute(self.db, all_opportunities=True, **self.kw)
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["skipped_short_description"], 1)
        self.assertEqual(result["model"], "tfidf-fallback")
        detail = semantic_match.match_detail(self.db, "opp_" + "a" * 24)
        self.assertTrue(0 <= detail["semantic_score"] <= 100)
        self.assertGreater(detail["semantic_score"], 0)
        self.assertEqual(detail["status"], "computed")
        self.assertEqual(semantic_match.match_detail(self.db, "opp_" + "b" * 24)["status"], "not_computed")
        for sim, expected in ((-0.5, 0), (0.0, 0), (0.5, 50), (1.7, 100), (float("nan"), 0)):
            self.assertEqual(semantic_match.similarity_to_score(sim), expected)

    # (4) recompute idempotency by hash
    def test_recompute_idempotent_until_description_changes(self):
        opp = "opp_" + "c" * 24
        self._insert(opp, "AI Engineer", "Acme", LONG_JD)
        first = semantic_match.recompute(self.db, opportunity_id=opp, **self.kw)
        self.assertEqual(first["computed"], 1)
        computed_at = first["results"][0]["computed_at"]
        second = semantic_match.recompute(self.db, opportunity_id=opp, **self.kw)
        self.assertEqual((second["computed"], second["unchanged"]), (0, 1))
        self.assertEqual(second["results"][0]["computed_at"], computed_at)
        with closing(pipeline_v2.connect(self.db)) as connection:
            connection.execute("UPDATE opportunities SET description=? WHERE id=?", (LONG_JD + " Also Kafka streaming.", opp))
            connection.commit()
        third = semantic_match.recompute(self.db, opportunity_id=opp, **self.kw)
        self.assertEqual(third["computed"], 1)
        self.assertNotEqual(third["results"][0]["content_hash"], first["results"][0]["content_hash"])
        forced = semantic_match.recompute(self.db, opportunity_id=opp, force=True, **self.kw)
        self.assertEqual(forced["computed"], 1)

    # (5) FTS search kept in sync by triggers
    def test_fts_search_ranked_with_snippets_and_synced(self):
        self._insert("opp_" + "d" * 24, "Senior LangGraph Engineer", "Acme", LONG_JD)
        self._insert("opp_" + "e" * 24, "Accountant", "Beta", "Bookkeeping and invoices, nothing technical here at all really.")
        result = semantic_match.search(self.db, "langgraph")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["id"], "opp_" + "d" * 24)
        self.assertIn("[", result["results"][0]["snippet"])
        with closing(pipeline_v2.connect(self.db)) as connection:
            connection.execute("UPDATE opportunities SET description='Now about Snowflake only, long enough text to be indexed properly here.' WHERE id=?", ("opp_" + "e" * 24,))
            connection.execute("DELETE FROM opportunities WHERE id=?", ("opp_" + "d" * 24,))
            connection.commit()
        self.assertEqual(semantic_match.search(self.db, "snowflake")["results"][0]["id"], "opp_" + "e" * 24)
        self.assertEqual(semantic_match.search(self.db, "langgraph")["count"], 0)
        # raw punctuation must not raise FTS syntax errors
        raw = semantic_match.search(self.db, 'snow"flake OR (x')
        self.assertIsInstance(raw["count"], int)
        self.assertEqual(semantic_match.search(self.db, 'snow"flake')["count"], 0)
        self.assertEqual(semantic_match.search(self.db, "Snowflake!!")["count"], 1)
        with self.assertRaises(pipeline_v2.ValidationError):
            semantic_match.search(self.db, "   ")

    def test_gaps_aggregate_open_only(self):
        self._insert("opp_" + "f" * 24, "AI", "A", LONG_JD)
        self._insert("opp_" + "g" * 24, "AI", "B", LONG_JD, status="closed")
        semantic_match.recompute(self.db, all_opportunities=True, **self.kw)
        gaps = semantic_match.skill_gaps(self.db, limit=5)
        self.assertEqual(gaps["opportunities_considered"], 1)
        self.assertEqual(len(gaps["top_missing"]), 5)
        self.assertIn("PyTorch", {g["skill"] for g in gaps["top_missing"]})
        self.assertEqual(semantic_match.skill_gaps(self.db, open_only=False)["opportunities_considered"], 2)


class SemanticMatchApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.db = root / "pipeline.sqlite3"
        pipeline_v2.create_schema(self.db)
        (root / "pipeline_v2.html").write_text("<html></html>", encoding="utf-8")
        self.server = pipeline_v2.make_server(self.db, root, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        with closing(pipeline_v2.connect(self.db)) as connection:
            connection.execute(
                """INSERT INTO opportunities(id, title, company, location, url, source, status, role_kind,
                   role_family, description, requirements, created_at, updated_at, source_json,
                   fit_score, eligibility_status, freshness_status, verification_confidence,
                   priority_score, score_schema_version, score_breakdown_json, match_score)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 50,'eligible','active',50,50,2,'{}',50)""",
                ("opp_" + "1" * 24, "Data Engineer", "Acme", "Rabat", "https://x/1", "test", "eligible", "exact_vacancy",
                 "data", LONG_JD, "", "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z", "{}"),
            )
            connection.commit()

    def _request(self, path, method="GET", body=None, headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_endpoints_and_cross_origin_forbidden(self):
        status, body = self._request(
            "/api/match/recompute", "POST", {"all": True}, headers={"Origin": "http://evil.example"}
        )
        self.assertEqual(status, 403)
        status, body = self._request("/api/match/recompute", "POST", {"all": True},
                                     headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        self.assertEqual(body["computed"], 1)
        status, body = self._request("/api/match/recompute", "POST", {})
        self.assertEqual(status, 400)
        status, detail = self._request("/api/match/opp_" + "1" * 24)
        self.assertEqual(status, 200)
        for key in ("semantic_score", "model", "skills_have", "skills_missing", "computed_at"):
            self.assertIn(key, detail)
        self.assertEqual(self._request("/api/match/opp_" + "0" * 24)[0], 404)
        status, rows = self._request("/api/opportunities")
        self.assertEqual(rows[0]["semantic_score"], detail["semantic_score"])
        self.assertEqual(rows[0]["skills_missing_count"], len(detail["skills_missing"]))
        status, gaps = self._request("/api/match/gaps?limit=3")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(gaps["top_missing"]), 3)
        status, found = self._request("/api/search?q=airflow%20dbt")
        self.assertEqual(status, 200)
        self.assertEqual(found["results"][0]["id"], "opp_" + "1" * 24)
        self.assertEqual(self._request("/api/search")[0], 400)


if __name__ == "__main__":
    unittest.main()
