import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import cv_workspace
import pipeline_v2

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def write_digest(directory):
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
            },
            {
                "title": "Data Engineer",
                "company": "Globex",
                "location": "Casablanca",
                "link": "https://example.test/jobs/77",
                "source": "official",
                "summary": "Streaming pipelines.",
                "requirements": "Kafka, Spark.",
                "match": 7.0,
                "eligibility_status": "eligible",
                "freshness_status": "active",
                "verification_status": "verified_official_source",
                "status": "To apply",
            },
        ],
        "people": [],
        "messages": [],
    }
    path = Path(directory) / "jobs_digest.json"
    path.write_text(json.dumps(digest), encoding="utf-8")
    return path


def make_db(directory):
    source = write_digest(directory)
    db_path = Path(directory) / "pipeline.sqlite3"
    pipeline_v2.migrate(source, db_path)
    return db_path


def opportunity_ids(db_path):
    rows = pipeline_v2.api_data(db_path, "opportunities")
    return {row["company"]: row["id"] for row in rows}


class CvWorkspaceModuleTests(unittest.TestCase):
    def test_list_cvs_includes_opportunity_fields_and_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = make_db(directory)
            rows = cv_workspace.list_cvs(db_path, project_root=Path(directory))
            self.assertEqual(len(rows), 2)
            by_company = {row["company"]: row for row in rows}
            acme = by_company["Acme"]
            self.assertEqual(acme["title"], "AI Engineer")
            self.assertIn(acme["classification"], {"exact_vacancy", "role_family"})
            self.assertIn("status", acme)
            self.assertIn("language", acme)
            self.assertIn("opportunity_id", acme)
            # Acme has an artifact from migration; Globex does not.
            self.assertTrue(acme["artifacts"])
            self.assertFalse(by_company["Globex"]["artifacts"])

    def test_list_cvs_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = make_db(directory)
            rows = cv_workspace.list_cvs(
                db_path, project_root=Path(directory), filters={"company": "acme"}
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["company"], "Acme")
            rows = cv_workspace.list_cvs(
                db_path, project_root=Path(directory), filters={"has_cv": "true"}
            )
            self.assertEqual([row["company"] for row in rows], ["Acme"])

    def test_cv_detail_includes_manifest_and_requirement_report(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = make_db(directory)
            ids = opportunity_ids(db_path)
            root = Path(directory)
            manifest_dir = root / "reference_cv_2027" / "out" / "tailored"
            manifest_dir.mkdir(parents=True)
            manifest = {
                "tailoring_basis": "exact_vacancy",
                "output_language": "en",
                "requirement_evidence": {"matched_requirements": ["python"], "missing_skills": []},
                "files": {"pdf": "reference_cv_2027/out/tailored/x.pdf"},
            }
            manifest_path = manifest_dir / "x.manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            pipeline_v2.register_cv_artifact(
                db_path, ids["Acme"], "reference_cv_2027/out/tailored/x.pdf", "Tailored"
            )
            detail = cv_workspace.cv_detail(db_path, ids["Acme"], project_root=root)
            self.assertEqual(detail["opportunity_id"], ids["Acme"])
            self.assertEqual(detail["classification"], "exact_vacancy")
            self.assertEqual(
                detail["requirement_evidence_report"]["matched_requirements"], ["python"]
            )
            self.assertEqual(detail["language"], "en")

    def test_cv_detail_missing_opportunity_raises_not_found(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = make_db(directory)
            with self.assertRaises(pipeline_v2.NotFoundError):
                cv_workspace.cv_detail(db_path, "nope", project_root=Path(directory))

    def test_generate_missing_opportunity_raises_not_found(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = make_db(directory)
            with self.assertRaises(pipeline_v2.NotFoundError):
                cv_workspace.generate_cv(
                    db_path, {"opportunity_id": "missing", "version": "x"},
                    project_root=Path(directory),
                )

    def test_generate_requires_version_and_valid_language(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = make_db(directory)
            ids = opportunity_ids(db_path)
            with self.assertRaises(pipeline_v2.ValidationError):
                cv_workspace.generate_cv(
                    db_path, {"opportunity_id": ids["Acme"]}, project_root=Path(directory)
                )
            row = cv_workspace.cv_detail(db_path, ids["Acme"], project_root=Path(directory))
            with self.assertRaises(pipeline_v2.ValidationError):
                cv_workspace.generate_cv(
                    db_path,
                    {
                        "opportunity_id": ids["Acme"],
                        "version": row["version"],
                        "language": "klingon",
                    },
                    project_root=Path(directory),
                )

    def test_generate_stale_version_raises_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = make_db(directory)
            ids = opportunity_ids(db_path)
            with self.assertRaises(pipeline_v2.ConflictError):
                cv_workspace.generate_cv(
                    db_path,
                    {"opportunity_id": ids["Acme"], "version": "stale-version"},
                    project_root=Path(directory),
                )

    def test_generate_calls_local_builder_and_registers_one_artifact_per_type(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = make_db(directory)
            root = Path(directory)
            ids = opportunity_ids(db_path)
            row = cv_workspace.cv_detail(db_path, ids["Globex"], project_root=root)

            def fake_builder(job, profile, description_override=None, **kwargs):
                return {
                    "tailoring_basis": "role_family",
                    "output_language": "en",
                    "archetype_display": "Data Engineer",
                    "files": {"pdf": "reference_cv_2027/out/tailored/fake.pdf",
                              "manifest": "reference_cv_2027/out/tailored/fake.manifest.json"},
                }

            with mock.patch.object(cv_workspace, "_load_builder") as loader:
                loader.return_value = (fake_builder, {"identity": {"name": "X"}}, None)
                result = cv_workspace.generate_cv(
                    db_path,
                    {"opportunity_id": ids["Globex"], "version": row["version"]},
                    project_root=root,
                )
                # regenerate: unique index means still one tailored artifact
                row2 = cv_workspace.cv_detail(db_path, ids["Globex"], project_root=root)
                cv_workspace.generate_cv(
                    db_path,
                    {"opportunity_id": ids["Globex"], "version": row2["version"]},
                    project_root=root,
                )
            self.assertEqual(result["artifact"]["opportunity_id"], ids["Globex"])
            detail = cv_workspace.cv_detail(db_path, ids["Globex"], project_root=root)
            tailored = [a for a in detail["artifacts"] if a["artifact_type"] == "tailored"]
            self.assertEqual(len(tailored), 1)

    def test_generate_never_touches_generated_read_only_snapshots(self):
        # persist_jobs_payload must still refuse generated snapshots.
        import sys
        sys.path.insert(0, str(PROJECT_ROOT / "reference_cv_2027" / "scripts"))
        try:
            import tailor_cv_agent
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "snapshot.json"
            written = tailor_cv_agent.persist_jobs_payload(
                target, {"generated_read_only": True, "jobs": []}
            )
            self.assertFalse(written)
            self.assertFalse(target.exists())

    def test_artifact_paths_outside_project_root_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = make_db(directory)
            root = Path(directory)
            ids = opportunity_ids(db_path)
            for bad in ("../outside.pdf", "C:/Windows/evil.pdf", "/etc/passwd"):
                with self.assertRaises(pipeline_v2.ValidationError):
                    cv_workspace.safe_artifact_path(root, bad)
            row = cv_workspace.cv_detail(db_path, ids["Acme"], project_root=root)

            def escaping_builder(job, profile, description_override=None, **kwargs):
                return {"tailoring_basis": "role_family", "output_language": "en",
                        "files": {"pdf": "../escape.pdf"}}

            with mock.patch.object(cv_workspace, "_load_builder") as loader:
                loader.return_value = (escaping_builder, {}, None)
                with self.assertRaises(pipeline_v2.ValidationError):
                    cv_workspace.generate_cv(
                        db_path,
                        {"opportunity_id": ids["Acme"], "version": row["version"]},
                        project_root=root,
                    )


class CvWorkspaceHttpTests(unittest.TestCase):
    def request_json(self, base_url, path, method="GET", payload=None, origin=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(base_url + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def start_server(self, directory):
        db_path = make_db(directory)
        server = pipeline_v2.make_server(db_path, Path(directory), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        return db_path, base

    def test_get_cvs_listing_and_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path, base = self.start_server(directory)
            status, rows = self.request_json(base, "/api/cvs")
            self.assertEqual(status, 200)
            self.assertEqual(len(rows), 2)
            status, filtered = self.request_json(base, "/api/cvs?company=acme")
            self.assertEqual(status, 200)
            self.assertEqual(len(filtered), 1)
            opp = filtered[0]["opportunity_id"]
            status, detail = self.request_json(base, f"/api/cvs/{opp}")
            self.assertEqual(status, 200)
            self.assertEqual(detail["opportunity_id"], opp)
            self.assertIn("requirement_evidence_report", detail)

    def test_get_cv_detail_missing_is_404(self):
        with tempfile.TemporaryDirectory() as directory:
            _, base = self.start_server(directory)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.request_json(base, "/api/cvs/does-not-exist")
            self.assertEqual(ctx.exception.code, 404)

    def test_post_generate_missing_opportunity_is_404_and_cross_origin_403(self):
        with tempfile.TemporaryDirectory() as directory:
            _, base = self.start_server(directory)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.request_json(
                    base, "/api/cvs/generate", method="POST",
                    payload={"opportunity_id": "missing", "version": "x"},
                )
            self.assertEqual(ctx.exception.code, 404)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.request_json(
                    base, "/api/cvs/generate", method="POST",
                    payload={"opportunity_id": "missing", "version": "x"},
                    origin="http://evil.test",
                )
            self.assertEqual(ctx.exception.code, 403)

    def test_post_generate_requires_version(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path, base = self.start_server(directory)
            ids = opportunity_ids(db_path)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.request_json(
                    base, "/api/cvs/generate", method="POST",
                    payload={"opportunity_id": ids["Acme"]},
                )
            self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
