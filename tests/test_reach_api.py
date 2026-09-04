import json
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

import pipeline_v2

REACH_TABLES = """
CREATE TABLE IF NOT EXISTS target_companies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    sector TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    intent TEXT NOT NULL DEFAULT 'any',
    priority INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people_candidates (
    id TEXT PRIMARY KEY,
    target_company_id TEXT REFERENCES target_companies(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    headline TEXT NOT NULL DEFAULT '',
    company_seen TEXT NOT NULL DEFAULT '',
    role_seen TEXT NOT NULL DEFAULT '',
    profile_url TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    evidence_url TEXT NOT NULL DEFAULT '',
    evidence_quote TEXT NOT NULL DEFAULT '',
    discovered_via TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL DEFAULT 0,
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    current_role_confirmed_at TEXT,
    promoted_contact_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def make_db(directory):
    db_path = Path(directory) / "pipeline.sqlite3"
    connection = sqlite3.connect(str(db_path))
    connection.executescript(pipeline_v2.SCHEMA)
    connection.executescript(REACH_TABLES)
    connection.commit()
    connection.close()
    return db_path


class ReachApiHttpTests(unittest.TestCase):
    def request_json(self, base_url, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        request = urllib.request.Request(base_url + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def request_error(self, base_url, path, method="GET", payload=None):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request_json(base_url, path, method=method, payload=payload)
        return ctx.exception.code, json.loads(ctx.exception.read())

    def start_server(self, directory):
        db_path = make_db(directory)
        server = pipeline_v2.make_server(db_path, Path(directory), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        return db_path, base

    # D1 ---------------------------------------------------------------
    def test_targets_empty_then_created_ordered_by_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            _, base = self.start_server(directory)
            status, rows = self.request_json(base, "/api/reach/targets")
            self.assertEqual(status, 200)
            self.assertEqual(rows, [])
            status, low = self.request_json(base, "/api/reach/targets", "POST",
                                            {"name": "Zeta", "priority": 1, "intent": "internship"})
            self.assertEqual(status, 201)
            self.assertEqual(low["name"], "Zeta")
            self.assertEqual(low["intent"], "internship")
            status, high = self.request_json(base, "/api/reach/targets", "POST",
                                             {"name": "Alpha", "priority": 5, "sector": "cloud", "country": "MA"})
            self.assertEqual(status, 201)
            status, rows = self.request_json(base, "/api/reach/targets")
            self.assertEqual([r["name"] for r in rows], ["Alpha", "Zeta"])
            self.assertEqual(rows[0]["sector"], "cloud")

    def test_post_existing_target_name_returns_200_without_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            _, base = self.start_server(directory)
            status, first = self.request_json(base, "/api/reach/targets", "POST", {"name": "OCP"})
            self.assertEqual(status, 201)
            status, again = self.request_json(base, "/api/reach/targets", "POST", {"name": "OCP", "priority": 9})
            self.assertEqual(status, 200)
            self.assertEqual(again["id"], first["id"])
            _, rows = self.request_json(base, "/api/reach/targets")
            self.assertEqual(len(rows), 1)

    def test_post_target_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            _, base = self.start_server(directory)
            code, body = self.request_error(base, "/api/reach/targets", "POST", {"name": "  "})
            self.assertEqual(code, 400)
            self.assertIn("name", body["error"])
            code, body = self.request_error(base, "/api/reach/targets", "POST", {"name": "X", "intent": "spam"})
            self.assertEqual(code, 400)
            self.assertIn("intent", body["error"])

    # D2 ---------------------------------------------------------------
    def seed_people(self, db_path):
        connection = sqlite3.connect(str(db_path))
        now = "2026-09-01T00:00:00+00:00"
        connection.execute(
            "INSERT INTO target_companies (id, name, created_at, updated_at) VALUES ('tgt_a', 'Acme', ?, ?)",
            (now, now))
        connection.execute(
            "INSERT INTO target_companies (id, name, created_at, updated_at) VALUES ('tgt_b', 'Globex', ?, ?)",
            (now, now))
        people = [
            ("p_hi", "tgt_a", "Hana", 90, "evidence_found", None),
            ("p_lo", "tgt_a", "Lina", 40, "unverified", None),
            ("p_ok", "tgt_b", "Omar", 75, "evidence_found", now),
        ]
        for pid, tgt, name, score, status, confirmed in people:
            connection.execute(
                "INSERT INTO people_candidates (id, target_company_id, name, score, verification_status,"
                " current_role_confirmed_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, tgt, name, score, status, confirmed, now, now))
        connection.commit()
        connection.close()

    def test_people_listing_joins_target_and_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path, base = self.start_server(directory)
            self.seed_people(db_path)
            status, rows = self.request_json(base, "/api/reach/people")
            self.assertEqual(status, 200)
            self.assertEqual([r["name"] for r in rows], ["Hana", "Omar", "Lina"])
            self.assertEqual(rows[0]["target_name"], "Acme")
            _, rows = self.request_json(base, "/api/reach/people?target=tgt_a&min_score=50")
            self.assertEqual([r["id"] for r in rows], ["p_hi"])
            _, rows = self.request_json(base, "/api/reach/people?status=unverified")
            self.assertEqual([r["id"] for r in rows], ["p_lo"])

    def test_confirm_role_requires_true_and_stamps_row(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path, base = self.start_server(directory)
            self.seed_people(db_path)
            code, _ = self.request_error(base, "/api/reach/people/p_hi/confirm-role", "POST", {"confirmed": "yes"})
            self.assertEqual(code, 400)
            status, row = self.request_json(base, "/api/reach/people/p_hi/confirm-role", "POST", {"confirmed": True})
            self.assertEqual(status, 200)
            self.assertEqual(row["id"], "p_hi")
            self.assertTrue(row["current_role_confirmed_at"])
            code, _ = self.request_error(base, "/api/reach/people/nope/confirm-role", "POST", {"confirmed": True})
            self.assertEqual(code, 404)

    def test_promote_gate_and_success(self):
        import reach.api as reach_api

        def fake_promote(db_path, candidate_id):
            connection = sqlite3.connect(str(db_path))
            try:
                row = connection.execute(
                    "SELECT current_role_confirmed_at FROM people_candidates WHERE id = ?", (candidate_id,)
                ).fetchone()
                if row is None:
                    raise LookupError("unknown candidate")
                if row[0] is None:
                    raise ValueError("current role not confirmed")
                connection.execute(
                    "UPDATE people_candidates SET promoted_contact_id = 'contact_1' WHERE id = ?", (candidate_id,))
                connection.commit()
            finally:
                connection.close()
            return "contact_1"

        with tempfile.TemporaryDirectory() as directory, \
                unittest.mock.patch.object(reach_api, "PROMOTE", fake_promote):
            db_path, base = self.start_server(directory)
            self.seed_people(db_path)
            code, body = self.request_error(base, "/api/reach/people/p_hi/promote", "POST", {})
            self.assertEqual(code, 409)
            self.assertEqual(body["error"], "current role not confirmed")
            code, _ = self.request_error(base, "/api/reach/people/missing/promote", "POST", {})
            self.assertEqual(code, 404)
            status, body = self.request_json(base, "/api/reach/people/p_ok/promote", "POST", {})
            self.assertEqual(status, 200)
            self.assertEqual(body, {"contact_id": "contact_1"})

    def test_draft_requires_promotion_and_saves_draft(self):
        import reach.api as reach_api
        calls = []

        def fake_draft_for(candidate, lang, fact, channel="linkedin", opportunity=None):
            calls.append((candidate["id"], lang, fact, channel))
            return f"Bonjour {candidate['name']} — {fact}"

        def fake_save_draft(db_path, candidate, body, channel, opportunity_id=None):
            connection = sqlite3.connect(str(db_path))
            connection.execute(
                "INSERT INTO drafts (id, contact_id, channel, body, status, source_json, created_at, updated_at)"
                " VALUES ('d_1', ?, ?, ?, 'draft_not_opened', '{}', 'x', 'x')",
                (candidate["promoted_contact_id"], channel, body))
            connection.commit()
            connection.close()
            return "d_1"

        with tempfile.TemporaryDirectory() as directory, \
                unittest.mock.patch.object(reach_api, "DRAFT_FOR", fake_draft_for), \
                unittest.mock.patch.object(reach_api, "SAVE_DRAFT", fake_save_draft):
            db_path, base = self.start_server(directory)
            self.seed_people(db_path)
            code, _ = self.request_error(base, "/api/reach/people/p_hi/draft", "POST",
                                         {"lang": "fr", "fact": "votre talk"})
            self.assertEqual(code, 409)
            connection = sqlite3.connect(str(db_path))
            connection.execute("INSERT INTO contacts (id, name, source_json, created_at, updated_at)"
                               " VALUES ('c_1', 'Omar', '{}', 'x', 'x')")
            connection.execute("UPDATE people_candidates SET promoted_contact_id = 'c_1' WHERE id = 'p_ok'")
            connection.commit()
            connection.close()
            code, _ = self.request_error(base, "/api/reach/people/p_ok/draft", "POST", {"lang": "de", "fact": "x"})
            self.assertEqual(code, 400)
            status, body = self.request_json(base, "/api/reach/people/p_ok/draft", "POST",
                                             {"lang": "fr", "fact": "votre talk"})
            self.assertEqual(status, 201)
            self.assertEqual(body["draft_id"], "d_1")
            self.assertIn("votre talk", body["body"])
            self.assertEqual(calls, [("p_ok", "fr", "votre talk", "linkedin")])
            connection = sqlite3.connect(str(db_path))
            self.assertEqual(connection.execute("SELECT status FROM drafts WHERE id='d_1'").fetchone()[0],
                             "draft_not_opened")
            connection.close()

    # D3 ---------------------------------------------------------------
    def seed_opportunities(self, db_path):
        connection = sqlite3.connect(str(db_path))
        rows = [
            # The radar stores internship|job in job_type (opportunities.role_kind is
            # CHECK-constrained to role_family|exact_vacancy and owned by the classifier).
            ("opp_a", "AI Engineer", "Acme", "Casablanca", "2026-09-01", "job", "ai_engineer",
             '{"radar": "morocco_ai_cloud", "role_kind": "job"}'),
            ("opp_b", "Data Engineer", "Globex", "Rabat", None, "internship", "data_engineer",
             '{"radar": "morocco_ai_cloud", "role_kind": "internship"}'),
            ("opp_c", "AI Engineer", "Other", "Paris", "2026-09-02", "job", "ai_engineer", '{}'),
        ]
        for oid, title, company, location, pub, _kind, family, source_json in rows:
            connection.execute(
                "INSERT INTO opportunities (id, title, company, location, url, source, publication_date, role_kind,"
                " role_family, fit_score, eligibility_status, freshness_status, verification_confidence,"
                " priority_score, score_schema_version, score_breakdown_json, match_score, status, source_json,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, 'https://x/' || ?, 'test', ?, 'exact_vacancy', ?,"
                " 50, 'unknown', 'unknown', 0, 50, 1, '{}', 50, 'new', ?, 'x', 'x')",
                (oid, title, company, location, oid, pub, family, source_json))
        connection.commit()
        connection.close()

    def test_jobs_only_radar_rows_with_filters_and_ordering(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path, base = self.start_server(directory)
            self.seed_opportunities(db_path)
            status, rows = self.request_json(base, "/api/reach/jobs")
            self.assertEqual(status, 200)
            self.assertEqual([r["id"] for r in rows], ["opp_a", "opp_b"])
            self.assertEqual(set(rows[0]), {"id", "title", "company", "location", "url", "source",
                                            "publication_date", "role_kind", "role_family", "status"})
            _, rows = self.request_json(base, "/api/reach/jobs?kind=internship")
            self.assertEqual([r["id"] for r in rows], ["opp_b"])
            _, rows = self.request_json(base, "/api/reach/jobs?family=ai_engineer")
            self.assertEqual([r["id"] for r in rows], ["opp_a"])
            _, rows = self.request_json(base, "/api/reach/jobs?q=rabat")
            self.assertEqual([r["id"] for r in rows], ["opp_b"])
            _, rows = self.request_json(base, "/api/reach/jobs?q=nothing")
            self.assertEqual(rows, [])



    # D4 ---------------------------------------------------------------
    def test_run_stage_lifecycle_and_guards(self):
        import time
        import reach.api as reach_api

        calls = []

        def fake_runner(db_path, payload):
            calls.append(payload)
            time.sleep(0.2)
            return {"inserted": 1}

        with tempfile.TemporaryDirectory() as directory:
            db_path, base = self.start_server(directory)
            with unittest.mock.patch.dict(reach_api.STAGE_RUNNERS, {"radar": fake_runner}, clear=False):
                code, body = self.request_error(base, "/api/reach/run", "POST", {"stage": "linkedin"})
                self.assertEqual(code, 400)
                status, body = self.request_json(base, "/api/reach/run", "POST", {"stage": "radar"})
                self.assertEqual(status, 202)
                run_id = body["run_id"]
                code, _ = self.request_error(base, "/api/reach/run", "POST", {"stage": "radar"})
                self.assertEqual(code, 429)
                deadline = time.time() + 3
                row = None
                while time.time() < deadline:
                    connection = sqlite3.connect(str(db_path))
                    connection.row_factory = sqlite3.Row
                    row = dict(connection.execute(
                        "SELECT * FROM automation_runs WHERE id = ?", (run_id,)
                    ).fetchone())
                    connection.close()
                    if row["status"] != "running":
                        break
                    time.sleep(0.05)
            self.assertEqual(row["run_type"], "reach_radar")
            self.assertEqual(row["status"], "ok")
            self.assertIsNotNone(row["finished_at"])
            self.assertEqual(json.loads(row["details"])["inserted"], 1)
            self.assertEqual(calls, [{"stage": "radar"}])
            status, runs = self.request_json(base, "/api/reach/runs")
            self.assertEqual(status, 200)
            self.assertEqual([r["id"] for r in runs], [run_id])

    def test_stage_runners_never_include_linkedin(self):
        import reach.api as reach_api

        self.assertEqual(set(reach_api.STAGE_RUNNERS), {"radar", "people_public", "emails"})

    def test_people_public_stage_uses_search_and_reader_channels(self):
        import reach.api as api
        seen = {}

        def fake_discover(conn, target_id, company, search_fn, read_fn, **kw):
            seen["target_id"], seen["company"] = target_id, company
            seen["search"] = search_fn("Deloitte Maroc recruiter")
            seen["read"] = read_fn("https://www2.deloitte.com/ma/fr/careers.html")
            return ["pc_1", "pc_2"]

        fake_search = lambda q: [{"url": "https://www.linkedin.com/in/x", "title": "X", "snippet": "s"}]
        fake_read = lambda url: ("page text", "direct")
        with tempfile.TemporaryDirectory() as directory:
            db_path = make_db(directory)
            connection = sqlite3.connect(str(db_path))
            connection.execute("INSERT INTO target_companies (id, name, intent, priority, created_at, updated_at)"
                               " VALUES ('tgt_1', 'Deloitte', 'internship', 90, 'x', 'x')")
            connection.commit(); connection.close()
            with mock.patch.object(api, "_discover_public", fake_discover),                  mock.patch.object(api, "_people_search_fn", fake_search),                  mock.patch.object(api, "_read_url", fake_read):
                result = api._run_people_public_stage(db_path, {"target_id": "tgt_1"})
        self.assertEqual(seen["company"], "Deloitte")
        self.assertEqual(seen["target_id"], "tgt_1")
        self.assertEqual(seen["search"][0]["url"], "https://www.linkedin.com/in/x")
        self.assertEqual(seen["read"], "page text")
        self.assertEqual(result["inserted"], 2)

    def test_people_public_stage_requires_target_and_reports_search_outage(self):
        import reach.api as api
        with tempfile.TemporaryDirectory() as directory:
            db_path = make_db(directory)
            with self.assertRaises(ValueError):
                api._run_people_public_stage(db_path, {})
            with mock.patch.object(api, "_search_available", lambda: False):
                with self.assertRaises(RuntimeError) as ctx:
                    api._run_people_public_stage(db_path, {"target_id": "tgt_missing"})
        self.assertIn("mcporter", str(ctx.exception))

    def test_emails_stage_counts_tiers_and_requires_target(self):
        import reach.api as api
        with tempfile.TemporaryDirectory() as directory:
            db_path = make_db(directory)
            connection = sqlite3.connect(str(db_path))
            for col in ("email_status TEXT DEFAULT 'none'", "email_evidence_url TEXT", "email_checked_at TEXT"):
                connection.execute(f"ALTER TABLE people_candidates ADD COLUMN {col}")
            connection.execute("INSERT INTO target_companies (id, name, intent, priority, created_at, updated_at)"
                               " VALUES ('tgt_1', 'Deloitte', 'internship', 90, 'x', 'x')")
            for cid, name, score in (("pc_a", "A One", 10), ("pc_b", "B Two", 90), ("pc_c", "C Three", 50)):
                connection.execute("INSERT INTO people_candidates (id, target_company_id, name, score, created_at, updated_at)"
                                   " VALUES (?, 'tgt_1', ?, ?, 'x', 'x')", (cid, name, score))
            connection.commit(); connection.close()
            order = []
            statuses = iter(["found_official", "inferred", "none"])

            def fake_find(conn, cand, search_fn, read_fn, verify_fn):
                order.append(cand["id"])
                return {"email_status": next(statuses)}

            with self.assertRaises(ValueError):
                api._run_emails_stage(db_path, {})
            with mock.patch.object(api, "_find_email", fake_find), mock.patch.object(api, "_sleep", lambda s: None):
                result = api._run_emails_stage(db_path, {"target_id": "tgt_1"})
        self.assertEqual(order, ["pc_b", "pc_c", "pc_a"])  # score desc
        self.assertEqual(result["checked"], 3)
        self.assertEqual(result["found_official"], 1)
        self.assertEqual(result["inferred"], 1)
        self.assertEqual(result["none"], 1)
        self.assertEqual(result["found_public"], 0)
        self.assertEqual(result["rejected"], 0)

    def test_emails_run_is_accepted_over_http(self):
        import time
        import reach.api as reach_api
        with tempfile.TemporaryDirectory() as directory:
            db_path, base = self.start_server(directory)
            with unittest.mock.patch.dict(reach_api.STAGE_RUNNERS, {"emails": lambda db, p: {"checked": 0}}, clear=False):
                code, _ = self.request_error(base, "/api/reach/run", "POST", {"stage": "emails"})
                self.assertEqual(code, 400)
                status, body = self.request_json(base, "/api/reach/run", "POST", {"stage": "emails", "target_id": "tgt_1"})
                self.assertEqual(status, 202)
                self.assertTrue(body["run_id"])
                deadline = time.time() + 3
                while time.time() < deadline:
                    _, runs = self.request_json(base, "/api/reach/runs")
                    if runs and runs[0]["status"] != "running":
                        break
                    time.sleep(0.05)
                self.assertEqual(runs[0]["run_type"], "reach_emails")
                self.assertEqual(runs[0]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
