"""Tests for analytics.py (read-only insights over the pipeline DB)."""

import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pipeline_v2
import analytics
from tests.resume_matcher_fixtures import PortTestCase, CAREER_MASTER

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


class AnalyticsTests(PortTestCase):
    def _opp(self, oid, title, company, status, days_ago, source="src_a", fit=80, url=None,
             description="Python and Kafka data pipelines with Docker and Kubernetes."):
        created = _iso(days_ago)
        with closing(pipeline_v2.connect(self.db_path)) as c:
            c.execute(
                """INSERT INTO opportunities(id, title, company, location, url, source, role_kind, role_family,
                       description, requirements, fit_score, eligibility_status, freshness_status,
                       verification_confidence, priority_score, score_schema_version, score_breakdown_json,
                       match_score, status, source_json, created_at, updated_at, publication_date)
                   VALUES (?, ?, ?, 'Remote', ?, ?, 'exact_vacancy', 'data', ?, '', ?, 'eligible', 'active',
                           90, ?, 2, '{}', ?, ?, '{}', ?, ?, ?)""",
                (oid, title, company, url or f"https://x.test/{oid}", source, description, fit, fit, fit,
                 status, created, created, created[:10]),
            )
            c.commit()

    def _llm(self, oid, fit):
        with closing(pipeline_v2.connect(self.db_path)) as c:
            c.execute("INSERT INTO llm_scores(opportunity_id, model, fit, payload_json, created_at) VALUES (?, 'm', ?, '{}', ?)",
                      (oid, fit, NOW.isoformat()))
            c.commit()

    def _event(self, oid, frm, to, days_ago):
        with closing(pipeline_v2.connect(self.db_path)) as c:
            c.execute("INSERT INTO lifecycle_events(id, entity_type, entity_id, from_status, to_status, occurred_at, confirmed_by_user)"
                      " VALUES (?, 'opportunity', ?, ?, ?, ?, 1)",
                      (pipeline_v2.stable_id("life", oid, frm or "", to, str(days_ago)), oid, frm, to, _iso(days_ago)))
            c.commit()

    def seed(self):
        self._opp("o1", "Data Engineer", "Acme", "discovered", 30, source="src_a", fit=50)
        self._opp("o2", "Senior Data Engineer (H/F)", "Acme", "verified_active", 2, source="src_a", fit=70)
        self._opp("o3", "ML Engineer", "Beta", "eligible", 10, source="src_b", fit=60, description="")
        self._opp("o4", "AI Engineer", "Beta", "shortlisted", 5, source="src_b", fit=90,
                  description="Terraform and Kubernetes, Airflow orchestration.")
        self._opp("o5", "Data Scientist", "Gamma", "user_applied", 1, source="src_b", fit=85)
        self._opp("o6", "Backend Dev", "Delta", "closed", 70, source="src_c", fit=20)
        self._opp("o7", "Data Engineer!", "Acme", "discovered", 1, source="src_a", fit=55)  # third Acme repost
        self._llm("o1", 52)      # agree
        self._llm("o4", 40)      # disagree (90 vs 40)
        self._llm("o5", 80)      # agree
        self._event("o5", "discovered", "verified_active", 8)
        self._event("o5", "verified_active", "eligible", 6)
        self._event("o5", "eligible", "shortlisted", 4)
        self._event("o5", "shortlisted", "user_applied", 1)

    def test_funnel(self):
        self.seed()
        funnel = analytics.funnel_stats(self.db_path)
        self.assertEqual(funnel["total"], 7)
        self.assertEqual(funnel["counts"], {"discovered": 2, "verified_active": 1, "eligible": 1,
                                            "shortlisted": 1, "user_applied": 1, "closed": 1})
        self.assertEqual(funnel["reached"]["discovered"], 6)
        self.assertEqual(funnel["reached"]["user_applied"], 1)
        conv = {(c["from"], c["to"]): c["pct"] for c in funnel["conversions"]}
        self.assertEqual(conv[("discovered", "verified_active")], 66.7)
        self.assertEqual(conv[("shortlisted", "user_applied")], 50.0)
        self.assertEqual(funnel["median_days_source"], "lifecycle_events")
        self.assertEqual(funnel["median_days_in_stage"]["verified_active"], 2.0)
        self.assertEqual(funnel["median_days_in_stage"]["shortlisted"], 3.0)
        self.assertIsNone(funnel["median_days_in_stage"]["user_applied"])

    def test_funnel_fallback_without_events(self):
        self._opp("o1", "Data Engineer", "Acme", "discovered", 3)
        funnel = analytics.funnel_stats(self.db_path)
        self.assertEqual(funnel["median_days_source"], "created_at_updated_at")
        self.assertEqual(funnel["median_days_in_stage"]["discovered"], 0.0)

    def test_weekly(self):
        self.seed()
        weekly = analytics.weekly_activity(self.db_path, weeks=8, now=NOW)
        self.assertEqual(len(weekly["series"]), 8)
        self.assertEqual(weekly["series"][-1]["week"], "2026-W36")
        self.assertEqual(weekly["totals"]["discovered"], 6)  # o6 (40d) outside window
        self.assertEqual(weekly["totals"]["applied"], 1)
        self.assertEqual(weekly["totals"]["verified"], 1)
        self.assertEqual(weekly["series"][-1]["applied"], 1)

    def test_sources(self):
        self.seed()
        sources = {s["source"]: s for s in analytics.source_performance(self.db_path)}
        self.assertEqual(set(sources), {"src_a", "src_b", "src_c"})
        self.assertEqual(sources["src_a"]["count"], 3)
        self.assertEqual(sources["src_b"]["pct_with_description"], 66.7)
        self.assertEqual(sources["src_b"]["user_applied"], 1)
        self.assertEqual(sources["src_b"]["avg_llm_fit"], 60.0)
        self.assertEqual(sources["src_a"]["avg_fit_score"], 58.3)
        self.assertIsNone(sources["src_c"]["avg_llm_fit"])

    def test_skill_demand(self):
        self.seed()
        result = analytics.skill_demand(self.db_path, top=25, **{k: v for k, v in self.sources.items() if k != "root"})
        skills = {s["skill"]: s for s in result["skills"]}
        self.assertEqual(result["jobs_analyzed"], 7)
        self.assertEqual(skills["Python"]["jobs_requesting"], 5)
        self.assertTrue(skills["Python"]["you_have"])
        self.assertEqual(skills["Python"]["gap_priority"], 0)
        self.assertEqual(skills["Kubernetes"]["jobs_requesting"], 6)
        self.assertFalse(skills["Kubernetes"]["you_have"])
        self.assertEqual(skills["Kubernetes"]["gap_priority"], 6)
        self.assertEqual(result["top_gaps"][0]["skill"], "Kubernetes")
        self.assertTrue(all(g["gap_priority"] > 0 for g in result["top_gaps"]))

    def test_normalize_title(self):
        self.assertEqual(analytics.normalize_title("Senior Data Engineer (H/F) - II"), "data engineer")
        self.assertEqual(analytics.normalize_title("Data Engineer!"), "data engineer")

    def test_reposts(self):
        self.seed()
        reposts = analytics.detect_reposts(self.db_path)
        self.assertEqual(len(reposts["groups"]), 1)
        group = reposts["groups"][0]
        self.assertEqual(group["company"], "acme")
        self.assertEqual(group["count"], 3)
        self.assertEqual(group["span_days"], 29.0)
        self.assertEqual(group["flag"], "possible repost/ghost job")
        self.assertEqual(reposts["flagged"], 1)

    def test_reposts_short_span_not_flagged(self):
        self._opp("a", "Data Engineer", "Acme", "discovered", 5)
        self._opp("b", "Data Engineer", "Acme", "discovered", 1)
        reposts = analytics.detect_reposts(self.db_path)
        self.assertEqual(reposts["groups"][0]["count"], 2)
        self.assertIsNone(reposts["groups"][0]["flag"])
        self.assertEqual(reposts["flagged"], 0)

    def test_fit_vs_llm(self):
        self.seed()
        result = analytics.fit_vs_llm(self.db_path)
        self.assertEqual(result["n"], 3)
        self.assertEqual([d["opportunity_id"] for d in result["disagreements"]], ["o4"])
        self.assertEqual(result["disagreements"][0]["delta"], -50.0)
        self.assertAlmostEqual(result["pearson_r"], analytics.pearson([50, 90, 85], [52, 40, 80]))
        self.assertEqual(analytics.pearson([1, 2, 3], [2, 4, 6]), 1.0)
        self.assertIsNone(analytics.pearson([1, 1], [2, 3]))
        self.assertIsNone(analytics.pearson([1], [2]))

    def test_endpoints_and_summary_cache(self):
        self.seed()
        analytics.clear_cache()
        self.start_server()
        for name in ("funnel", "weekly", "sources", "skills", "reposts", "fit-vs-llm"):
            status, body = self.request(f"/api/analytics/{name}")
            self.assertEqual(status, 200, name)
        status, body = self.request("/api/analytics/weekly?weeks=3")
        self.assertEqual(len(body["series"]), 3)
        status, body = self.request("/api/analytics/nope")
        self.assertEqual(status, 404)
        status, first = self.request("/api/analytics/summary")
        self.assertEqual(status, 200)
        self.assertEqual(set(first), {"generated_at", "funnel", "weekly", "sources", "skills", "reposts", "fit_vs_llm"})
        self.assertEqual(first["funnel"]["total"], 7)
        self._opp("o8", "New Role", "Zeta", "discovered", 0)
        status, second = self.request("/api/analytics/summary")
        self.assertEqual(second["generated_at"], first["generated_at"])  # cached 60s
        self.assertEqual(second["funnel"]["total"], 7)
        analytics.clear_cache()
        status, third = self.request("/api/analytics/summary")
        self.assertEqual(third["funnel"]["total"], 8)
        # read-only: mutation verbs untouched
        status, _ = self.request("/api/analytics/summary", method="POST", payload={})
        self.assertNotEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
