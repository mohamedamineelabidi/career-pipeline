import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import pipeline_v2
from reach import morocco_radar

LONG_JD = (
    "Responsibilities: build data pipelines with Spark and Airflow. Requirements: Python, SQL, "
    "2 years experience with machine learning systems and cloud deployment on AWS or GCP. "
    "You will collaborate with data scientists to ship GenAI features using LLM APIs and RAG. "
    "Qualifications: Master's degree in computer science or equivalent."
)

QUERIES = [
    {"keywords": "Data Engineer", "location": "Casablanca", "role_kind": "job", "role_family": "data_engineer"},
    {"keywords": "stage PFE data", "location": "Rabat", "role_kind": "internship", "role_family": "data_engineer"},
]


def listing(url, title="Data Engineer", description=LONG_JD, **overrides):
    record = {
        "title": title, "company": "Acme SA", "location": "Casablanca, Morocco", "url": url,
        "description": description, "publication_date": "2026-09-01", "source": "linkedin",
    }
    record.update(overrides)
    return record


def make_fetcher(description=LONG_JD):
    def fetcher(query):
        if query["role_kind"] == "job":
            return [listing("https://www.linkedin.com/jobs/view/1001", description=description),
                    listing("https://ma.indeed.com/viewjob?jk=abc", title="Senior Data Engineer", source="indeed")]
        return [listing("https://www.linkedin.com/jobs/view/2002", title="Stage PFE Data", description=description)]
    return fetcher


class MoroccoRadarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "radar.sqlite3"
        pipeline_v2.create_schema(self.db)
        self.conn = pipeline_v2.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def rows(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM opportunities ORDER BY url")]

    def test_inserts_rows_with_role_kind_and_radar_tag(self):
        summary = morocco_radar.run_radar(self.conn, QUERIES, make_fetcher())
        self.assertEqual(summary["inserted"], 3)
        self.assertEqual(summary["updated"], 0)
        self.assertEqual(summary["errors"], [])
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        by_url = {r["url"]: r for r in rows}
        job = by_url["https://www.linkedin.com/jobs/view/1001"]
        intern = by_url["https://www.linkedin.com/jobs/view/2002"]
        # The DB column `role_kind` is a CHECK-constrained classifier
        # ('role_family' | 'exact_vacancy'); the radar's internship/job kind
        # lives in `job_type` and in source_json.role_kind.
        self.assertEqual(job["job_type"], "job")
        self.assertEqual(intern["job_type"], "internship")
        self.assertEqual(job["role_kind"], "exact_vacancy")
        self.assertEqual(job["role_family"], "data_engineer")
        source = json.loads(job["source_json"])
        self.assertEqual(source["radar"], "morocco_ai_cloud")
        self.assertEqual(source["query"], "Data Engineer")
        self.assertEqual(source["role_kind"], "job")
        self.assertEqual(source["role_family"], "data_engineer")
        self.assertEqual(source["source"], "linkedin")  # job_sources fields still present
        self.assertIn("content_hash", source)
        self.assertEqual(json.loads(intern["source_json"])["query"], "stage PFE data")

    def test_second_identical_run_changes_nothing(self):
        morocco_radar.run_radar(self.conn, QUERIES, make_fetcher())
        before = self.rows()
        summary = morocco_radar.run_radar(self.conn, QUERIES, make_fetcher())
        self.assertEqual((summary["inserted"], summary["updated"]), (0, 0))
        self.assertEqual(summary["skipped"], 3)
        self.assertEqual(self.rows(), before)

    def test_user_owned_fields_survive_content_change(self):
        morocco_radar.run_radar(self.conn, QUERIES, make_fetcher())
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(opportunities)")}
        if "notes" not in columns:  # no notes column in the current schema; add one to prove it is left alone
            self.conn.execute("ALTER TABLE opportunities ADD COLUMN notes TEXT NOT NULL DEFAULT ''")
        self.conn.execute(
            "UPDATE opportunities SET status='shortlisted', notes='keep', archive_reason='manual' WHERE url=?",
            ("https://www.linkedin.com/jobs/view/1001",),
        )
        self.conn.commit()
        summary = morocco_radar.run_radar(self.conn, QUERIES, make_fetcher(description=LONG_JD + " Updated."))
        self.assertEqual(summary["inserted"], 0)
        self.assertGreaterEqual(summary["updated"], 1)
        row = dict(self.conn.execute(
            "SELECT * FROM opportunities WHERE url=?", ("https://www.linkedin.com/jobs/view/1001",)
        ).fetchone())
        self.assertEqual(row["status"], "shortlisted")
        self.assertEqual(row["notes"], "keep")
        self.assertEqual(row["archive_reason"], "manual")
        self.assertTrue(row["description"].endswith("Updated."))
        self.assertEqual(row["job_type"], "job")

    def test_dry_run_writes_nothing(self):
        summary = morocco_radar.run_radar(self.conn, QUERIES, make_fetcher(), dry_run=True)
        self.assertEqual(summary["inserted"], 3)
        self.assertTrue(summary["dry_run"])
        self.assertEqual(self.rows(), [])

    def test_limit_and_fetcher_errors_are_reported(self):
        def flaky(query):
            if query["role_kind"] == "internship":
                raise RuntimeError("boom")
            return make_fetcher()(query)
        summary = morocco_radar.run_radar(self.conn, QUERIES, flaky, limit=1)
        self.assertEqual(summary["inserted"], 1)
        self.assertEqual(len(self.rows()), 1)
        summary = morocco_radar.run_radar(self.conn, QUERIES, flaky)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertIn("boom", summary["errors"][0])

    def test_query_translation_to_job_sources_shape(self):
        translated = morocco_radar.to_job_sources_query(QUERIES[1])
        self.assertEqual(translated["search_term"], "stage PFE data")
        self.assertEqual(translated["country"], "morocco")
        self.assertEqual(translated["job_type"], "internship")
        self.assertIn("Rabat", translated["location"])


if __name__ == "__main__":
    unittest.main()
