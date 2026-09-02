"""Shared fixtures for the Resume-Matcher port tests (temp DB, temp evidence)."""

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

import pipeline_v2

OPP_ID = "opp_" + "a" * 24
INVENTED = "Zorblaxium"  # never appears in any evidence source

CAREER_MASTER = {
    "identity": {"name": "Test Candidate"},
    "targets": {"primary_identity": "Data & AI Engineer"},
    "summary_evidence": ["Data and AI Engineer building RAG applications and data pipelines."],
    "skills": {"data": ["Python", "SQL", "Kafka"], "ai": ["RAG", "FastAPI"]},
    "experience": [
        {
            "id": "acme_2025", "company": "Acme", "title": "Data Engineer",
            "status": "verified", "start_date": "2025-01", "end_date": "present",
            "bullets": [
                {"statement": "Built a streaming pipeline with Kafka and Python that processes ten thousand events per minute.",
                 "evidence_status": "verified", "metrics": ["ten thousand events per minute"],
                 "technologies": ["Python", "Kafka"]},
                {"statement": "Developed FastAPI services backed by PostgreSQL.",
                 "evidence_status": "user_confirmed", "metrics": [], "technologies": ["FastAPI", "PostgreSQL"]},
                {"statement": "Secret rejected claim about Airflow.",
                 "evidence_status": "rejected", "metrics": [], "technologies": ["Airflow"]},
            ],
        }
    ],
    "projects": [
        {"id": "ragproj", "name": "RAG Assistant", "role": "AI Project", "status": "verified",
         "bullets": [{"statement": "Built a RAG document assistant with Qdrant vector search.",
                      "evidence_status": "verified", "metrics": [], "technologies": ["RAG", "Qdrant"]}]}
    ],
    "leadership": [{"organization": "Data Club", "title": "President", "status": "verified",
                    "statement": "Led 15 team leads and trained 300 students.", "metrics": ["15 team leads"]}],
    "certifications": [{"name": "AWS Certified Cloud Practitioner", "issuer": "AWS", "status": "user_confirmed"}],
    "languages": [{"name": "French", "level": "B2"}, {"name": "English", "level": "B2"}],
}
KNOWLEDGE = {"evidence_linked_skills": {"Docker": {"sources": ["projects.ragproj.bullet_1"], "strongest_status": "verified"}}}

JD_EN = (
    "Data Engineer at Acme Corp. We are looking for a Data Engineer with strong Python and Kafka skills. "
    "You will build data pipelines with Apache Kafka and Docker, and deploy Kubernetes workloads. "
    "Experience with Terraform and data pipelines is a plus. Data pipelines are central to the team. "
    f"Knowledge of {INVENTED} is required. You will run quality checks daily; quality checks matter here."
)
JD_FR = (
    "Nous recherchons un Data Engineer pour rejoindre notre équipe à Paris. Vous travaillerez avec Python et Kafka "
    "dans une équipe de mission data. Vous avez des compétences en Docker et une bonne maîtrise des pipelines de données. "
    "Le poste est basé dans nos locaux avec une expérience en Kubernetes appréciée pour la mission."
)


class PortTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "test.sqlite3"
        pipeline_v2.create_schema(self.db_path)
        self.master_path = self.root / "career_master.yaml"
        self.master_path.write_text(yaml.safe_dump(CAREER_MASTER, allow_unicode=True), encoding="utf-8")
        self.evidence_path = self.root / "evidence_register.yaml"
        self.evidence_path.write_text("version: 1\n", encoding="utf-8")
        self.knowledge_path = self.root / "tailoring_knowledge.yaml"
        self.knowledge_path.write_text(yaml.safe_dump(KNOWLEDGE), encoding="utf-8")

    @property
    def sources(self):
        return dict(
            root=self.root, career_master_path=self.master_path,
            evidence_register_path=self.evidence_path, knowledge_path=self.knowledge_path,
        )

    def insert_opportunity(self, opportunity_id=OPP_ID, description=JD_EN, status="shortlisted",
                           eligibility="eligible", freshness="active", confidence=90):
        now = datetime.now(timezone.utc).isoformat()
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            connection.execute(
                """INSERT INTO opportunities(
                       id, title, company, location, url, source, role_kind, role_family,
                       description, requirements, fit_score, eligibility_status,
                       freshness_status, verification_confidence, priority_score,
                       score_schema_version, score_breakdown_json, match_score, status,
                       source_json, created_at, updated_at
                   ) VALUES (?, 'Data Engineer', 'Acme Corp', 'Remote', 'https://a.test/1', 'test',
                             'exact_vacancy', 'data', ?, '', 80, ?, ?, ?, 80,
                             2, '{"fit_score": 80}', 80, ?, '{}', ?, ?)""",
                (opportunity_id, description, eligibility, freshness, confidence, status, now, now),
            )
            connection.commit()
        return opportunity_id, now

    def insert_artifact(self, opportunity_id, cv_text, name="cv_test"):
        pdf = self.root / f"{name}.pdf"
        pdf.write_bytes(b"%PDF-1.4 test")
        pdf.with_suffix(".txt").write_text(cv_text, encoding="utf-8")
        artifact_id = pipeline_v2.stable_id("cv", opportunity_id, "tailored", pdf.name)
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO cv_artifacts(id, opportunity_id, path, label, artifact_type) VALUES (?, ?, ?, '', 'tailored')",
                (artifact_id, opportunity_id, pdf.name),
            )
            connection.commit()
        return artifact_id

    def start_server(self):
        server = pipeline_v2.make_server(self.db_path, self.root, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"
        return self.base

    def request(self, path, method="GET", payload=None, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")
