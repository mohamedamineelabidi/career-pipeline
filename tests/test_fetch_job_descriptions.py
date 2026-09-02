import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import fetch_job_descriptions as fjd
import pipeline_v2

from test_opportunity_filters_and_descriptions import _seed

LONG = "Responsibilities: " + "build data pipelines and deploy models. " * 12


def fake_fetcher(responses):
    calls = []

    def fetch(url):
        calls.append(url)
        result = responses[url]
        if isinstance(result, Exception):
            raise result
        return result

    fetch.calls = calls
    return fetch


class ExtractionTests(unittest.TestCase):
    def test_prefers_json_ld_job_posting_description(self):
        html = """<html><head><script type="application/ld+json">
        {"@type": "JobPosting", "title": "X", "description": "<p>From JSON-LD: %s</p>"}
        </script></head><body><nav>menu</nav><div>%s other</div></body></html>""" % (LONG, LONG)
        text = fjd.extract_description(html)
        self.assertTrue(text.startswith("From JSON-LD"))
        self.assertNotIn("<p>", text)

    def test_falls_back_to_largest_text_block_and_strips_noise(self):
        html = f"""<html><body><script>var a=1;</script><style>.x{{}}</style><nav>Home About</nav>
        <div class="short">tiny</div><section><p>{LONG}</p></section><footer>foot</footer></body></html>"""
        text = fjd.extract_description(html)
        self.assertIn("build data pipelines", text)
        self.assertNotIn("var a=1", text)
        self.assertNotIn("Home About", text)

    def test_login_wall_detection(self):
        self.assertTrue(fjd.is_login_wall_url("https://www.linkedin.com/login?x=1"))
        self.assertTrue(fjd.is_login_wall_url("https://www.linkedin.com/authwall?trk=x"))
        self.assertTrue(fjd.is_login_wall_url("https://www.glassdoor.com/job/x"))
        self.assertFalse(fjd.is_login_wall_url("https://www.linkedin.com/jobs/view/123"))


class RunTests(unittest.TestCase):
    def rows(self):
        return [
            {"id": "opp-ok", "url": "https://boards.test/job/1", "description": "short", "priority_score": 99},
            {"id": "opp-closed", "url": "https://boards.test/job/2", "status": "closed"},
            {"id": "opp-has-jd", "url": "https://boards.test/job/3", "description": LONG},
            {"id": "opp-mailto", "url": "mailto:hr@x.test"},
            {"id": "opp-403", "url": "https://www.indeed.com/viewjob?jk=1"},
            {"id": "opp-500", "url": "https://boards.test/job/5"},
            {"id": "opp-blocked", "url": "https://boards.test/job/6"},
            {"id": "opp-li", "url": "https://www.linkedin.com/login?session_redirect=x"},
            {"id": "opp-redirect", "url": "https://www.linkedin.com/jobs/view/999"},
        ]

    def test_run_updates_only_candidates_and_records_statuses(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "p.sqlite3"
            _seed(db_path, self.rows())
            ok_html = f"<html><body><main><p>{LONG}</p></main></body></html>"
            fetch = fake_fetcher({
                "https://boards.test/job/1": fjd.FetchResult(200, ok_html, "https://boards.test/job/1"),
                "https://www.indeed.com/viewjob?jk=1": fjd.FetchResult(403, "blocked", "https://www.indeed.com/viewjob?jk=1"),
                "https://boards.test/job/5": fjd.FetchResult(500, "", "https://boards.test/job/5"),
                "https://boards.test/job/6": fjd.FetchResult(200, "<html><body><p>Please verify you are a human. captcha</p></body></html>", "https://boards.test/job/6"),
                "https://www.linkedin.com/jobs/view/999": fjd.FetchResult(200, "<html><body>Sign in</body></html>", "https://www.linkedin.com/authwall?x=1"),
            })
            summary = fjd.run(db_path, fetcher=fetch, sleep=lambda _s: None)
            self.assertEqual(summary["candidates"], 6)
            self.assertEqual(summary["ok"], 1)
            self.assertEqual(summary["login_wall"], 3)  # indeed 403, linkedin login (skipped), authwall redirect
            self.assertEqual(summary["blocked"], 1)
            self.assertEqual(summary["error"], 1)
            self.assertNotIn("https://www.linkedin.com/login?session_redirect=x", fetch.calls)
            with closing(pipeline_v2.connect(db_path)) as connection:
                rows = {r["id"]: dict(r) for r in connection.execute("SELECT * FROM opportunities")}
            ok = rows["opp-ok"]
            self.assertIn("build data pipelines", ok["description"])
            source = json.loads(ok["source_json"])
            self.assertEqual(source["jd_fetch_status"], "ok")
            self.assertEqual(source["full_job_description"], ok["description"])
            self.assertTrue(source["jd_fetched_at"])
            self.assertNotEqual(ok["updated_at"], "2026-01-01T00:00:00+00:00")
            self.assertEqual(ok["status"], "discovered")
            self.assertEqual(json.loads(rows["opp-403"]["source_json"])["jd_fetch_status"], "login_wall")
            self.assertEqual(json.loads(rows["opp-li"]["source_json"])["jd_fetch_status"], "login_wall")
            self.assertEqual(json.loads(rows["opp-redirect"]["source_json"])["jd_fetch_status"], "login_wall")
            self.assertEqual(json.loads(rows["opp-500"]["source_json"])["jd_fetch_status"], "error:500")
            self.assertEqual(json.loads(rows["opp-blocked"]["source_json"])["jd_fetch_status"], "blocked")
            # failures keep description untouched
            self.assertEqual(rows["opp-500"]["description"], "")
            for untouched in ("opp-closed", "opp-has-jd", "opp-mailto"):
                self.assertEqual(rows[untouched]["updated_at"], "2026-01-01T00:00:00+00:00")
                self.assertNotIn("jd_fetch_status", json.loads(rows[untouched]["source_json"]))

    def test_dry_run_and_limit_do_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "p.sqlite3"
            _seed(db_path, self.rows())
            ok_html = f"<html><body><p>{LONG}</p></body></html>"
            fetch = fake_fetcher({"https://boards.test/job/1": fjd.FetchResult(200, ok_html, "https://boards.test/job/1")})
            summary = fjd.run(db_path, fetcher=fetch, sleep=lambda _s: None, limit=1, dry_run=True)
            self.assertEqual(summary["ok"], 1)
            self.assertEqual(len(fetch.calls), 1)
            with closing(pipeline_v2.connect(db_path)) as connection:
                row = connection.execute("SELECT description, updated_at FROM opportunities WHERE id='opp-ok'").fetchone()
            self.assertEqual(row["description"], "short")
            self.assertEqual(row["updated_at"], "2026-01-01T00:00:00+00:00")

    def test_network_exception_is_recorded_as_error(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "p.sqlite3"
            _seed(db_path, [{"id": "opp-ok", "url": "https://boards.test/job/1"}])
            fetch = fake_fetcher({"https://boards.test/job/1": OSError("timed out")})
            summary = fjd.run(db_path, fetcher=fetch, sleep=lambda _s: None)
            self.assertEqual(summary["error"], 1)
            with closing(pipeline_v2.connect(db_path)) as connection:
                source = json.loads(connection.execute("SELECT source_json FROM opportunities").fetchone()[0])
            self.assertTrue(source["jd_fetch_status"].startswith("error:"))

    def test_summary_table_format(self):
        table = fjd.format_summary({"candidates": 3, "ok": 1, "blocked": 1, "login_wall": 0, "error": 1, "hosts_blocked": ["a.test"]})
        self.assertIn("ok", table)
        self.assertIn("a.test", table)
