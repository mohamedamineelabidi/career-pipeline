import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import agent_reach_channel as arc
import fetch_job_descriptions as fjd
import pipeline_v2

from test_opportunity_filters_and_descriptions import _seed

LONG = "Responsibilities: " + "build data pipelines and deploy models. " * 12
OK_HTML = f"<html><body><main><p>{LONG}</p></main></body></html>"
JINA_BODY = f"Title: Data Engineer\n\nURL Source: https://x.test/j\n\nMarkdown Content:\n# Data Engineer\n\n{LONG}\n[Apply](https://x.test/apply)\n"


def direct(responses):
    calls = []

    def fetch(url):
        calls.append(url)
        r = responses[url]
        if isinstance(r, Exception):
            raise r
        return r
    fetch.calls = calls
    return fetch


def jina(responses):
    calls = []

    def fetch(url):
        calls.append(url)
        r = responses[url]
        if isinstance(r, Exception):
            raise r
        return r
    fetch.calls = calls
    return fetch


class BackendOrderingTests(unittest.TestCase):
    def setUp(self):
        arc._last_jina_at[0] = 0.0

    def test_direct_success_never_calls_jina(self):
        url = "https://boards.test/job/1"
        d = direct({url: fjd.FetchResult(200, OK_HTML, url)})
        j = jina({})
        out = arc.read_url(url, direct_fetcher=d, jina_fetcher=j, sleep=lambda s: None, clock=lambda: 100.0)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["backend"], "direct")
        self.assertEqual(j.calls, [])
        self.assertIn("build data pipelines", out["text"])

    def test_direct_blocked_falls_back_to_jina_and_cleans_text(self):
        url = "https://jobs.ashbyhq.com/acme/123"
        d = direct({url: fjd.FetchResult(200, "<html><body>enable javascript and cookies to continue</body></html>", url)})
        j = jina({url: (200, JINA_BODY)})
        out = arc.read_url(url, direct_fetcher=d, jina_fetcher=j, sleep=lambda s: None, clock=lambda: 100.0)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["backend"], "jina")
        self.assertEqual(j.calls, [url])
        self.assertNotIn("Markdown Content", out["text"])
        self.assertNotIn("](https://", out["text"])
        self.assertIn("Apply", out["text"])
        self.assertEqual([a["backend"] for a in out["attempts"]], ["direct", "jina"])

    def test_jina_failure_ends_as_blocked(self):
        url = "https://boards.test/job/2"
        d = direct({url: fjd.FetchResult(403, "", url)})
        j = jina({url: (451, "Target URL returned error 403")})
        out = arc.read_url(url, direct_fetcher=d, jina_fetcher=j, sleep=lambda s: None, clock=lambda: 100.0)
        self.assertEqual(out["status"], "blocked")
        self.assertEqual(out["backend"], "blocked")

    def test_jina_short_or_exception_is_blocked(self):
        url = "https://boards.test/job/3"
        d = direct({url: OSError("timeout")})
        j = jina({url: (200, "Markdown Content:\nshort")})
        out = arc.read_url(url, direct_fetcher=d, jina_fetcher=j, sleep=lambda s: None, clock=lambda: 100.0)
        self.assertEqual(out["status"], "blocked")
        j2 = jina({url: OSError("boom")})
        out = arc.read_url(url, direct_fetcher=d, jina_fetcher=j2, sleep=lambda s: None, clock=lambda: 100.0)
        self.assertEqual(out["status"], "blocked")

    def test_jina_rate_limit_sleeps_between_calls(self):
        url = "https://boards.test/job/4"
        d = direct({url: fjd.FetchResult(500, "", url)})
        j = jina({url: (200, JINA_BODY)})
        slept = []
        ticks = iter([100.0, 100.0, 100.5, 100.5])
        arc.read_url(url, direct_fetcher=d, jina_fetcher=j, sleep=slept.append, clock=lambda: next(ticks))
        arc.read_url(url, direct_fetcher=d, jina_fetcher=j, sleep=slept.append, clock=lambda: next(ticks))
        self.assertTrue(slept and slept[-1] >= 1.4)


class DenyListTests(unittest.TestCase):
    def test_deny_hosts(self):
        for url in ("https://www.linkedin.com/jobs/view/1", "https://fr.linkedin.com/jobs/view/1",
                    "https://www.glassdoor.fr/job/x", "https://ca.indeed.com/viewjob?jk=1", "https://www.indeed.com/x"):
            self.assertTrue(arc.is_jina_denied(url), url)
        self.assertFalse(arc.is_jina_denied("https://jobs.ashbyhq.com/x"))
        self.assertFalse(arc.is_jina_denied("https://notlinkedin.example/linkedin.com"))

    def test_denied_url_never_reaches_jina(self):
        url = "https://www.linkedin.com/jobs/view/999"
        d = direct({url: fjd.FetchResult(200, "<html>Sign in</html>", "https://www.linkedin.com/authwall")})
        j = jina({url: (200, JINA_BODY)})
        out = arc.read_url(url, direct_fetcher=d, jina_fetcher=j, sleep=lambda s: None, clock=lambda: 0.0)
        self.assertEqual(out["status"], "login_wall")
        self.assertEqual(out["backend"], "blocked")
        self.assertEqual(j.calls, [])
        self.assertEqual(out["attempts"][-1]["status"], "skipped_denylist")
        self.assertEqual(arc.read_via_jina(url, fetcher=j, sleep=lambda s: None, clock=lambda: 0.0), ("login_wall", ""))
        self.assertEqual(j.calls, [])


