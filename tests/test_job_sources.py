import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import job_sources
import pipeline_v2

LONG_JD = (
    "Responsibilities: build data pipelines with Spark and Airflow. Requirements: Python, SQL, "
    "2 years experience with machine learning systems and cloud deployment on AWS or GCP. "
    "You will collaborate with data scientists to ship GenAI features using LLM APIs and RAG. "
    "Qualifications: Master's degree in computer science or equivalent."
)


def fake_record(**overrides):
    record = {
        "id": "li-1", "site": "linkedin", "job_url": "https://www.linkedin.com/jobs/view/111",
        "job_url_direct": None, "title": "Data Engineer", "company": "Acme SA",
        "location": "Rabat, Morocco", "date_posted": "2026-08-30", "job_type": "fulltime",
        "salary_source": "direct_data", "interval": "yearly", "min_amount": 30000.0,
        "max_amount": 45000.0, "currency": "MAD", "is_remote": False, "job_level": "entry",
        "job_function": None, "listing_type": None, "emails": "hr@acme.ma",
        "description": LONG_JD, "company_industry": None, "company_url": "https://acme.ma",
    }
    record.update(overrides)
    return record


class FakeScraper:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    def __call__(self, query):
        self.calls.append(query)
        item = self.batches.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


QUERY = {"search_term": "data engineer", "location": "Rabat", "country": "ma",
         "sites": ["linkedin"], "hours_old": 168, "results_wanted": 5}


class JobSourcesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "p.sqlite3"
        self.sleeps = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_discover(self, batches, **kw):
        scraper = FakeScraper(batches)
        summary = job_sources.discover(
            [QUERY] * len(batches), self.db, scraper=scraper, sleep=self.sleeps.append, **kw
        )
        return scraper, summary

    def fetch(self, url):
        with closing(pipeline_v2.connect(self.db)) as c:
            return dict(c.execute("SELECT * FROM opportunities WHERE url=?", (url,)).fetchone())

    def test_mapping_and_migration_v6(self):
        mapped = job_sources.map_job(fake_record(), job_sources.normalize_query(QUERY))
        self.assertEqual(mapped["source"], "linkedin")
        self.assertEqual(mapped["publication_date"], "2026-08-30")
        self.assertEqual(mapped["salary_currency"], "MAD")
        self.assertEqual(mapped["source_json"]["emails"], ["hr@acme.ma"])
        self.assertEqual(mapped["source_json"]["company_url"], "https://acme.ma")
        self.assertEqual(mapped["source_json"]["salary_interval"], "yearly")
        self.assertEqual(len(mapped["source_json"]["content_hash"]), 64)
        self.assertIsNone(job_sources.map_job(fake_record(job_url=None), QUERY))
        _, summary = self.run_discover([[fake_record()]])
        self.assertEqual(summary["inserted"], 1)
        row = self.fetch("https://www.linkedin.com/jobs/view/111")
        self.assertEqual(row["role_kind"], "exact_vacancy")
        self.assertEqual(row["status"], "discovered")
        self.assertEqual(row["job_type"], "fulltime")
        self.assertEqual(row["is_remote"], 0)
        self.assertEqual(row["salary_min"], 30000.0)
        self.assertEqual(row["id"], pipeline_v2.opportunity_identity({"url": row["url"]}))
        with closing(pipeline_v2.connect(self.db)) as c:
            self.assertGreaterEqual(c.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertGreaterEqual(pipeline_v2.MIGRATION_VERSION, 6)
            columns = {r[1] for r in c.execute("PRAGMA table_info(opportunities)")}
            self.assertTrue({"content_hash", "job_type", "is_remote", "salary_min", "salary_max", "salary_currency"} <= columns)
            runs = [dict(r) for r in c.execute("SELECT * FROM automation_runs")]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run_type"], job_sources.JOB_NAME)
        self.assertEqual(runs[0]["status"], "success")

    def test_dedupe_within_run_and_unchanged_on_rescan(self):
        _, summary = self.run_discover([[fake_record(), fake_record(id="li-dup")]])
        self.assertEqual((summary["inserted"], summary["records_mapped"]), (1, 1))
        first = self.fetch("https://www.linkedin.com/jobs/view/111")
        _, summary = self.run_discover([[fake_record()]])
        self.assertEqual((summary["inserted"], summary["updated"], summary["unchanged"]), (0, 0, 1))
        self.assertEqual(summary["automation_run_status"], "no_change")
        self.assertEqual(self.fetch(first["url"])["updated_at"], first["updated_at"])

    def test_content_hash_update_preserves_user_status(self):
        self.run_discover([[fake_record()]])
        url = "https://www.linkedin.com/jobs/view/111"
        with closing(pipeline_v2.connect(self.db)) as c:
            c.execute("UPDATE opportunities SET status='user_applied', archive_reason='' WHERE url=?", (url,))
            c.commit()
        before = self.fetch(url)
        _, summary = self.run_discover([[fake_record(description=LONG_JD + " Updated: deadline extended.")]])
        self.assertEqual(summary["updated"], 1)
        after = self.fetch(url)
        self.assertEqual(after["status"], "user_applied")
        self.assertEqual(after["archive_reason"], "")
        self.assertEqual(after["created_at"], before["created_at"])
        self.assertNotEqual(after["updated_at"], before["updated_at"])
        self.assertNotEqual(after["content_hash"], before["content_hash"])
        self.assertIn("deadline extended", after["description"])
        self.assertEqual(json.loads(after["source_json"])["content_hash"], after["content_hash"])

    def test_blocked_query_reported_dry_run_and_limit(self):
        scraper, summary = self.run_discover([RuntimeError("429 Too Many Requests"), [fake_record()]])
        self.assertEqual((summary["queries_ok"], summary["queries_blocked"]), (1, 1))
        self.assertEqual(summary["per_query"][0]["status"], "blocked")
        self.assertIn("429", summary["per_query"][0]["error"])
        self.assertEqual(summary["automation_run_status"], "partial")
        self.assertEqual(self.sleeps, [job_sources.MIN_SECONDS_BETWEEN_QUERIES])
        records = [fake_record(id=str(i), job_url=f"https://x.example/{i}") for i in range(5)]
        _, summary = self.run_discover([records], dry_run=True, limit=2)
        self.assertEqual(summary["inserted"], 2)
        with closing(pipeline_v2.connect(self.db)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 1)

    def test_default_queries_file_created(self):
        path = Path(self.tmp.name) / "q.json"
        queries = job_sources.load_queries(path)
        self.assertTrue(path.exists())
        countries = {q["country"] for q in queries}
        self.assertTrue({"morocco", "france", "canada", "united arab emirates"} <= countries)
        self.assertTrue(any(q["job_type"] == "internship" for q in queries))
        self.assertTrue(any(q["is_remote"] for q in queries))
        with self.assertRaises(ValueError):
            job_sources.normalize_query({"sites": ["monster"]})


if __name__ == "__main__":
    unittest.main()
