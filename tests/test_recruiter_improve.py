import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pypdf import PdfWriter

import pipeline_v2
import recruiter_agent

KNOWLEDGE = {
    "evidence_linked_skills": {
        "Python": {"strongest_status": "verified", "sources": ["experience.acme_2025.bullet_1"]},
        "Kafka": {"strongest_status": "verified", "sources": ["experience.acme_2025.bullet_2"]},
        "FastAPI": {"strongest_status": "verified", "sources": ["experience.acme_2025.bullet_1"]},
        "PostgreSQL": {"strongest_status": "verified", "sources": ["experience.acme_2025.bullet_2"]},
    },
    "safe_keyword_synonyms": {"equivalents": [{"canonical": "Kafka", "safe_terms": ["Apache Kafka"]}]},
}

CAREER_MASTER = {
    "identity": {"name": "the candidate", "location": "Rabat, Morocco",
                 "email": "you@example.com", "phone": "+351 912345678",
                 "linkedin_handle": "your-linkedin-handle"},
    "data_ai_variant": {"headline": "Data Engineer | Streaming & APIs",
                        "summary": "Data Engineer building pipelines with Python. Experience spans client delivery and team leadership."},
    "experience": [{
        "id": "acme_2025", "company": "Acme", "title": "Data Engineer", "location": "Remote",
        "start_date": "2025-01", "end_date": "2025-12", "selected_for_data_ai": True,
        "bullets": [
            {"statement": "Built FastAPI services in Python for data products.", "technologies": ["Python", "FastAPI"]},
            {"statement": "Operated streaming ingestion and storage.", "technologies": ["PostgreSQL"]},
        ],
    }],
    "projects": [],
    "education": [{"institution": "ENSAH", "degree": "Ing.", "field": "Data Engineering",
                   "start_date": "2024-09", "end_date": "2027-06"}],
    "certifications": [],
}


def fake_renderer(pages=1):
    def renderer(yaml_path: Path, out_dir: Path, language: str):
        out_dir.mkdir(parents=True, exist_ok=True)
        pdf = out_dir / "out.pdf"
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=595, height=842)
        with pdf.open("wb") as handle:
            writer.write(handle)
        document = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        import cv_render

        text = cv_render.yaml_visible_text(document)
        (out_dir / "out.txt").write_text(text, encoding="utf-8")
        return {"pdf_path": str(pdf), "pages": pages, "text_path": str(out_dir / "out.txt")}
    return renderer


class ImprovementLoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "test.sqlite3"
        pipeline_v2.create_schema(self.db_path)
        self.knowledge_path = self.root / "knowledge.yaml"
        self.knowledge_path.write_text(yaml.safe_dump(KNOWLEDGE), encoding="utf-8")
        self.evidence_path = self.root / "evidence_register.yaml"
        self.evidence_path.write_text("version: 1\n", encoding="utf-8")
        self.master_path = self.root / "career_master.yaml"
        self.master_path.write_text(yaml.safe_dump(CAREER_MASTER, allow_unicode=True), encoding="utf-8")

    def _insert_opportunity(self, description, title="Data Engineer"):
        opp_id = "opp_" + "a" * 24
        now = datetime.now(timezone.utc).isoformat()
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            connection.execute(
                """INSERT INTO opportunities(
                       id, title, company, location, url, source, role_kind, role_family,
                       description, requirements, fit_score, eligibility_status,
                       freshness_status, verification_confidence, priority_score,
                       score_schema_version, score_breakdown_json, match_score, status,
                       source_json, created_at, updated_at
                   ) VALUES (?, ?, 'Acme', 'Remote', 'https://a.test/1', 'test',
                             'exact_vacancy', 'data', ?, '', 80, 'eligible', 'active', 90, 80,
                             2, '{"fit_score": 80}', 80, 'shortlisted', '{}', ?, ?)""",
                (opp_id, title, description, now, now),
            )
            connection.commit()
        return opp_id, now

    def _insert_artifact(self, opp_id, profile, cv_text, language="en"):
        out = self.root / "out"
        out.mkdir(exist_ok=True)
        pdf = out / "cv.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        pdf.with_suffix(".txt").write_text(cv_text, encoding="utf-8")
        (out / "source_profiles").mkdir(exist_ok=True)
        (out / "source_profiles" / "cv.yaml").write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")
        pdf.with_suffix(".manifest.json").write_text(json.dumps({
            "page_count": 1, "output_language": language,
            "files": {"source_profile": "out/source_profiles/cv.yaml"},
        }), encoding="utf-8")
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO cv_artifacts(id, opportunity_id, path, label, artifact_type) VALUES (?, ?, ?, '', 'tailored')",
                (pipeline_v2.stable_id("cv", opp_id, "tailored", "out/cv.pdf"), opp_id, "out/cv.pdf"),
            )
            connection.commit()

    def _loop(self, opp_id, renderer=None, max_rounds=3):
        return recruiter_agent.improvement_loop(
            self.db_path, opp_id, max_rounds, root=self.root,
            knowledge_path=self.knowledge_path, evidence_register_path=self.evidence_path,
            career_master_path=self.master_path, renderer=renderer or fake_renderer(1),
            out_dir=self.root / "improved",
        )

    def test_review_extras_language_layout_citations(self):
        opp_id, version = self._insert_opportunity(
            "Nous recherchons un ingénieur pour la mission avec les équipes: Python et Apache Kafka."
        )
        profile = json.loads(json.dumps(CAREER_MASTER))
        text = ("Jordan you@example.com +351 912345678\n"
                "Data Engineer Python Kafka\nSkills\nPython, Kafka\n")
        self._insert_artifact(opp_id, profile, text, language="en")
        review = recruiter_agent.run_review(
            self.db_path, {"opportunity_id": opp_id, "version": version}, root=self.root,
            knowledge_path=self.knowledge_path, evidence_register_path=self.evidence_path,
        )["review"]
        self.assertEqual(review["jd_language"], "fr")
        self.assertTrue(review["language_mismatch"])
        self.assertTrue(review["layout_findings"]["standalone_skills_section"])
        self.assertFalse(review["layout_findings"]["two_column_layout"])
        self.assertTrue(any("Standalone skills" in f for f in review["red_flags"]))
        self.assertTrue(any("Language mismatch" in f for f in review["red_flags"]))
        self.assertTrue(review["evidence_citations"])
        sources = " ".join(c["source"] for c in review["evidence_citations"])
        self.assertIn("experience.acme_2025.bullet_1", sources)
        self.assertIn("experience.acme_2025.bullet_2", sources)
        # two-column detection via wide gaps on most lines
        wide = "\n".join(f"left{i}" + " " * 20 + f"right{i}" for i in range(10))
        findings = recruiter_agent._layout_findings(wide, {"pdf": self.root / "x.pdf"}, {}, self.root)
        self.assertTrue(findings["two_column_layout"])

    def test_loop_gains_then_stops_on_no_gain(self):
        opp_id, _ = self._insert_opportunity("Python, Apache Kafka, FastAPI and PostgreSQL required")
        profile = json.loads(json.dumps(CAREER_MASTER))
        # Kafka is evidence-backed but absent from the CV -> loop should surface it.
        text = recruiter_agent.profile_visible_text(profile)
        self.assertNotIn("Kafka", text)
        self._insert_artifact(opp_id, profile, text)
        result = self._loop(opp_id)
        self.assertEqual(result["rounds"][0]["ats_before"], 75.0)
        self.assertEqual(result["rounds"][0]["ats_after"], 100.0)
        self.assertTrue(result["rounds"][0]["accepted"])
        self.assertEqual(result["best_round"], 1)
        self.assertIn(result["stopped_reason"], ("no_edits_proposed", "no_ats_gain"))
        self.assertLessEqual(len(result["rounds"]), 2)
        self.assertTrue(any(e["type"] == "surface_skill" and e["skill"] == "Kafka" for e in result["rounds"][0]["edits"]))
        self.assertEqual(result["rounds"][0]["pages"], 1)
        self.assertEqual(result["artifact"]["path"], result["rounds"][0]["pdf_path"])
        self.assertIn("improved", result["artifact"]["label"])
        stored = recruiter_agent.improvements_for_opportunity(self.db_path, opp_id)
        self.assertEqual(stored["best_round"], 1)
        self.assertEqual(stored["rounds"][0]["edits"][0]["type"], result["rounds"][0]["edits"][0]["type"])
        # Rerun: new baseline already covers everything -> no gain / no edits, nothing accepted.
        again = self._loop(opp_id)
        self.assertIsNone(again["best_round"])

    def test_renderer_unavailable_still_reviews_yaml_text(self):
        opp_id, _ = self._insert_opportunity("Python, Apache Kafka, FastAPI and PostgreSQL required")
        profile = json.loads(json.dumps(CAREER_MASTER))
        self._insert_artifact(opp_id, profile, recruiter_agent.profile_visible_text(profile))
        result = self._loop(opp_id, renderer=lambda y, o, l: None)
        self.assertIsNone(result["rounds"][0]["pdf_path"])
        self.assertEqual(result["rounds"][0]["ats_after"], 100.0)
        self.assertTrue(result["rounds"][0]["accepted"])
        # No PDF -> artifact is not replaced.
        self.assertEqual(result["artifact"]["path"], "out/cv.pdf")

    def test_truthfulness_guard_rejects_invented_tokens_and_seniority(self):
        corpus = recruiter_agent.evidence_corpus_tokens(CAREER_MASTER, KNOWLEDGE)
        ok = recruiter_agent.truthfulness_check("Built FastAPI", "Built FastAPI with Python", corpus)
        self.assertTrue(ok["ok"])
        bad = recruiter_agent.truthfulness_check("Built FastAPI", "Built FastAPI with Kubernetes", corpus)
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["invented_tokens"], ["kubernetes"])
        senior = recruiter_agent.truthfulness_check("Engineer", "Senior Engineer", corpus | {"senior"})
        self.assertFalse(senior["ok"])
        # Loop-level: an edit that injects an unevidenced token halts the loop.
        opp_id, _ = self._insert_opportunity("Python, Apache Kafka required")
        profile = json.loads(json.dumps(CAREER_MASTER))
        self._insert_artifact(opp_id, profile, recruiter_agent.profile_visible_text(profile))
        original = recruiter_agent.propose_edits

        def poisoned(profile, opportunity, review, corpus_tokens):
            edited, edits = original(profile, opportunity, review, corpus_tokens)
            edited["data_ai_variant"]["summary"] += " Expert in Terraform."
            edits.append({"type": "poison"})
            return edited, edits
        recruiter_agent.propose_edits = poisoned
        try:
            result = self._loop(opp_id)
        finally:
            recruiter_agent.propose_edits = original
        self.assertEqual(result["stopped_reason"], "truthfulness_failed")
        self.assertIn("terraform", result["rounds"][0]["truthfulness"]["invented_tokens"])
        self.assertIsNone(result["best_round"])
        self.assertEqual(result["artifact"]["path"], "out/cv.pdf")

    def test_title_line_never_adopts_seniority_or_unevidenced_title(self):
        opp_id, _ = self._insert_opportunity("Python required", title="Senior Data Engineer")
        profile = json.loads(json.dumps(CAREER_MASTER))
        corpus = recruiter_agent.evidence_corpus_tokens(CAREER_MASTER, KNOWLEDGE) | {"senior"}
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            opportunity = dict(connection.execute("SELECT * FROM opportunities WHERE id=?", (opp_id,)).fetchone())
        review = {"requirement_evidence": {"matched_requirements": [{"canonical_skill": "Python", "vacancy_term": "Python", "evidence_sources": []}]}}
        edited, edits = recruiter_agent.propose_edits(profile, opportunity, review, corpus)
        self.assertFalse(any(e["type"] == "title_line" for e in edits))
        self.assertEqual(edited["data_ai_variant"]["headline"], profile["data_ai_variant"]["headline"])

    def test_migration_v8_table_idempotent(self):
        pipeline_v2.create_schema(self.db_path)
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("cv_improvement_rounds", tables)
            # v8 introduced this table; later workstreams bump the version (v9-v11).
            self.assertGreaterEqual(connection.execute("PRAGMA user_version").fetchone()[0], 8)

    def test_http_improve_endpoints(self):
        opp_id, version = self._insert_opportunity("Python, Apache Kafka, FastAPI and PostgreSQL required")
        profile = json.loads(json.dumps(CAREER_MASTER))
        self._insert_artifact(opp_id, profile, recruiter_agent.profile_visible_text(profile))
        # Route through fake renderer + temp truth sources by patching module defaults.
        original = recruiter_agent.improvement_loop

        def patched(db_path, opportunity_id, max_rounds=3, **kwargs):
            kwargs.update(knowledge_path=self.knowledge_path, evidence_register_path=self.evidence_path,
                          career_master_path=self.master_path, renderer=fake_renderer(1),
                          out_dir=self.root / "improved")
            return original(db_path, opportunity_id, max_rounds, **kwargs)
        recruiter_agent.improvement_loop = patched
        self.addCleanup(setattr, recruiter_agent, "improvement_loop", original)

        server = pipeline_v2.make_server(self.db_path, self.root, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        def request(path, method="GET", payload=None, origin=None):
            headers = {"Content-Type": "application/json"}
            if origin:
                headers["Origin"] = origin
            data = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request("/api/recruiter/improve", "POST", {"opportunity_id": opp_id, "version": version}, origin="https://evil.example")
        self.assertEqual(ctx.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request("/api/recruiter/improve", "POST", {"opportunity_id": opp_id, "version": "stale"})
        self.assertEqual(ctx.exception.code, 409)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request("/api/recruiter/improve", "POST", {"opportunity_id": opp_id})
        self.assertEqual(ctx.exception.code, 400)
        # 404: opportunity without CV
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            connection.execute("DELETE FROM cv_artifacts WHERE opportunity_id=?", (opp_id,))
            connection.commit()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request("/api/recruiter/improve", "POST", {"opportunity_id": opp_id, "version": version})
        self.assertEqual(ctx.exception.code, 404)
        self._insert_artifact(opp_id, profile, recruiter_agent.profile_visible_text(profile))

        status, body = request("/api/recruiter/improve", "POST",
                               {"opportunity_id": opp_id, "version": version, "max_rounds": 2},
                               origin=f"http://127.0.0.1:{server.server_port}")
        self.assertEqual(status, 201)
        self.assertEqual(body["best_round"], 1)
        self.assertTrue(body["rounds"][0]["edits"])
        self.assertTrue(body["artifact"]["path"].endswith(".pdf"))

        status, stored = request(f"/api/recruiter/improvements/{opp_id}")
        self.assertEqual(status, 200)
        self.assertEqual(stored["best_round"], 1)
        self.assertEqual(stored["rounds"][0]["ats_after"], 100.0)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request("/api/recruiter/improvements/opp_" + "9" * 24)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
