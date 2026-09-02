import json
import re
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

import pipeline_v2

ROOT = Path(__file__).resolve().parents[1]


def _seed(db_path, rows):
    pipeline_v2.create_schema(db_path)
    with closing(pipeline_v2.connect(db_path)) as connection:
        for row in rows:
            defaults = {
                "id": "opp-x", "title": "T", "company": "C", "location": "L", "url": "https://example.test/j",
                "source": "official", "publication_date": None, "role_kind": "role_family", "role_family": "",
                "description": "", "requirements": "", "deadline": None,
                "source_verification_status": "verified_official_source", "fit_score": 50,
                "eligibility_status": "eligible", "freshness_status": "active", "verification_confidence": 90,
                "priority_score": 50, "score_schema_version": 2, "score_breakdown_json": "{}", "archive_reason": "",
                "match_score": 50, "status": "discovered", "source_json": "{}",
                "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
            }
            defaults.update(row)
            columns = ", ".join(defaults)
            placeholders = ", ".join("?" for _ in defaults)
            connection.execute(f"INSERT INTO opportunities({columns}) VALUES ({placeholders})", tuple(defaults.values()))
        connection.commit()


class OpportunityApiFieldsTests(unittest.TestCase):
    def test_api_rows_expose_track_publication_date_and_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "p.sqlite3"
            _seed(db_path, [{
                "id": "opp-1", "publication_date": "2026-08-15", "deadline": "2026-09-07",
                "source_json": json.dumps({"opportunity_track": "pfe_internship"}),
            }, {"id": "opp-2"}])
            rows = pipeline_v2.api_data(db_path, "opportunities")
            by_id = {row["id"]: row for row in rows}
            self.assertEqual(by_id["opp-1"]["opportunity_track"], "pfe_internship")
            self.assertEqual(by_id["opp-1"]["publication_date"], "2026-08-15")
            self.assertEqual(by_id["opp-1"]["deadline"], "2026-09-07")
            self.assertEqual(by_id["opp-2"]["opportunity_track"], "")
            self.assertIsNone(by_id["opp-2"]["publication_date"])
            # additive: legacy fields still present
            self.assertTrue({"score", "cv_path", "cv_status", "updated_at"} <= set(by_id["opp-1"]))

    def test_description_endpoint_returns_status_and_404_for_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "p.sqlite3"
            _seed(db_path, [{
                "id": "opp-1", "description": "Long text",
                "source_json": json.dumps({"jd_fetch_status": "ok", "jd_fetched_at": "2026-09-01T00:00:00+00:00"}),
            }])
            detail = pipeline_v2.opportunity_description(db_path, "opp-1")
            self.assertEqual(detail, {
                "id": "opp-1", "description": "Long text", "jd_fetch_status": "ok",
                "jd_fetched_at": "2026-09-01T00:00:00+00:00",
            })
            with self.assertRaises(pipeline_v2.NotFoundError):
                pipeline_v2.opportunity_description(db_path, "nope")
            server = pipeline_v2.make_server(db_path, ROOT, port=0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(base + "/api/opportunities/opp-1/description", timeout=3) as response:
                    body = json.loads(response.read())
                self.assertEqual(body["id"], "opp-1")
                self.assertEqual(body["jd_fetch_status"], "ok")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(base + "/api/opportunities/missing/description", timeout=3)
                self.assertEqual(caught.exception.code, 404)
            finally:
                server.shutdown(); server.server_close()


class DashboardFiltersTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "pipeline_v2.html").read_text(encoding="utf-8")

    def test_filters_sort_and_columns_present(self):
        for token in (
            'id="opportunity-type-filter"', 'id="opportunity-posted-filter"',
            'id="opportunity-deadline-filter"', 'id="opportunity-sort"',
            'value="professional_role"', 'value="internship"', 'value="pfe_internship"',
            "<th>Posted</th>", "Deadline ${deadline.label}", "Not provided",
            "posted_desc", "deadline_asc", "priority_desc", "fit_desc",
        ):
            self.assertIn(token, self.text, token)

    def test_overview_breakdown_and_description_toggle(self):
        for token in ("By type", "Applied by you", "user_applied", "/description", "Description", "pre-wrap", "jd-panel"):
            self.assertIn(token, self.text, token)
        self.assertNotIn("innerHTML", self.text)