class UseReaderRunTests(unittest.TestCase):
    def rows(self):
        return [
            {"id": "opp-blocked", "url": "https://jobs.ashbyhq.com/acme/1", "source_json": json.dumps({"jd_fetch_status": "blocked"}), "priority_score": 90},
            {"id": "opp-error", "url": "https://boards.test/j/2", "source_json": json.dumps({"jd_fetch_status": "error:500"}), "priority_score": 80},
            {"id": "opp-li", "url": "https://www.linkedin.com/jobs/view/3", "source_json": json.dumps({"jd_fetch_status": "blocked"}), "priority_score": 70},
            {"id": "opp-loginwall", "url": "https://boards.test/j/4", "source_json": json.dumps({"jd_fetch_status": "login_wall"})},
            {"id": "opp-ok-already", "url": "https://boards.test/j/5", "source_json": json.dumps({"jd_fetch_status": "ok"})},
            {"id": "opp-never", "url": "https://boards.test/j/6"},
            {"id": "opp-hasjd", "url": "https://boards.test/j/7", "description": LONG, "source_json": json.dumps({"jd_fetch_status": "blocked"})},
        ]

    def fake_reader(self, results):
        calls = []

        def read(url):
            calls.append(url)
            return results[url]
        read.calls = calls
        return read

    def test_marks_ok_reader_and_jina_backend(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "p.sqlite3"
            _seed(db, self.rows())
            reader = self.fake_reader({
                "https://jobs.ashbyhq.com/acme/1": {"text": LONG, "status": "ok", "backend": "jina", "attempts": [{"backend": "direct", "status": "blocked"}, {"backend": "jina", "status": "ok"}]},
                "https://boards.test/j/2": {"text": "", "status": "blocked", "backend": "blocked", "attempts": []},
                "https://www.linkedin.com/jobs/view/3": {"text": "", "status": "login_wall", "backend": "blocked", "attempts": []},
            })
            summary = fjd.run_reader(db, reader=reader)
            self.assertEqual(summary["candidates"], 3)
            self.assertEqual(summary["ok_reader"], 1)
            self.assertEqual(summary["blocked"], 1)
            self.assertEqual(summary["login_wall"], 1)
            self.assertEqual(summary["recovered_by_host"], {"jobs.ashbyhq.com": 1})
            self.assertEqual(set(reader.calls), {"https://jobs.ashbyhq.com/acme/1", "https://boards.test/j/2", "https://www.linkedin.com/jobs/view/3"})
            with closing(pipeline_v2.connect(db)) as c:
                rows = {r["id"]: dict(r) for r in c.execute("SELECT * FROM opportunities")}
            src = json.loads(rows["opp-blocked"]["source_json"])
            self.assertEqual(src["jd_fetch_status"], "ok_reader")
            self.assertEqual(src["jd_backend"], "jina")
            self.assertEqual(rows["opp-blocked"]["description"], LONG)
            self.assertEqual(src["full_job_description"], LONG)
            self.assertEqual(rows["opp-blocked"]["status"], "discovered")
            self.assertEqual(json.loads(rows["opp-li"]["source_json"])["jd_fetch_status"], "login_wall")
            self.assertEqual(json.loads(rows["opp-error"]["source_json"])["jd_fetch_status"], "blocked")
            for untouched in ("opp-loginwall", "opp-ok-already", "opp-never", "opp-hasjd"):
                self.assertEqual(rows[untouched]["updated_at"], "2026-01-01T00:00:00+00:00", untouched)

    def test_dry_run_and_limit(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "p.sqlite3"
            _seed(db, self.rows())
            reader = self.fake_reader({"https://jobs.ashbyhq.com/acme/1": {"text": LONG, "status": "ok", "backend": "jina", "attempts": []}})
            summary = fjd.run_reader(db, reader=reader, limit=1, dry_run=True)
            self.assertEqual(summary["ok_reader"], 1)
            self.assertEqual(len(reader.calls), 1)
            with closing(pipeline_v2.connect(db)) as c:
                row = c.execute("SELECT description, updated_at FROM opportunities WHERE id='opp-blocked'").fetchone()
            self.assertEqual(row["description"], "")
            self.assertEqual(row["updated_at"], "2026-01-01T00:00:00+00:00")

    def test_cli_flag_records_automation_run(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "p.sqlite3"
            _seed(db, [{"id": "opp-1", "url": "https://boards.test/j/1", "source_json": json.dumps({"jd_fetch_status": "blocked"})}])
            original = fjd.run_reader
            fjd.run_reader = lambda db_path, **kw: {"candidates": 1, "ok_reader": 0, "ok": 0, "blocked": 1, "login_wall": 0, "recovered_by_host": {}, "still_blocked_by_host": {}, "dry_run": False}
            try:
                self.assertEqual(fjd.main(["--db", str(db), "--use-reader"]), 0)
            finally:
                fjd.run_reader = original
            with closing(pipeline_v2.connect(db)) as c:
                run = c.execute("SELECT run_type, status FROM automation_runs").fetchone()
            self.assertEqual((run["run_type"], run["status"]), ("fetch_job_descriptions_reader", "no_change"))
