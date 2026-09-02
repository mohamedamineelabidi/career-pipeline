import json
import time
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

import paste_import
import pipeline_v2

ROOT = Path(__file__).parents[1]
JD = ("Mission: build ML pipelines. Responsibilities: deploy models. Requirements: Python, SQL, "
      "Docker, 2 years of experience in data engineering. Qualifications: engineering degree. " * 2)


class PasteImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "p.sqlite3"
        pipeline_v2.create_schema(self.db)
        self.server = pipeline_v2.make_server(self.db, ROOT, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def post(self, path, payload, headers=None):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=3) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())
            except (ConnectionAbortedError, ConnectionResetError, urllib.error.URLError) as error:
                # Windows: the server may reject (403) and close before the body is written; retry the request.
                if attempt == 2:
                    raise
                if not isinstance(error, (ConnectionAbortedError, ConnectionResetError)) and not isinstance(getattr(error, "reason", None), (ConnectionAbortedError, ConnectionResetError)):
                    raise
                time.sleep(0.2)

    def test_paste_creates_then_updates_by_url(self):
        payload = {"url": "https://jobs.example/1", "title": "ML Engineer", "company": "Acme",
                   "location": "Paris", "text": JD, "version": "new"}
        status, row = self.post("/api/opportunities/paste", payload)
        self.assertEqual(status, 201)
        self.assertEqual(row["source"], "pasted_by_user")
        self.assertEqual(row["description"], JD.strip())
        self.assertEqual(row["role_kind"], "exact_vacancy")
        self.assertEqual(row["status"], "discovered")
        self.assertEqual(json.loads(row["source_json"])["jd_fetch_status"], "pasted")
        self.assertEqual(row["id"], pipeline_v2.opportunity_identity({"url": payload["url"]}))
        status, again = self.post("/api/opportunities/paste", {**payload, "text": JD + " extra"})
        self.assertEqual(status, 201)
        self.assertEqual(again["id"], row["id"])
        self.assertTrue(again["description"].endswith("extra"))
        with closing(pipeline_v2.connect(self.db)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 1)
        with urllib.request.urlopen(f"{self.base}/api/opportunities/{row['id']}/description", timeout=3) as r:
            self.assertEqual(json.loads(r.read())["jd_fetch_status"], "pasted")

    def test_paste_validation(self):
        self.assertEqual(self.post("/api/opportunities/paste", {"url": "https://x.example", "text": JD})[0], 400)
        self.assertEqual(self.post("/api/opportunities/paste", {"url": "ftp://x", "text": JD, "version": "new"})[0], 400)
        self.assertEqual(self.post("/api/opportunities/paste", {"url": "https://x.example", "text": "  ", "version": "new"})[0], 400)

    def test_cross_origin_forbidden(self):
        # The server rejects before reading the body, so keep payloads tiny (socket may close early).
        status, body = self.post(
            "/api/opportunities/paste",
            {"url": "https://x.example", "text": "jd", "version": "new"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        status, _ = self.post("/api/opportunities/opp_x/description", {"text": "jd", "version": "v"},
                              headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        with closing(pipeline_v2.connect(self.db)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 0)
        status, _ = self.post("/api/opportunities/paste",
                              {"url": "https://x.example", "text": JD, "version": "new"},
                              headers={"Origin": f"http://127.0.0.1:{self.server.server_port}"})
        self.assertEqual(status, 201)

    def test_attach_description_version_409_and_status_preserved(self):
        _, row = self.post("/api/opportunities/paste",
                           {"url": "https://x.example/2", "title": "T", "text": "short", "version": "new"})
        with closing(pipeline_v2.connect(self.db)) as c:
            c.execute("UPDATE opportunities SET status='user_applied' WHERE id=?", (row["id"],))
            c.commit()
        status, updated = self.post(f"/api/opportunities/{row['id']}/description",
                                    {"text": JD, "version": row["updated_at"]})
        self.assertEqual(status, 200)
        self.assertEqual(updated["description"], JD.strip())
        self.assertEqual(updated["status"], "user_applied")
        self.assertNotEqual(updated["updated_at"], row["updated_at"])
        self.assertEqual(json.loads(updated["source_json"])["jd_fetch_status"], "pasted")
        status, body = self.post(f"/api/opportunities/{row['id']}/description",
                                 {"text": JD, "version": row["updated_at"]})
        self.assertEqual(status, 409)
        self.assertEqual(self.post("/api/opportunities/missing/description",
                                   {"text": JD, "version": "x"})[0], 404)
        self.assertEqual(self.post(f"/api/opportunities/{row['id']}/description", {"text": JD})[0], 400)


if __name__ == "__main__":
    unittest.main()
