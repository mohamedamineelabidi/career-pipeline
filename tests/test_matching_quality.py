"""Workstream C tests: fuzzy aliases, score breakdown, LLM rubric, section coverage."""

import json
import unittest
from contextlib import closing
from unittest import mock

import keyword_highlight
import llm_client
import llm_scoring
import pipeline_v2
import recruiter_agent
import semantic_match
from pipeline_v2 import ConflictError, NotFoundError, ValidationError
from resume_matcher_fixtures import CAREER_MASTER, INVENTED, JD_EN, PortTestCase

LONG_JD = (
    "Data Engineer (junior welcome). We are looking for a Data Engineer with strong Python and "
    "PostgreSQL skills. You will build data pipelines with Apache Kafka and Docker, and deploy "
    "Kubernetes workloads. Experience with Terraform is a plus. 1-2 years of experience. "
    f"Knowledge of {INVENTED} is required. " * 3
)


class FuzzyAliasTests(unittest.TestCase):
    def test_postgresql_in_jd_matches_postgres_in_cv(self):
        self.assertEqual(keyword_highlight.count_term("Postgres and SQL", "PostgreSQL"), 1)
        self.assertEqual(keyword_highlight.count_term("We use PostgreSQL daily", "Postgres"), 1)
        self.assertEqual(keyword_highlight.count_term("k8s and Kubernetes", "Kubernetes"), 2)
        self.assertEqual(keyword_highlight.count_term("Google Cloud and GCP", "GCP"), 2)

    def test_rapidfuzz_single_token_ratio_92(self):
        self.assertEqual(keyword_highlight.count_term("Kubernets cluster", "Kubernetes"), 1)
        self.assertEqual(keyword_highlight.count_term("Kubernets cluster", "Kubernetes", fuzzy=False), 0)
        # Far tokens never match; Java is not JavaScript, Scala is not scikit-learn.
        self.assertEqual(keyword_highlight.count_term("Java developer", "JavaScript"), 0)
        self.assertEqual(keyword_highlight.count_term("Scala code", "scikit-learn"), 0)

    def test_every_taxonomy_skill_has_aliases(self):
        tax = semantic_match.taxonomy()
        self.assertTrue(all(skill.get("aliases") for skill in tax.skills))
        by = {s["name"]: [a.casefold() for a in s["aliases"]] for s in tax.skills}
        self.assertIn("postgres", by["PostgreSQL"])
        self.assertIn("k8s", by["Kubernetes"])
        self.assertIn("large language models", by["LLMs"])
        self.assertIn("retrieval augmented generation", by["RAG"])
        self.assertIn("amazon web services", by["AWS"])


class BreakdownTests(PortTestCase):
    def test_explain_deterministic_and_exposed(self):
        opp_id, _ = self.insert_opportunity(description=LONG_JD)
        kw = dict(career_master_path=self.master_path, evidence_register_path=self.evidence_path)
        first = semantic_match.explain(self.db_path, opp_id, **kw)
        second = semantic_match.explain(self.db_path, opp_id, **kw)
        self.assertEqual(first, second)
        for key in ("semantic", "hard_skills_pct", "title_similarity", "seniority_fit", "language_fit", "total"):
            self.assertIn(key, first)
            self.assertTrue(0 <= first[key] <= 100)
        self.assertEqual(first["semantic"], 0)  # not computed yet
        self.assertGreater(first["hard_skills_pct"], 0)
        self.assertEqual(first["required_years"], 1)
        self.assertGreaterEqual(first["seniority_fit"], 90)
        self.assertEqual(first["title_similarity"], 100)  # "Data Engineer" == experience title
        detail = semantic_match.match_detail(self.db_path, opp_id)
        self.assertIn("breakdown", detail)
        self.assertEqual(detail["breakdown"]["method"], "cv_matcher_breakdown_v1")

    def test_senior_years_penalised(self):
        self.assertLess(semantic_match.seniority_fit_score("Senior Data Engineer, 7+ years of experience"), 30)
        self.assertEqual(semantic_match.required_years("5 to 8 ans d'expérience"), 5)
        self.assertIsNone(semantic_match.required_years("no figures here"))

    def test_http_match_has_breakdown(self):
        opp_id, _ = self.insert_opportunity(description=LONG_JD)
        base = self.start_server()
        _status, body = self.request(f"/api/match/{opp_id}")
        self.assertIn("total", body["breakdown"])


