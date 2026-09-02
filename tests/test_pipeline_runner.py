"""Tests for pipeline_runner: staged, resumable, draft-only pipeline run."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pipeline_runner  # noqa: E402
from resume_matcher_fixtures import PortTestCase  # noqa: E402


class RunnerTests(PortTestCase):
    def fake_stages(self, fail_at=None):
        calls = []

        def make(name):
            def stage(db_path, log):
                calls.append(name)
                log(f"{name} ran")
                if name == fail_at:
                    raise RuntimeError(f"{name} boom")
                return {"stage": name, "count": len(calls)}
            return stage
        return calls, {name: make(name) for name in pipeline_runner.STAGE_ORDER}

    def test_stage_order_is_the_documented_pipeline(self):
        self.assertEqual(pipeline_runner.STAGE_ORDER,
                         ("discover", "fetch_descriptions", "match", "llm_score", "digest"))

    def test_run_records_each_stage_and_persists(self):
        calls, stages = self.fake_stages()
        result = pipeline_runner.run(self.db_path, stages=stages, sleep=lambda _s: None)
        self.assertEqual(calls, list(pipeline_runner.STAGE_ORDER))
        self.assertEqual(result["status"], "completed")
        self.assertEqual([s["name"] for s in result["stages"]], list(pipeline_runner.STAGE_ORDER))
        self.assertTrue(all(s["status"] == "ok" for s in result["stages"]))
        stored = pipeline_runner.latest(self.db_path)
        self.assertEqual(stored["id"], result["id"])
        self.assertEqual(stored["status"], "completed")
        self.assertIn("discover ran", stored["log"])

    def test_failure_marks_stage_failed_and_continues(self):
        calls, stages = self.fake_stages(fail_at="fetch_descriptions")
        result = pipeline_runner.run(self.db_path, stages=stages, sleep=lambda _s: None)
        by_name = {s["name"]: s for s in result["stages"]}
        self.assertEqual(by_name["fetch_descriptions"]["status"], "failed")
        self.assertIn("boom", by_name["fetch_descriptions"]["error"])
        self.assertEqual(by_name["match"]["status"], "ok")
        self.assertEqual(result["status"], "completed_with_errors")

    def test_selected_stages_only(self):
        calls, stages = self.fake_stages()
        pipeline_runner.run(self.db_path, stages=stages, only=("match", "digest"), sleep=lambda _s: None)
        self.assertEqual(calls, ["match", "digest"])

    def test_runner_never_sends_or_applies(self):
        source = Path(pipeline_runner.__file__).read_text(encoding="utf-8")
        for forbidden in ("smtplib", "linkedin.com/", "voyager", ".click(", "submit("):
            self.assertNotIn(forbidden, source)

    def test_digest_is_text_with_sections(self):
        opp_id, _ = self.insert_opportunity()
        text = pipeline_runner.build_digest(self.db_path)
        for header in ("New this run", "Best fits", "Follow up", "Due today", "Ready to submit"):
            self.assertIn(header, text)
        self.assertNotIn("\u2014", text)

    def test_http_endpoints(self):
        _, stages = self.fake_stages()
        pipeline_runner.run(self.db_path, stages=stages, sleep=lambda _s: None)
        self.start_server()
        status, body = self.request("/api/pipeline/latest")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "completed")
        status, body = self.request("/api/pipeline/runs")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["runs"]), 1)
        status, _ = self.request("/api/pipeline/run", "POST", {"only": ["digest"]}, origin="http://evil.test")
        self.assertEqual(status, 403)
        status, body = self.request("/api/pipeline/run", "POST", {"only": ["digest"]})
        self.assertEqual(status, 202)
        self.assertIn("id", body)
        for _ in range(50):
            time.sleep(0.1)
            status, body = self.request(f"/api/pipeline/runs/{body['id']}")
            if body.get("status") != "running":
                break
        self.assertIn(body["status"], ("completed", "completed_with_errors"))
        self.assertEqual([s["name"] for s in body["stages"]], ["digest"])


if __name__ == "__main__":
    import unittest
    unittest.main()
