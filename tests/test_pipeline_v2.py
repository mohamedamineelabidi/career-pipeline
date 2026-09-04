import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from unittest import mock

import pipeline_v2


class PipelineV2Tests(unittest.TestCase):
    def write_digest(self, directory):
        digest = {
            "updated": "2026-08-29 21:54",
            "jobs": [
                {
                    "title": "AI Engineer",
                    "company": "Acme",
                    "location": "Rabat",
                    "link": "https://example.test/jobs/42",
                    "source": "official",
                    "summary": "Build and deploy production AI services.",
                    "requirements": "Python, SQL, Docker and three years experience.",
                    "match": 8.5,
                    "eligibility_status": "eligible",
                    "freshness_status": "active",
                    "verification_status": "verified_official_source",
                    "status": "To apply",
                    "cv": "base.pdf",
                    "tailored_cv": "tailored.pdf",
                }
            ],
            "people": [
                {
                    "name": "Rita Recruiter",
                    "company": "Acme",
                    "role": "Recruiter",
                    "profile": "https://linkedin.test/in/rita",
                    "email": "rita@example.test",
                    "verification_status": "verified_official_email",
                    "related_job_link": "https://example.test/jobs/42",
                }
            ],
            "messages": [
                {
                    "target": "Rita Recruiter",
                    "company": "Acme",
                    "linked_job": "https://example.test/jobs/42",
                    "channel": "email",
                    "subject": "AI Engineer",
                    "text": "Hello Rita",
                    "status": "draft_not_opened",
                    "created_at": "2026-08-29T12:00:00+01:00",
                }
            ],
        }
        path = Path(directory) / "jobs_digest.json"
        path.write_text(json.dumps(digest), encoding="utf-8")
        return path

    def test_migration_cli_backs_up_source_before_write_and_validates_integrity(self):
        import migrate_pipeline_v2

        with tempfile.TemporaryDirectory() as directory:
            source = self.write_digest(directory)
            db_path = Path(directory) / "pipeline.sqlite3"
            observed_backups = []
            real_migrate = pipeline_v2.migrate

            def migration_probe(source_path, target_path):
                observed_backups.extend(Path(directory).glob("jobs_digest.*.bak.json"))
                return real_migrate(source_path, target_path)

            with mock.patch.object(migrate_pipeline_v2.pipeline_v2, "migrate", side_effect=migration_probe):
                result = migrate_pipeline_v2.migrate_with_backup(source, db_path)
            self.assertTrue(observed_backups)
            self.assertEqual(result["opportunities"], 1)
            report = migrate_pipeline_v2.validate_integrity(db_path)
            self.assertTrue(report["ok"])
            self.assertEqual(report["counts"]["opportunities"], 1)
            self.assertEqual(report["errors"], [])

    def request_json(self, base_url, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, dict(response.headers), json.loads(response.read())

    def test_localhost_api_contract_and_manual_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_digest(directory)
            db_path = Path(directory) / "pipeline.sqlite3"
            pipeline_v2.migrate(source, db_path)
            server = pipeline_v2.make_server(db_path, Path(__file__).parents[1], port=0)
            self.assertEqual(server.server_address[0], "127.0.0.1")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                for endpoint in ("summary", "opportunities", "contacts", "drafts", "funnel", "health"):
                    status, headers, body = self.request_json(base_url, f"/api/{endpoint}")
                    self.assertEqual(status, 200)
                    self.assertNotIn("Access-Control-Allow-Origin", headers)
                    self.assertIsNotNone(body)
                _, _, opportunities = self.request_json(base_url, "/api/opportunities")
                _, _, drafts = self.request_json(base_url, "/api/drafts")
                _, _, summary = self.request_json(base_url, "/api/summary")
                _, _, contacts = self.request_json(base_url, "/api/contacts")
                _, _, funnel = self.request_json(base_url, "/api/funnel")
                self.assertTrue({"actionable", "drafts_ready", "contacts_verified", "cvs_ready", "run_health"} <= set(summary))
                self.assertTrue({"score", "cv_path", "cv_status", "role_kind", "updated_at"} <= set(opportunities[0]))
                self.assertTrue({"verification_status", "channel", "evidence_url"} <= set(contacts[0]))
                self.assertTrue({"recipient", "verification_status", "updated_at"} <= set(drafts[0]))
                self.assertIsInstance(funnel, list)
                self.assertTrue(all({"stage", "count"} <= set(stage) for stage in funnel))
                opportunity_id = opportunities[0]["id"]
                draft_id = drafts[0]["id"]
                status, _, changed_opportunity = self.request_json(
                    base_url,
                    f"/api/opportunities/{opportunity_id}",
                    "PATCH",
                    {"status": "shortlisted", "version": opportunities[0]["updated_at"]},
                )
                self.assertEqual(status, 200)
                self.assertEqual(changed_opportunity["status"], "shortlisted")
                with closing(pipeline_v2.connect(db_path)) as connection:
                    lifecycle = connection.execute(
                        "SELECT entity_type, entity_id, from_status, to_status FROM lifecycle_events"
                    ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in lifecycle],
                    [("opportunity", opportunity_id, "eligible", "shortlisted")],
                )
                with self.assertRaises(urllib.error.HTTPError) as conflict:
                    self.request_json(
                        base_url,
                        f"/api/opportunities/{opportunity_id}",
                        "PATCH",
                        {"status": "eligible", "version": opportunities[0]["updated_at"]},
                    )
                self.assertEqual(conflict.exception.code, 409)
                status, _, applied = self.request_json(
                    base_url,
                    f"/api/opportunities/{opportunity_id}",
                    "PATCH",
                    {"status": "user_applied", "confirmed_by_user": True, "version": changed_opportunity["updated_at"]},
                )
                self.assertEqual(status, 200)
                self.assertEqual(applied["status"], "user_applied")
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    self.request_json(
                        base_url,
                        f"/api/drafts/{draft_id}",
                        "PATCH",
                        {"status": "sent_by_user"},
                    )
                self.assertEqual(rejected.exception.code, 400)
                status, _, reviewed = self.request_json(
                    base_url,
                    f"/api/drafts/{draft_id}",
                    "PATCH",
                    {"status": "reviewed", "version": drafts[0]["updated_at"]},
                )
                self.assertEqual(status, 200)
                status, _, approved = self.request_json(
                    base_url,
                    f"/api/drafts/{draft_id}",
                    "PATCH",
                    {"status": "approved_by_user", "version": reviewed["updated_at"]},
                )
                self.assertEqual(status, 200)
                status, _, draft = self.request_json(
                    base_url,
                    f"/api/drafts/{draft_id}",
                    "PATCH",
                    {"status": "sent_by_user", "confirmed_by_user": True, "version": approved["updated_at"]},
                )
                self.assertEqual(status, 200)
                self.assertEqual(draft["status"], "sent_by_user")
                with closing(pipeline_v2.connect(db_path)) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0], 1)
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM outreach_events WHERE event_type='message_sent'").fetchone()[0], 1
                    )
                status, _, outcome = self.request_json(
                    base_url,
                    "/api/outcomes",
                    "POST",
                    {
                        "opportunity_id": opportunity_id,
                        "draft_id": draft_id,
                        "event_type": "reply_received",
                        "notes": "Manual update",
                    },
                )
                self.assertEqual(status, 201)
                self.assertEqual(outcome["event_type"], "reply_received")
                status, _, screening = self.request_json(
                    base_url, "/api/outcomes", "POST",
                    {"opportunity_id": opportunity_id, "event_type": "screening", "notes": "Manual update"},
                )
                self.assertEqual(status, 201)
                self.assertEqual(screening["event_type"], "screening")
                with closing(pipeline_v2.connect(db_path)) as connection:
                    event_count = connection.execute("SELECT COUNT(*) FROM outreach_events").fetchone()[0]
                malicious = urllib.request.Request(
                    base_url + "/api/outcomes",
                    data=json.dumps({"event_type": "offer", "notes": "forged"}).encode("utf-8"),
                    headers={"Content-Type": "text/plain", "Origin": "https://evil.example"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(malicious, timeout=5)
                self.assertEqual(rejected.exception.code, 403)
                with closing(pipeline_v2.connect(db_path)) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM outreach_events").fetchone()[0], event_count)
                _, _, funnel_after = self.request_json(base_url, "/api/funnel")
                self.assertEqual(
                    [stage["stage"] for stage in funnel_after],
                    ["discovered", "verified_active", "eligible", "shortlisted", "approved_by_user", "user_applied", "response_received", "screening", "interview", "offer", "rejection"],
                )
                with urllib.request.urlopen(base_url + "/pipeline_v2.html", timeout=3) as response:
                    self.assertEqual(response.status, 200)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_stable_ids_are_immutable_in_database(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_digest(directory)
            db_path = Path(directory) / "pipeline.sqlite3"
            pipeline_v2.migrate(source, db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                opportunity_id = connection.execute("SELECT id FROM opportunities").fetchone()[0]
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE opportunities SET id = ? WHERE id = ?",
                        ("opp_rewritten", opportunity_id),
                    )

    def test_update_rules_reject_invalid_opportunity_and_unconfirmed_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_digest(directory)
            db_path = Path(directory) / "pipeline.sqlite3"
            pipeline_v2.migrate(source, db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                opportunity_id = connection.execute("SELECT id FROM opportunities").fetchone()[0]
                draft_id, draft_version = connection.execute("SELECT id, updated_at FROM drafts").fetchone()
            with self.assertRaises(pipeline_v2.ValidationError):
                pipeline_v2.update_opportunity(db_path, opportunity_id, {"status": "sent"})
            with self.assertRaises(pipeline_v2.ValidationError):
                pipeline_v2.update_opportunity(db_path, opportunity_id, {"status": "user_applied"})
            with self.assertRaises(pipeline_v2.ValidationError):
                pipeline_v2.update_draft(db_path, draft_id, {"status": "sent"})
            with self.assertRaises(pipeline_v2.ValidationError):
                pipeline_v2.update_draft(db_path, draft_id, {"status": "sent_by_user"})
            with self.assertRaises(pipeline_v2.ValidationError):
                pipeline_v2.update_draft(
                    db_path, draft_id,
                    {"status": "sent_by_user", "confirmed_by_user": True, "version": draft_version},
                )
            reviewed = pipeline_v2.update_draft(
                db_path, draft_id, {"status": "reviewed", "version": draft_version},
            )
            approved = pipeline_v2.update_draft(
                db_path, draft_id, {"status": "approved_by_user", "version": reviewed["updated_at"]},
            )
            updated = pipeline_v2.update_draft(
                db_path, draft_id,
                {"status": "sent_by_user", "confirmed_by_user": True, "version": approved["updated_at"]},
            )
            self.assertEqual(updated["status"], "sent_by_user")

    def test_score_breakdown_and_hard_lifecycle_gates(self):
        schema_v2_low = pipeline_v2.compute_opportunity_score({
            "fit_score": 8, "verification_confidence": 8, "score_schema_version": 2,
            "source_verification_status": "verified_official_source",
        })
        self.assertEqual(schema_v2_low["fit_score"], 8)
        self.assertEqual(schema_v2_low["verification_confidence"], 8)
        adversarial = pipeline_v2.compute_opportunity_score({
            "verification_status": "unverified_official_source",
        })
        self.assertEqual(adversarial["verification_confidence"], 0)
        self.assertNotEqual(
            pipeline_v2.map_opportunity_status({"status": "Not applied"}), "user_applied"
        )
        eligible = pipeline_v2.compute_opportunity_score({
            "match": 8.5,
            "eligibility_status": "eligible",
            "freshness_status": "active",
            "verification_status": "verified_official_source",
        })
        self.assertEqual(eligible["fit_score"], 85)
        self.assertEqual(eligible["score_schema_version"], 2)
        self.assertGreater(eligible["priority_score"], 0)

        blocked = pipeline_v2.compute_opportunity_score({
            "match": 9.8,
            "eligibility_status": "blocked",
            "freshness_status": "stale",
            "verification_status": "verified_official_source",
        })
        self.assertEqual(blocked["priority_score"], 0)
        self.assertEqual(blocked["archive_reason"], "eligibility_blocked")

        with tempfile.TemporaryDirectory() as directory:
            source = self.write_digest(directory)
            payload = json.loads(source.read_text(encoding="utf-8"))
            payload["jobs"].append({
                "title": "Blocked role", "company": "Acme", "link": "https://example.test/blocked",
                "match": 10, "status": "priority", "eligibility_status": "blocked",
                "freshness_status": "stale", "verification_status": "verified_official_source",
            })
            source.write_text(json.dumps(payload), encoding="utf-8")
            db_path = Path(directory) / "pipeline.sqlite3"
            pipeline_v2.migrate(source, db_path)
            with closing(pipeline_v2.connect(db_path)) as connection:
                rows = [dict(row) for row in connection.execute(
                    "SELECT id, title, fit_score, eligibility_status, freshness_status, "
                    "verification_confidence, priority_score, score_schema_version, "
                    "score_breakdown_json, status, archive_reason FROM opportunities ORDER BY title"
                )]
            blocked_row = next(row for row in rows if row["title"] == "Blocked role")
            self.assertEqual(blocked_row["priority_score"], 0)
            self.assertEqual(blocked_row["status"], "closed")
            self.assertEqual(blocked_row["archive_reason"], "eligibility_blocked")
            self.assertEqual(blocked_row["score_schema_version"], 2)
            self.assertIn("fit_score", json.loads(blocked_row["score_breakdown_json"]))
            with self.assertRaises(pipeline_v2.ValidationError):
                pipeline_v2.update_opportunity(db_path, blocked_row["id"], {"status": "shortlisted"})

    def test_email_lint_contact_verification_and_company_collision_controls(self):
        good = pipeline_v2.lint_draft(
            "email", "AI Engineer opportunity", "Hello Rita,\n\nI am writing about the AI Engineer role. Would a brief reply by email be possible?\n\nBest regards,\nMohamed"
        )
        bad = pipeline_v2.lint_draft(
            "email", "URGENT!!! FREE OFFER!!!", "Click now https://a.test https://b.test BUY NOW!!!"
        )
        self.assertEqual(good["status"], "pass")
        self.assertEqual(bad["status"], "fail")
        self.assertTrue(bad["errors"])

        with tempfile.TemporaryDirectory() as directory:
            source = self.write_digest(directory)
            payload = json.loads(source.read_text(encoding="utf-8"))
            payload["people"].append({
                "name": "Alex Recruiter", "company": "Acme", "email": "alex@example.test",
                "verification_status": "verified_public_professional_email",
            })
            payload["messages"].append({
                "target": "Alex Recruiter", "company": "Acme", "channel": "email",
                "subject": "Another role", "text": "Hello Alex, would a reply by email be possible?",
                "status": "draft_not_opened", "created_at": "2026-08-29T13:00:00+01:00",
            })
            source.write_text(json.dumps(payload), encoding="utf-8")
            db_path = Path(directory) / "pipeline.sqlite3"
            pipeline_v2.migrate(source, db_path)
            drafts = pipeline_v2.api_data(db_path, "drafts")
            self.assertTrue(all("lint" in draft for draft in drafts))
            first, second = drafts
            first_reviewed = pipeline_v2.update_draft(
                db_path, first["id"], {"status": "reviewed", "version": first["updated_at"]}
            )
            pipeline_v2.update_draft(
                db_path, first["id"],
                {"status": "approved_by_user", "version": first_reviewed["updated_at"]},
            )
            second_reviewed = pipeline_v2.update_draft(
                db_path, second["id"], {"status": "reviewed", "version": second["updated_at"]}
            )
            with self.assertRaises(pipeline_v2.ValidationError):
                pipeline_v2.update_draft(
                    db_path, second["id"],
                    {"status": "approved_by_user", "version": second_reviewed["updated_at"]},
                )

    def test_migration_links_contacts_drafts_and_cv_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_digest(directory)
            db_path = Path(directory) / "pipeline.sqlite3"
            pipeline_v2.migrate(source, db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                opportunity_id = connection.execute(
                    "SELECT id FROM opportunities"
                ).fetchone()[0]
                role_kind = connection.execute(
                    "SELECT role_kind FROM opportunities"
                ).fetchone()[0]
                contact = connection.execute(
                    "SELECT id FROM contacts"
                ).fetchone()
                routes = connection.execute(
                    "SELECT route_type, value FROM contact_routes ORDER BY route_type"
                ).fetchall()
                draft = connection.execute(
                    "SELECT opportunity_id, contact_id, status FROM drafts"
                ).fetchone()
                artifacts = connection.execute(
                    "SELECT opportunity_id, artifact_type FROM cv_artifacts ORDER BY artifact_type"
                ).fetchall()
            self.assertEqual(role_kind, "role_family")
            self.assertIsNotNone(contact)
            self.assertEqual(routes, [("email", "rita@example.test"), ("linkedin", "https://linkedin.test/in/rita")])
            self.assertEqual(draft, (opportunity_id, contact[0], "draft_local"))
            self.assertEqual(artifacts, [(opportunity_id, "base"), (opportunity_id, "tailored")])

    def test_migration_is_idempotent_with_stable_ids_and_normalized_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_digest(directory)
            db_path = Path(directory) / "pipeline.sqlite3"
            first = pipeline_v2.migrate(source, db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                first_id = connection.execute(
                    "SELECT id FROM opportunities"
                ).fetchone()[0]
                score = connection.execute(
                    "SELECT match_score FROM opportunities"
                ).fetchone()[0]
            second = pipeline_v2.migrate(source, db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                second_id = connection.execute(
                    "SELECT id FROM opportunities"
                ).fetchone()[0]
                count = connection.execute(
                    "SELECT COUNT(*) FROM opportunities"
                ).fetchone()[0]
            self.assertEqual(first_id, second_id)
            self.assertEqual(score, 85)
            self.assertEqual(count, 1)
            self.assertEqual(first["opportunities"], 1)
            self.assertEqual(second["opportunities"], 1)

    def test_json_export_is_generated_compatible_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_digest(directory)
            db_path = Path(directory) / "pipeline.sqlite3"
            output = Path(directory) / "jobs_digest_export.json"
            pipeline_v2.migrate(source, db_path)
            # A later freshness-only ingestion must not erase persisted CV links.
            partial = Path(directory) / "partial.json"
            partial.write_text(json.dumps({
                "jobs": [{
                    "title": "AI Engineer",
                    "company": "Acme",
                    "location": "Rabat",
                    "link": "https://example.test/jobs/42",
                    "freshness_status": "active",
                }],
                "people": [],
                "messages": [],
            }), encoding="utf-8")
            pipeline_v2.migrate(partial, db_path)
            (Path(directory) / "tailored.manifest.json").write_text("{}", encoding="utf-8")
            counts = pipeline_v2.export_json_snapshot(db_path, output)
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(exported["generated_read_only"])
            self.assertEqual(exported["score_schema_version"], 2)
            self.assertEqual(len(exported["jobs"]), 1)
            self.assertEqual(exported["jobs"][0]["match"], 85)
            self.assertIn("priority_score", exported["jobs"][0])
            self.assertEqual(len(exported["people"]), 1)
            self.assertEqual(len(exported["messages"]), 1)
            self.assertEqual(exported["jobs"][0]["tailored_cv"], "tailored.pdf")
            self.assertEqual(exported["jobs"][0]["tailoring_manifest"], "tailored.manifest.json")
            self.assertFalse(output.stat().st_mode & 0o200)
            with self.assertRaises(pipeline_v2.ValidationError):
                pipeline_v2.migrate(output, Path(directory) / "forbidden.sqlite3")

    def test_incremental_title_refresh_and_reingest_preserve_identity_and_draft_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_digest(directory)
            db_path = Path(directory) / "pipeline.sqlite3"
            pipeline_v2.migrate(source, db_path)
            draft = pipeline_v2.api_data(db_path, "drafts")[0]
            edited = pipeline_v2.update_draft(
                db_path, draft["id"], {"body": "Locally reviewed body", "version": draft["updated_at"]}
            )
            payload = json.loads(source.read_text(encoding="utf-8"))
            payload["jobs"][0]["title"] = "Senior AI Engineer"
            source.write_text(json.dumps(payload), encoding="utf-8")
            pipeline_v2.migrate(source, db_path)
            with closing(pipeline_v2.connect(db_path)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 1)
                stored = connection.execute(
                    "SELECT body, updated_at FROM drafts WHERE id=?", (draft["id"],)
                ).fetchone()
            self.assertEqual(stored["body"], "Locally reviewed body")
            self.assertEqual(stored["updated_at"], edited["updated_at"])

    def test_confirmed_sent_reimport_reconciles_draft_and_event(self):
        self.assertEqual(pipeline_v2.map_draft_status("not archived"), "draft_local")
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_digest(directory)
            db_path = Path(directory) / "pipeline.sqlite3"
            pipeline_v2.migrate(source, db_path)
            payload = json.loads(source.read_text(encoding="utf-8"))
            payload["messages"][0]["status"] = "sent_by_user"
            payload["messages"][0]["confirmed_by_user"] = True
            source.write_text(json.dumps(payload), encoding="utf-8")
            pipeline_v2.migrate(source, db_path)
            with closing(pipeline_v2.connect(db_path)) as connection:
                self.assertEqual(connection.execute("SELECT status FROM drafts").fetchone()[0], "sent_by_user")
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM outreach_events WHERE event_type='message_sent'").fetchone()[0], 1
                )

    def test_automation_run_health_distinguishes_no_change_and_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_digest(directory)
            db_path = Path(directory) / "pipeline.sqlite3"
            pipeline_v2.migrate(source, db_path)
            first = pipeline_v2.record_automation_run(
                db_path, "daily-scan", "no_change", 0, "No verified eligible roles"
            )
            self.assertEqual(first["status"], "no_change")
            summary = pipeline_v2.api_data(db_path, "summary")
            self.assertEqual(summary["run_health"]["status"], "no_change")
            successful = pipeline_v2.record_automation_run(
                db_path, "daily-scan", "success", 2, "Two verified roles"
            )
            second = pipeline_v2.record_automation_run(
                db_path, "contact-scout", "blocked", 0, "Login wall"
            )
            self.assertEqual(second["status"], "blocked")
            health = pipeline_v2.api_data(db_path, "summary")["run_health"]
            self.assertEqual(health["status"], "blocked")
            self.assertEqual(health["last_success_at"], successful["finished_at"])

    def test_exact_vacancy_requires_an_explicit_substantive_full_job_description(self):
        summary_only = {
            "title": "AI Engineer",
            "summary": "Responsibilities include Python delivery and collaboration.",
            "requirements": "Python, SQL and Docker are required.",
        }
        self.assertEqual(pipeline_v2.classify_opportunity(summary_only), "role_family")

        full_jd = (
            "Responsibilities: design, build, test and operate reliable AI services with product and "
            "engineering teams. Improve retrieval quality, observability, data validation, deployment "
            "automation and incident response. Requirements: professional Python and SQL experience, "
            "API design, Docker, cloud delivery, testing, documentation and clear communication. The "
            "successful candidate will review system performance, investigate failures, collaborate "
            "across functions and deliver maintainable production software while documenting technical "
            "decisions and measurable outcomes for stakeholders."
        )
        exact = dict(summary_only, full_job_description=full_jd)
        self.assertGreaterEqual(len(full_jd), 200)
        self.assertEqual(pipeline_v2.classify_opportunity(exact), "exact_vacancy")

    def test_status_vocabulary_matches_the_audited_pipeline(self):
        self.assertEqual(
            pipeline_v2.OPPORTUNITY_STATUSES,
            frozenset({"discovered", "verified_active", "eligible", "shortlisted", "user_applied", "closed"}),
        )
        self.assertEqual(
            pipeline_v2.DRAFT_STATUSES,
            frozenset({"draft_local", "needs_verification", "reviewed", "approved_by_user", "sent_by_user", "replied", "closed"}),
        )

    def test_create_schema_has_required_tables_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "pipeline.sqlite3"
            pipeline_v2.create_schema(db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertTrue(
                    {
                        "opportunities",
                        "contacts",
                        "contact_routes",
                        "drafts",
                        "cv_artifacts",
                        "applications",
                        "outreach_events",
                        "automation_runs",
                        "lifecycle_events",
                        "metadata",
                    }.issubset(tables)
                )
                foreign_keys = connection.execute(
                    "PRAGMA foreign_key_list(drafts)"
                ).fetchall()
                self.assertTrue(foreign_keys)


if __name__ == "__main__":
    unittest.main()
