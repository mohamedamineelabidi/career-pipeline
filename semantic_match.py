"""Semantic matching + skill-gap analysis for Career Pipeline v2.

Inspired by abasukanga4/resume-job-matcher and Resume-Matcher, kept local and
deterministic:

* Candidate profile text is built ONLY from ``career_master.yaml`` (+ the
  evidence-backed skill list). No skill is ever inferred from the taxonomy or
  from a job description into the candidate's "have" set.
* Each opportunity whose description is >= MIN_DESCRIPTION_CHARS is embedded
  with ``all-MiniLM-L6-v2`` (sentence-transformers, cached under ``.models/``).
  If the model cannot be loaded (offline, missing dependency) we fall back to a
  deterministic pure-python TF-IDF cosine and record ``model='tfidf-fallback'``.
* ``skills_taxonomy.json`` provides canonical skills + aliases. Skills are
  extracted from both the JD and the candidate profile; ``skills_have`` are JD
  skills the candidate has evidence for, ``skills_missing`` are the rest.
* Results are upserted into ``semantic_scores`` keyed by ``opportunity_id`` and
  are only recomputed when the ``content_hash`` of the JD changes (or when
  ``force=True``).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

import pipeline_v2
from pipeline_v2 import NotFoundError, PathLike, ValidationError, connect

ROOT = Path(__file__).resolve().parent
TAXONOMY_PATH = ROOT / "skills_taxonomy.json"
def _profile_path(base, filename):
    """Return the personal profile file, or the shipped example when absent."""
    import pathlib as _pathlib
    real = _pathlib.Path(base) / filename
    if real.exists():
        return real
    example = _pathlib.Path(base) / filename.replace(".yaml", ".example.yaml")
    return example if example.exists() else real


CAREER_MASTER_PATH = _profile_path(ROOT / "reference_cv_2027" / "data", "career_master.yaml")
EVIDENCE_REGISTER_PATH = _profile_path(ROOT / "reference_cv_2027" / "data", "evidence_register.yaml")
MODEL_CACHE_DIR = ROOT / ".models"
EMBED_MODEL_NAME = "all-MiniLM-L6-v2"
FALLBACK_MODEL_NAME = "tfidf-fallback"
MIN_DESCRIPTION_CHARS = 200
SCORE_VERSION = 1

# Evidence statuses that make a career_master entry usable as candidate truth.
ACCEPTED_EVIDENCE_STATUSES = {
    "verified",
    "verified_from_local_repository",
    "verified_from_git_history",
    "user_confirmed",
    "resolved_for_candidate",
}

_WORD_RE = re.compile(r"[a-zà-ÿ0-9][a-zà-ÿ0-9+#./-]*", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Taxonomy
# --------------------------------------------------------------------------- #
class SkillTaxonomy:
    def __init__(self, skills: list[dict]):
        self.skills = skills
        self._patterns: list[tuple[str, re.Pattern]] = []
        for skill in skills:
            aliases = set(skill.get("aliases", []))
            if len(skill["name"]) >= 4:
                aliases.add(skill["name"])
            aliases = sorted(aliases, key=len, reverse=True)
            escaped = "|".join(re.escape(alias) for alias in aliases if alias)
            pattern = re.compile(rf"(?<![a-z0-9+#])(?:{escaped})(?![a-z0-9+#])", re.IGNORECASE)
            self._patterns.append((skill["name"], pattern))
        self.names = [skill["name"] for skill in skills]

    @classmethod
    def load(cls, path: PathLike = TAXONOMY_PATH) -> "SkillTaxonomy":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        skills = data["skills"] if isinstance(data, dict) else data
        return cls(list(skills))

    def extract(self, text: str) -> list[str]:
        """Return canonical skill names present in ``text`` (taxonomy order)."""
        if not text:
            return []
        found = []
        for name, pattern in self._patterns:
            if pattern.search(text):
                found.append(name)
        return found


_TAXONOMY: SkillTaxonomy | None = None


def taxonomy(path: PathLike = TAXONOMY_PATH) -> SkillTaxonomy:
    global _TAXONOMY
    if _TAXONOMY is None or Path(path) != TAXONOMY_PATH:
        loaded = SkillTaxonomy.load(path)
        if Path(path) == TAXONOMY_PATH:
            _TAXONOMY = loaded
        return loaded
    return _TAXONOMY


# --------------------------------------------------------------------------- #
# Candidate profile (evidence sources only)
# --------------------------------------------------------------------------- #
def _accepted(status: object) -> bool:
    return str(status or "").strip() in ACCEPTED_EVIDENCE_STATUSES


def build_candidate_profile(
    career_master_path: PathLike = CAREER_MASTER_PATH,
    evidence_register_path: PathLike = EVIDENCE_REGISTER_PATH,
) -> dict:
    """Return {'text': str, 'skills': [str], 'sources': [...]} from evidence files only."""
    master = yaml.safe_load(Path(career_master_path).read_text(encoding="utf-8")) or {}
    evidence: dict = {}
    evidence_path = Path(evidence_register_path)
    if evidence_path.exists():
        evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or {}

    lines: list[str] = []
    skills: list[str] = []
    sources: list[str] = []

    targets = master.get("targets") or {}
    for key in ("primary_identity", "headline"):
        if targets.get(key):
            lines.append(str(targets[key]))
    for statement in master.get("summary_evidence") or []:
        lines.append(str(statement))
        sources.append("summary_evidence")

    for group, items in (master.get("skills") or {}).items():
        for item in items or []:
            skills.append(str(item))
            sources.append(f"skills.{group}.{item}")
    lines.append("Skills: " + ", ".join(skills))

    for section in ("experience", "projects"):
        for entry in master.get(section) or []:
            entry_status = entry.get("status")
            if entry_status is not None and not _accepted(entry_status):
                continue
            header = " ".join(
                str(entry.get(key) or "") for key in ("title", "role", "company", "name")
            ).strip()
            if header:
                lines.append(header)
            for bullet in entry.get("bullets") or []:
                if isinstance(bullet, dict):
                    if not _accepted(bullet.get("evidence_status", entry_status or "verified")):
                        continue
                    lines.append(str(bullet.get("statement") or ""))
                    for technology in bullet.get("technologies") or []:
                        skills.append(str(technology))
                    sources.append(f"{section}.{entry.get('id') or header}")
                else:
                    lines.append(str(bullet))

    for education in master.get("education") or []:
        lines.append(" ".join(str(education.get(k) or "") for k in ("degree", "field", "institution")))
    for cert in master.get("certifications") or []:
        if _accepted(cert.get("status", "user_confirmed")):
            lines.append(str(cert.get("name") or ""))
            sources.append(f"certifications.{cert.get('name')}")
    for language in master.get("languages") or []:
        lines.append(f"{language.get('name')} {language.get('level') or ''}".strip())

    # Evidence register: repository names are corroborating context only.
    for repo_key, repo in (evidence.get("repository_evidence") or {}).items():
        if isinstance(repo, dict) and _accepted(repo.get("status")):
            sources.append(f"evidence_register.repository_evidence.{repo_key}")

    text = "\n".join(line for line in lines if line and line.strip())
    return {"text": text, "skills": sorted(set(skills), key=str.casefold), "sources": sources}


def candidate_skill_names(profile: dict, tax: SkillTaxonomy) -> set[str]:
    """Canonical taxonomy names the candidate has evidence for."""
    text = profile["text"] + "\n" + ", ".join(profile["skills"])
    return set(tax.extract(text))


# --------------------------------------------------------------------------- #
# Embedding backends
# --------------------------------------------------------------------------- #
def _tokens(text: str) -> list[str]:
    return [token.lower() for token in _WORD_RE.findall(text or "")]


class TfidfBackend:
    name = FALLBACK_MODEL_NAME

    def __init__(self, corpus: Iterable[str]):
        documents = [_tokens(doc) for doc in corpus]
        df: Counter = Counter()
        for doc in documents:
            df.update(set(doc))
        self.n_docs = max(1, len(documents))
        self.idf = {term: math.log((1 + self.n_docs) / (1 + count)) + 1.0 for term, count in df.items()}

    def vector(self, text: str) -> dict[str, float]:
        tf = Counter(_tokens(text))
        total = sum(tf.values()) or 1
        return {term: (count / total) * self.idf.get(term, math.log(1 + self.n_docs) + 1.0) for term, count in tf.items()}

    def similarity(self, a: str, b: str) -> float:
        va, vb = self.vector(a), self.vector(b)
        dot = sum(weight * vb.get(term, 0.0) for term, weight in va.items())
        norm = math.sqrt(sum(w * w for w in va.values())) * math.sqrt(sum(w * w for w in vb.values()))
        return dot / norm if norm else 0.0


class SentenceTransformerBackend:
    name = EMBED_MODEL_NAME

    def __init__(self, model):
        self.model = model
        self._cache: dict[str, list[float]] = {}

    def embed(self, text: str) -> list[float]:
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if key not in self._cache:
            self._cache[key] = [float(x) for x in self.model.encode(text, normalize_embeddings=True)]
        return self._cache[key]

    def similarity(self, a: str, b: str) -> float:
        va, vb = self.embed(a), self.embed(b)
        return float(sum(x * y for x, y in zip(va, vb)))


def load_backend(corpus: Iterable[str], prefer: str = "auto"):
    """Return (backend). prefer='tfidf' forces the deterministic fallback."""
    corpus = list(corpus)
    if prefer == "tfidf" or os.environ.get("SEMANTIC_MATCH_BACKEND") == "tfidf":
        return TfidfBackend(corpus)
    try:
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        from sentence_transformers import SentenceTransformer  # type: ignore

        model = SentenceTransformer(EMBED_MODEL_NAME, cache_folder=str(MODEL_CACHE_DIR))
        return SentenceTransformerBackend(model)
    except Exception:  # pragma: no cover - network / dependency dependent
        return TfidfBackend(corpus)


def similarity_to_score(similarity: float) -> int:
    """Map cosine similarity to an integer 0-100 (clamped)."""
    if similarity != similarity:  # NaN
        return 0
    return int(round(max(0.0, min(1.0, similarity)) * 100))


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def content_hash(description: str, profile_text: str, model: str) -> str:
    payload = f"{SCORE_VERSION}\x1f{model}\x1f{profile_text}\x1f{description}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _row(connection: sqlite3.Connection, opportunity_id: str) -> dict | None:
    row = connection.execute(
        "SELECT * FROM semantic_scores WHERE opportunity_id = ?", (opportunity_id,)
    ).fetchone()
    return dict(row) if row else None


def _serialize(row: dict) -> dict:
    return {
        "opportunity_id": row["opportunity_id"],
        "semantic_score": int(round(float(row["score"]))),
        "model": row["model"],
        "skills_have": json.loads(row["skills_have_json"] or "[]"),
        "skills_missing": json.loads(row["skills_missing_json"] or "[]"),
        "computed_at": row["computed_at"],
        "content_hash": row["content_hash"],
    }


def gap_analysis(description: str, candidate_skills: set[str], tax: SkillTaxonomy) -> tuple[list[str], list[str]]:
    required = tax.extract(description)
    have = [skill for skill in required if skill in candidate_skills]
    missing = [skill for skill in required if skill not in candidate_skills]
    return have, missing


def recompute(
    db_path: PathLike,
    opportunity_id: str | None = None,
    *,
    all_opportunities: bool = False,
    force: bool = False,
    backend: str = "auto",
    career_master_path: PathLike = CAREER_MASTER_PATH,
    evidence_register_path: PathLike = EVIDENCE_REGISTER_PATH,
    taxonomy_path: PathLike = TAXONOMY_PATH,
) -> dict:
    """Compute/refresh semantic_scores. Returns a summary dict."""
    if not opportunity_id and not all_opportunities:
        raise ValidationError("opportunity_id or all:true is required")
    tax = taxonomy(taxonomy_path)
    profile = build_candidate_profile(career_master_path, evidence_register_path)
    candidate_skills = candidate_skill_names(profile, tax)

    with closing(connect(db_path)) as connection:
        if opportunity_id:
            rows = connection.execute(
                "SELECT id, title, company, description FROM opportunities WHERE id = ?", (opportunity_id,)
            ).fetchall()
            if not rows:
                raise NotFoundError(f"opportunity {opportunity_id} not found")
        else:
            rows = connection.execute("SELECT id, title, company, description FROM opportunities ORDER BY id").fetchall()

        eligible = [dict(row) for row in rows if len(str(row["description"] or "")) >= MIN_DESCRIPTION_CHARS]
        skipped_short = len(rows) - len(eligible)
        engine = None
        computed = 0
        unchanged = 0
        results = []
        for row in eligible:
            description = str(row["description"] or "")
            jd_text = f"{row['title']}\n{row['company']}\n{description}"
            if engine is None:
                engine = load_backend([profile["text"], *(r["description"] for r in eligible)], prefer=backend)
            digest = content_hash(jd_text, profile["text"], engine.name)
            existing = _row(connection, row["id"])
            if existing and existing["content_hash"] == digest and not force:
                unchanged += 1
                results.append(_serialize(existing))
                continue
            score = similarity_to_score(engine.similarity(profile["text"], jd_text))
            have, missing = gap_analysis(jd_text, candidate_skills, tax)
            now = _now()
            connection.execute(
                """INSERT INTO semantic_scores(opportunity_id, score, model, skills_have_json,
                                               skills_missing_json, computed_at, content_hash)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(opportunity_id) DO UPDATE SET
                     score=excluded.score, model=excluded.model,
                     skills_have_json=excluded.skills_have_json,
                     skills_missing_json=excluded.skills_missing_json,
                     computed_at=excluded.computed_at, content_hash=excluded.content_hash""",
                (row["id"], float(score), engine.name, json.dumps(have), json.dumps(missing), now, digest),
            )
            computed += 1
            results.append(_serialize(_row(connection, row["id"])))
        connection.commit()
    model_name = engine.name if engine else None
    return {
        "requested": len(rows),
        "eligible": len(eligible),
        "skipped_short_description": skipped_short,
        "computed": computed,
        "unchanged": unchanged,
        "model": model_name,
        "candidate_skill_count": len(candidate_skills),
        "results": results if opportunity_id else None,
    }


def match_detail(db_path: PathLike, opportunity_id: str) -> dict:
    with closing(connect(db_path)) as connection:
        if not connection.execute("SELECT 1 FROM opportunities WHERE id=?", (opportunity_id,)).fetchone():
            raise NotFoundError(f"opportunity {opportunity_id} not found")
        row = _row(connection, opportunity_id)
    if row is None:
        return {
            "opportunity_id": opportunity_id,
            "semantic_score": None,
            "model": None,
            "skills_have": [],
            "skills_missing": [],
            "computed_at": None,
            "content_hash": None,
            "status": "not_computed",
            "breakdown": explain(db_path, opportunity_id),
        }
    result = _serialize(row)
    result["status"] = "computed"
    result["breakdown"] = explain(db_path, opportunity_id)
    return result


# --------------------------------------------------------------------------- #
# Score breakdown (CV-Matcher idea): explain WHY a score is what it is.
# --------------------------------------------------------------------------- #
BREAKDOWN_WEIGHTS = {
    "semantic": 0.35,
    "hard_skills_pct": 0.35,
    "title_similarity": 0.10,
    "seniority_fit": 0.10,
    "language_fit": 0.10,
}
CANDIDATE_MAX_YEARS = 3  # 2027 graduate: 1-3 years of experience is the realistic band
_YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|\s*(?:-|to|à|a)\s*\d{1,2})?\s*\+?\s*(?:years?|yrs?|ans?|années?)",
    re.IGNORECASE,
)
_SENIOR_RE = re.compile(r"\b(senior|principal|staff|lead|head|director|architect|confirm[ée]|s[ée]nior)\b", re.IGNORECASE)
_JUNIOR_RE = re.compile(r"\b(junior|intern|internship|stage|stagiaire|graduate|entry[- ]level|alternance|pfe|d[ée]butant)\b", re.IGNORECASE)


def required_years(text: str) -> int | None:
    """Minimum years of experience the JD asks for (smallest explicit figure), or None."""
    figures = [int(m.group(1)) for m in _YEARS_RE.finditer(text or "") if 0 < int(m.group(1)) <= 30]
    return min(figures) if figures else None


def seniority_fit_score(jd_text: str, max_years: int = CANDIDATE_MAX_YEARS) -> int:
    years = required_years(jd_text)
    if _JUNIOR_RE.search(jd_text or ""):
        base = 100
    elif _SENIOR_RE.search(jd_text or ""):
        base = 30
    else:
        base = 75
    if years is None:
        return base
    if years <= max_years:
        return min(100, base + 10) if base >= 75 else base
    return max(0, base - 20 * (years - max_years))


def title_similarity_score(jd_title: str, candidate_titles: Iterable[str]) -> int:
    tokens = set(_tokens(jd_title)) - {"h/f", "f/h", "m/f", "f/m", "cdi", "stage", "-", "&", "and", "et"}
    if not tokens:
        return 0
    best = 0.0
    for title in candidate_titles:
        other = set(_tokens(title))
        if other:
            best = max(best, len(tokens & other) / len(tokens | other))
    return int(round(best * 100))


def language_fit_score(jd_text: str, languages: list[dict]) -> int:
    """100 when the JD language is one the candidate lists at B2+ (or the JD is fr/en)."""
    lowered = (jd_text or "").casefold()
    fr = sum(lowered.count(f" {w} ") for w in ("le", "la", "les", "des", "et", "vous", "nous", "une"))
    en = sum(lowered.count(f" {w} ") for w in ("the", "and", "you", "with", "our", "for", "will"))
    jd_language = "fr" if fr > en else "en"
    names = {str(l.get("name") or "").casefold(): str(l.get("level") or "") for l in languages if isinstance(l, dict)}
    key = "french" if jd_language == "fr" else "english"
    level = names.get(key)
    if level is None:
        return 40
    if re.search(r"native|c1|c2|fluent|bilingual", level, re.IGNORECASE):
        return 100
    if re.search(r"b2", level, re.IGNORECASE):
        return 90
    return 60


def explain(
    db_path: PathLike,
    opportunity_id: str,
    career_master_path: PathLike = CAREER_MASTER_PATH,
    evidence_register_path: PathLike = EVIDENCE_REGISTER_PATH,
    taxonomy_path: PathLike = TAXONOMY_PATH,
) -> dict:
    """Deterministic breakdown {semantic, hard_skills_pct, title_similarity, seniority_fit,
    language_fit, total}. All components are integers 0-100; total is the weighted sum."""
    with closing(connect(db_path)) as connection:
        opportunity = connection.execute(
            "SELECT id, title, company, description, requirements FROM opportunities WHERE id=?", (opportunity_id,)
        ).fetchone()
        if opportunity is None:
            raise NotFoundError(f"opportunity {opportunity_id} not found")
        stored = _row(connection, opportunity_id)
    tax = taxonomy(taxonomy_path)
    master = yaml.safe_load(Path(career_master_path).read_text(encoding="utf-8")) or {}
    profile = build_candidate_profile(career_master_path, evidence_register_path)
    candidate_skills = candidate_skill_names(profile, tax)
    jd_text = "\n".join(str(opportunity[k] or "") for k in ("title", "description", "requirements"))
    have, missing = gap_analysis(jd_text, candidate_skills, tax)
    required = len(have) + len(missing)
    hard_skills = int(round(len(have) / required * 100)) if required else 0
    semantic = int(round(float(stored["score"]))) if stored else 0
    targets = master.get("targets") or {}
    candidate_titles = [str(targets.get("primary_identity") or ""), str(targets.get("headline") or "").split("|")[0]]
    candidate_titles += [str(e.get("title") or "") for e in master.get("experience") or [] if isinstance(e, dict)]
    components = {
        "semantic": semantic,
        "hard_skills_pct": hard_skills,
        "title_similarity": title_similarity_score(str(opportunity["title"] or ""), candidate_titles),
        "seniority_fit": seniority_fit_score(jd_text),
        "language_fit": language_fit_score(jd_text, master.get("languages") or []),
    }
    total = int(round(sum(components[k] * w for k, w in BREAKDOWN_WEIGHTS.items())))
    return {
        **components,
        "total": max(0, min(100, total)),
        "weights": BREAKDOWN_WEIGHTS,
        "required_years": required_years(jd_text),
        "skills_required": required,
        "method": "cv_matcher_breakdown_v1",
    }


def skill_gaps(db_path: PathLike, limit: int = 25, open_only: bool = True) -> dict:
    """Aggregate missing skills across (open) opportunities -> learning roadmap."""
    limit = max(1, min(int(limit or 25), 200))
    closed = {"archived", "closed", "rejected", "withdrawn"}
    counter: Counter = Counter()
    have_counter: Counter = Counter()
    considered = 0
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """SELECT s.skills_missing_json, s.skills_have_json, o.status
               FROM semantic_scores s JOIN opportunities o ON o.id = s.opportunity_id"""
        ).fetchall()
    for row in rows:
        if open_only and str(row["status"] or "") in closed:
            continue
        considered += 1
        counter.update(json.loads(row["skills_missing_json"] or "[]"))
        have_counter.update(json.loads(row["skills_have_json"] or "[]"))
    gaps = [
        {"skill": skill, "count": count, "share": round(count / considered, 3) if considered else 0.0}
        for skill, count in counter.most_common(limit)
    ]
    strengths = [{"skill": skill, "count": count} for skill, count in have_counter.most_common(limit)]
    return {"opportunities_considered": considered, "open_only": open_only, "top_missing": gaps, "top_have": strengths}


def search(db_path: PathLike, query: str, limit: int = 25) -> dict:
    """FTS5 bm25-ranked search over opportunities_fts."""
    query = str(query or "").strip()
    if not query:
        raise ValidationError("q is required")
    limit = max(1, min(int(limit or 25), 200))
    # Sanitise: build an implicit AND of prefix terms so raw user input can't break FTS syntax.
    terms = [re.sub(r'"', "", token) for token in _WORD_RE.findall(query)]
    if not terms:
        raise ValidationError("q has no searchable terms")
    fts_query = " ".join(f'"{term}"*' for term in terms)
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """SELECT o.id, o.title, o.company, o.status, o.priority_score,
                      bm25(opportunities_fts, 5.0, 3.0, 1.0) AS rank,
                      snippet(opportunities_fts, 2, '[', ']', '…', 18) AS snippet
               FROM opportunities_fts
               JOIN opportunities o ON o.rowid = opportunities_fts.rowid
               WHERE opportunities_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (fts_query, limit),
        ).fetchall()
    return {
        "query": query,
        "count": len(rows),
        "results": [
            {
                "id": row["id"],
                "title": row["title"],
                "company": row["company"],
                "status": row["status"],
                "priority_score": row["priority_score"],
                "rank": round(float(row["rank"]), 4),
                "snippet": row["snippet"],
            }
            for row in rows
        ],
    }


def parse_recompute_payload(payload: dict) -> dict:
    opportunity_id = payload.get("opportunity_id")
    all_flag = bool(payload.get("all"))
    if opportunity_id is not None and not isinstance(opportunity_id, str):
        raise ValidationError("opportunity_id must be a string")
    if not opportunity_id and not all_flag:
        raise ValidationError("provide opportunity_id or all:true")
    return {
        "opportunity_id": opportunity_id or None,
        "all_opportunities": all_flag and not opportunity_id,
        "force": bool(payload.get("force", False)),
    }


if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="Recompute semantic scores")
    parser.add_argument("--db", default=str(ROOT / "career_pipeline_v2.sqlite3"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--backend", default="auto", choices=["auto", "tfidf"])
    args = parser.parse_args()
    pipeline_v2.create_schema(args.db)
    print(json.dumps(recompute(args.db, all_opportunities=True, force=args.force, backend=args.backend), indent=2))
    print(json.dumps(skill_gaps(args.db, limit=10), indent=2))