FAKE_LLM = {
    "fit": 72.4, "reasons": ["Python and Kafka match", "no Kubernetes", "third", "fourth"],
    "missing_skills": ["Kubernetes", "Terraform", INVENTED, "Python", "Snowflake"],
    "matching_skills": ["Python", "Kafka", "Kubernetes", "Rust"],
    "seniority_ok": True, "red_flags": ["Zorblaxium required"],
}


class LLMScoringTests(PortTestCase):
    def setUp(self):
        super().setUp()
        self.calls = []

        def fake_chat_json(messages, **kwargs):
            self.calls.append(messages)
            return json.loads(json.dumps(FAKE_LLM))

        patches = [
            mock.patch.object(llm_client, "chat_json", fake_chat_json),
            mock.patch.object(llm_client, "llm_available", lambda: True),
            mock.patch.object(llm_client, "model_name", lambda: "fake-model"),
            mock.patch.object(llm_scoring, "evidence_text", lambda: "Python Kafka FastAPI PostgreSQL RAG Qdrant"),
            mock.patch.object(llm_scoring, "SLEEP_BETWEEN_CALLS", 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_migration_v9_table_exists(self):
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("llm_scores", tables)

    def test_score_validates_lists_and_persists(self):
        opp_id, _ = self.insert_opportunity(description=JD_EN)
        with self.assertRaises(NotFoundError):
            llm_scoring.get_score(self.db_path, opp_id)
        result = llm_scoring.score_opportunity(self.db_path, opp_id)
        self.assertEqual(result["fit"], 72)
        self.assertEqual(len(result["reasons"]), 3)
        # missing must be in JD and not in evidence; Python is in evidence, Snowflake not in JD.
        self.assertEqual(result["missing_skills"], ["Kubernetes", "Terraform", INVENTED])
        # matching must be in both; Kubernetes/Rust are not in evidence.
        self.assertEqual(result["matching_skills"], ["Python", "Kafka"])
        self.assertIn("Snowflake", result["dropped_unverified_terms"])
        self.assertTrue(result["seniority_ok"])
        self.assertEqual(result["model"], "fake-model")
        prompt = self.calls[0][0]["content"]
        self.assertIn("JOB DESCRIPTION", prompt)
        self.assertNotIn("you", prompt)
        stored = llm_scoring.get_score(self.db_path, opp_id)
        self.assertEqual(stored["fit"], 72)
        rows = pipeline_v2.api_data(self.db_path, "opportunities")
        self.assertEqual(rows[0]["llm_fit"], 72)

    def test_score_all_only_missing_and_min_length(self):
        long_id, _ = self.insert_opportunity(description=JD_EN)
        self.insert_opportunity(opportunity_id="opp_" + "b" * 24, description="short")
        summary = llm_scoring.score_all(self.db_path, limit=10, min_description_chars=300)
        self.assertEqual(summary["scored"], 1)
        self.assertEqual(summary["fit_distribution"]["buckets"]["60-79"], 1)
        again = llm_scoring.score_all(self.db_path, limit=10, min_description_chars=300)
        self.assertEqual(again["candidates"], 0)

    def test_score_all_stops_on_429(self):
        self.insert_opportunity(description=JD_EN)
        self.insert_opportunity(opportunity_id="opp_" + "c" * 24, description=JD_EN)

        def rate_limited(messages, **kwargs):
            raise llm_client.LLMError("HTTP 429: too many requests")

        with mock.patch.object(llm_client, "chat_json", rate_limited):
            summary = llm_scoring.score_all(self.db_path, limit=10, min_description_chars=100, backoff_seconds=0)
        self.assertEqual(summary["scored"], 0)
        self.assertIn("429", summary["stopped_reason"])

    def test_http_endpoints(self):
        opp_id, version = self.insert_opportunity(description=JD_EN)
        base = self.start_server()
        status, _ = self.request(f"/api/llm-score/{opp_id}")
        self.assertEqual(status, 404)
        status, _ = self.request(f"/api/llm-score/{opp_id}", "POST", {"version": "stale"})
        self.assertEqual(status, 409)
        status, body = self.request(f"/api/llm-score/{opp_id}", "POST", {"version": version})
        self.assertEqual(status, 201)
        self.assertEqual(body["fit"], 72)
        status, body = self.request(f"/api/llm-score/{opp_id}")
        self.assertEqual((status, body["fit"]), (200, 72))
        status, body = self.request("/api/llm-score/recompute", "POST", {"limit": 5})
        self.assertEqual(status, 200)
        self.assertEqual(body["candidates"], 0)
        with mock.patch.object(llm_client, "llm_available", lambda: False):
            status, _ = self.request(f"/api/llm-score/{opp_id}", "POST", {"version": version})
            self.assertEqual(status, 503)
        status, _ = self.request(f"/api/llm-score/{opp_id}", "POST", {"version": version}, origin="http://evil.test")
        self.assertEqual(status, 403)

    def test_unavailable_raises(self):
        opp_id, _ = self.insert_opportunity(description=JD_EN)
        with mock.patch.object(llm_client, "llm_available", lambda: False):
            with self.assertRaises(llm_scoring.LLMUnavailable):
                llm_scoring.score_opportunity(self.db_path, opp_id)


class SectionCoverageTests(unittest.TestCase):
    def test_section_coverage_percentages(self):
        profile = json.loads(json.dumps(CAREER_MASTER))
        profile["data_ai_variant"] = {"headline": "Data Engineer", "summary": "Python pipelines."}
        profile["education"] = [{"institution": "ENSIAS", "degree": "Engineer", "field": "Data"}]
        coverage = recruiter_agent.section_coverage(profile, ["Python", "Kafka", "FastAPI", "Qdrant"])
        self.assertEqual(set(coverage), {"summary", "experience", "projects", "education"})
        self.assertEqual(coverage["summary"], 25.0)
        self.assertEqual(coverage["experience"], 75.0)
        self.assertEqual(coverage["projects"], 25.0)
        self.assertEqual(coverage["education"], 0.0)
        self.assertEqual(recruiter_agent.section_coverage(profile, []), {k: 0.0 for k in coverage})

    def test_propose_edits_targets_weakest_section_first(self):
        profile = json.loads(json.dumps(CAREER_MASTER))
        profile["data_ai_variant"] = {"headline": "Data Engineer", "summary": "Builds pipelines."}
        profile["experience"][0]["bullets"] = profile["experience"][0]["bullets"][:1]
        review = {"requirement_evidence": {"matched_requirements": [{
            "canonical_skill": "Docker", "vacancy_term": "Docker", "evidence_status": "verified",
            "evidence_sources": ["experience.acme_2025.bullet_1", "projects.ragproj.bullet_1"],
        }]}}
        corpus = recruiter_agent.text_tokens("Docker Python Kafka")
        edited, edits = recruiter_agent.propose_edits(profile, {"title": "Data Engineer", "description": "Docker Python Kafka"}, review, corpus)
        surface = [e for e in edits if e["type"] == "surface_skill"]
        self.assertEqual(len(surface), 1)
        coverage = recruiter_agent.propose_edits.last_coverage
        # Only matched JD terms count (Docker): both sections at 0 -> tie resolves to experience,
        # and the single surfaced skill lands in the targeted section.
        self.assertEqual(coverage["before"]["experience"], coverage["before"]["projects"])
        target = coverage["target_section"]
        self.assertEqual(target, "experience")
        self.assertTrue(surface[0]["target"].startswith(target + "."))
        self.assertIn("Docker", edited["experience"][0]["bullets"][0]["technologies"])


if __name__ == "__main__":
    unittest.main()
