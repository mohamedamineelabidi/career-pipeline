"""Keyword highlight: JD keywords vs tailored CV, grounded in evidence sources.

Port of the Resume-Matcher "keyword highlight" idea as a local, deterministic,
rule-based module (no LLM calls). For one opportunity it returns which job
description keywords appear in the tailored CV, which are missing but already
evidence-backed (the actionable ones), and which are missing with no evidence
(never to be added).

This module also hosts the shared evidence/CV/JD helpers reused by
interview_prep.py, cover_letter.py and application_tracker.py.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

import yaml

import semantic_match
from pipeline_v2 import NotFoundError, PathLike, connect

ROOT = Path(__file__).resolve().parent
REFERENCE_DATA = ROOT / "reference_cv_2027" / "data"
def _profile_path(base, filename):
    """Return the personal profile file, or the shipped example when absent."""
    import pathlib as _pathlib
    real = _pathlib.Path(base) / filename
    if real.exists():
        return real
    example = _pathlib.Path(base) / filename.replace(".yaml", ".example.yaml")
    return example if example.exists() else real


CAREER_MASTER_PATH = _profile_path(REFERENCE_DATA, "career_master.yaml")
EVIDENCE_REGISTER_PATH = _profile_path(REFERENCE_DATA, "evidence_register.yaml")
KNOWLEDGE_PATH = _profile_path(REFERENCE_DATA, "tailoring_knowledge.yaml")
TAXONOMY_PATH = semantic_match.TAXONOMY_PATH

ACCEPTED = semantic_match.ACCEPTED_EVIDENCE_STATUSES
MAX_PHRASES = 15
MIN_PHRASE_COUNT = 2

_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9+#./-]*")
STOPWORDS = frozenset("""
a an the and or of to in on for with at by from as is are be been was were will would
should can could may might must this that these those you your we our they their it its
who whom which what when where how not no yes all any some more most other such than
into over under about after before between during through per via etc including
including experience experiences years year strong good excellent ability able work
working team teams role roles position job jobs skills skill knowledge required
requirements requirement preferred plus nice have has had do does did also both either
new using use used based within across
le la les un une des du de et ou à au aux en dans pour par sur avec sans sous chez
ce cet cette ces qui que quoi dont où est sont être avoir vous nous votre notre vos nos
leur leurs son sa ses il elle ils elles on ne pas plus très bien tout tous toute toutes
poste mission missions équipe équipes expérience expériences compétences compétence
ans années profil connaissance connaissances maîtrise capacité
""".split())


# --------------------------------------------------------------------------- #
# Shared helpers: opportunity, JD text, CV text
# --------------------------------------------------------------------------- #
def load_opportunity(connection, opportunity_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM opportunities WHERE id=?", (str(opportunity_id),)
    ).fetchone()
    if row is None:
        raise NotFoundError("opportunity not found")
    return dict(row)


def vacancy_text(opportunity: dict[str, Any]) -> str:
    try:
        source = json.loads(opportunity.get("source_json") or "{}")
    except json.JSONDecodeError:
        source = {}
    if not isinstance(source, dict):
        source = {}
    parts = [
        str(opportunity.get("title") or ""),
        str(opportunity.get("description") or ""),
        str(opportunity.get("requirements") or ""),
        str(source.get("full_job_description") or source.get("job_description") or ""),
    ]
    return "\n".join(part for part in parts if part.strip())


def select_artifact(connection, opportunity_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT * FROM cv_artifacts WHERE opportunity_id=?
           ORDER BY CASE artifact_type WHEN 'tailored' THEN 0 ELSE 1 END, id LIMIT 1""",
        (str(opportunity_id),),
    ).fetchone()
    return dict(row) if row is not None else None


def artifact_text(artifact: dict[str, Any] | None, root: PathLike = ROOT) -> str:
    """Best-effort visible text of a CV artifact: .txt sidecar, PDF text, or YAML.

    Cached per (path, mtime): PDF extraction is slow and the dashboard fires many
    highlight requests concurrently.
    """
    if not artifact:
        return ""
    path = Path(str(artifact.get("path") or ""))
    if not path.is_absolute():
        path = Path(root) / path
    try:
        stamp = path.stat().st_mtime_ns if path.is_file() else 0
    except OSError:
        stamp = 0
    key = (str(path), stamp)
    cached = _TEXT_CACHE.get(key)
    if cached is not None:
        return cached
    result = _artifact_text_uncached(path)
    _TEXT_CACHE[key] = result
    return result


_TEXT_CACHE: dict[tuple[str, int], str] = {}
_PROFILE_CACHE: dict[tuple, dict[str, Any]] = {}


def _artifact_text_uncached(path: Path) -> str:
    sidecar = path.with_suffix(".txt")
    if sidecar.is_file():
        return sidecar.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.casefold()
    if suffix == ".pdf" and path.is_file():
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception:
            return ""
    if suffix in {".yaml", ".yml"} and path.is_file():
        try:
            import cv_render

            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return cv_render.yaml_visible_text(document)
        except Exception:
            return ""
    if suffix in {".html", ".htm", ".txt", ".md"} and path.is_file():
        raw = path.read_text(encoding="utf-8", errors="replace")
        return re.sub(r"<[^>]+>", " ", raw)
    return ""


# --------------------------------------------------------------------------- #
# Evidence profile (career_master + evidence register + tailoring knowledge)
# --------------------------------------------------------------------------- #
def _load_yaml(path: PathLike) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _accepted(status: object, default: str = "verified") -> bool:
    return str(status if status is not None else default).strip() in ACCEPTED


def evidence_profile(
    career_master_path: PathLike = CAREER_MASTER_PATH,
    evidence_register_path: PathLike = EVIDENCE_REGISTER_PATH,
    knowledge_path: PathLike = KNOWLEDGE_PATH,
    taxonomy_path: PathLike = TAXONOMY_PATH,
) -> dict[str, Any]:
    """Facts with citations. Every fact carries a career_master path or evidence id.

    Returns {identity, targets, languages, facts:[{citation, kind, company, title,
    statement, technologies, metrics}], skill_citations:{canonical_or_raw: [citations]},
    text}. Only accepted evidence statuses are included.
    """
    master = _load_yaml(career_master_path)
    knowledge = _load_yaml(knowledge_path)
    _load_yaml(evidence_register_path)  # existence check only; statuses live in master
    tax = semantic_match.taxonomy(taxonomy_path)

    facts: list[dict[str, Any]] = []
    skill_citations: dict[str, list[str]] = {}
    lines: list[str] = []

    def cite(skill: str, citation: str) -> None:
        key = str(skill).strip().casefold()
        if not key:
            return
        bucket = skill_citations.setdefault(key, [])
        if citation not in bucket:
            bucket.append(citation)
        for canonical in tax.extract(str(skill)):
            canon_bucket = skill_citations.setdefault(canonical.casefold(), [])
            if citation not in canon_bucket:
                canon_bucket.append(citation)

    for index, statement in enumerate(master.get("summary_evidence") or [], start=1):
        citation = f"summary_evidence[{index}]"
        facts.append({
            "citation": citation, "kind": "summary", "company": "", "title": "",
            "statement": str(statement), "technologies": [], "metrics": [],
        })
        lines.append(str(statement))
        for canonical in tax.extract(str(statement)):
            cite(canonical, citation)

    for group, items in (master.get("skills") or {}).items():
        for item in items or []:
            cite(str(item), f"skills.{group}.{item}")
            lines.append(str(item))

    for section in ("experience", "projects"):
        for entry in master.get(section) or []:
            if not isinstance(entry, dict):
                continue
            entry_status = entry.get("status")
            if entry_status is not None and not _accepted(entry_status):
                continue
            entry_id = str(entry.get("id") or entry.get("name") or "").strip()
            company = str(entry.get("company") or entry.get("name") or "")
            title = str(entry.get("title") or entry.get("role") or "")
            for number, bullet in enumerate(entry.get("bullets") or [], start=1):
                if isinstance(bullet, dict):
                    if not _accepted(bullet.get("evidence_status", entry_status)):
                        continue
                    statement = str(bullet.get("statement") or "")
                    technologies = [str(t) for t in bullet.get("technologies") or []]
                    metrics = [str(m) for m in bullet.get("metrics") or []]
                else:
                    statement, technologies, metrics = str(bullet), [], []
                if not statement.strip():
                    continue
                citation = f"{section}.{entry_id}.bullet_{number}"
                facts.append({
                    "citation": citation, "kind": section, "company": company,
                    "title": title, "statement": statement,
                    "technologies": technologies, "metrics": metrics,
                    "start_date": str(entry.get("start_date") or ""),
                    "end_date": str(entry.get("end_date") or ""),
                })
                lines.append(statement)
                for technology in technologies:
                    cite(technology, citation)
                for canonical in tax.extract(statement):
                    cite(canonical, citation)

    for index, item in enumerate(master.get("leadership") or [], start=1):
        if not isinstance(item, dict) or not _accepted(item.get("status")):
            continue
        statement = str(item.get("statement") or "")
        if statement:
            citation = f"leadership[{index}]"
            facts.append({
                "citation": citation, "kind": "leadership",
                "company": str(item.get("organization") or ""),
                "title": str(item.get("title") or ""), "statement": statement,
                "technologies": [], "metrics": [str(m) for m in item.get("metrics") or []],
            })
            lines.append(statement)

    for cert in master.get("certifications") or []:
        if isinstance(cert, dict) and _accepted(cert.get("status"), "user_confirmed"):
            name = str(cert.get("name") or "")
            citation = f"certifications.{name}"
            facts.append({
                "citation": citation, "kind": "certification",
                "company": str(cert.get("issuer") or ""), "title": name,
                "statement": name, "technologies": [], "metrics": [],
            })
            lines.append(name)
            for canonical in tax.extract(name):
                cite(canonical, citation)

    for canonical, evidence in (knowledge.get("evidence_linked_skills") or {}).items():
        if not isinstance(evidence, dict):
            continue
        for source in evidence.get("sources") or []:
            cite(str(canonical), f"tailoring_knowledge.evidence_linked_skills.{canonical}<-{source}")

    return {
        "identity": master.get("identity") or {},
        "targets": master.get("targets") or {},
        "languages": master.get("languages") or [],
        "facts": facts,
        "skill_citations": skill_citations,
        "text": "\n".join(lines),
    }


def citations_for(profile: dict[str, Any], term: str) -> list[str]:
    return list(profile["skill_citations"].get(str(term).strip().casefold(), []))


# --------------------------------------------------------------------------- #
# Term counting and JD keyword extraction
# --------------------------------------------------------------------------- #
def term_pattern(term: str, aliases: list[str] | None = None) -> re.Pattern:
    variants = sorted({term, *(aliases or [])}, key=len, reverse=True)
    escaped = "|".join(re.escape(v) for v in variants if v)
    return re.compile(rf"(?<![a-z0-9+#])(?:{escaped})(?![a-z0-9+#])", re.IGNORECASE)


FUZZY_RATIO_THRESHOLD = 92
FUZZY_MIN_TOKEN_LEN = 5
_FUZZY_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9+#.-]*")

try:  # SkillSavvy idea: fuzzy single-token matching (typos, plural/singular drift).
    from rapidfuzz import fuzz as _fuzz
except Exception:  # pragma: no cover - dependency optional at runtime
    _fuzz = None


def _taxonomy_aliases(term: str) -> list[str]:
    """Aliases from skills_taxonomy.json for a canonical name or one of its aliases."""
    try:
        tax = semantic_match.taxonomy()
    except Exception:
        return []
    key = str(term).strip().casefold()
    for skill in tax.skills:
        names = {str(skill.get("name", "")).casefold(), *(str(a).casefold() for a in skill.get("aliases", []))}
        if key in names:
            return [str(skill["name"]), *[str(a) for a in skill.get("aliases", [])]]
    return []


def fuzzy_token_matches(text: str, variants: list[str], threshold: int = FUZZY_RATIO_THRESHOLD) -> int:
    """Count single tokens of ``text`` that are near-identical (ratio >= threshold) to a
    single-token variant but are NOT an exact (case-insensitive) match of any variant."""
    if _fuzz is None or not text:
        return 0
    singles = [v.casefold() for v in variants if v and " " not in v and len(v) >= FUZZY_MIN_TOKEN_LEN]
    if not singles:
        return 0
    exact = {v.casefold() for v in variants if v}
    hits = 0
    for token in _FUZZY_TOKEN_RE.findall(text):
        lowered = token.casefold().rstrip(".")
        if len(lowered) < FUZZY_MIN_TOKEN_LEN or lowered in exact:
            continue
        if any(_fuzz.ratio(lowered, variant) >= threshold for variant in singles):
            hits += 1
    return hits


def count_term(text: str, term: str, aliases: list[str] | None = None, *, fuzzy: bool = True) -> int:
    """Occurrences of ``term`` in ``text`` counting taxonomy aliases (Postgres == PostgreSQL)
    plus rapidfuzz near-matches (ratio >= 92) for single tokens (Kubernets ~ Kubernetes)."""
    if not text:
        return 0
    variants = sorted({term, *(aliases or []), *_taxonomy_aliases(term)} - {""})
    exact = len(term_pattern(term, variants).findall(text))
    if not fuzzy:
        return exact
    return exact + fuzzy_token_matches(text, variants)


def noun_phrases(text: str, exclude: set[str], limit: int = MAX_PHRASES) -> list[tuple[str, int]]:
    """Repeated 2-3 word phrases (no stopword edges) as a cheap noun-phrase proxy."""
    counts: Counter = Counter()
    for segment in re.split(r"[.;:!?\n\r()\[\]•|/,]+", text or ""):
        tokens = [t for t in _TOKEN_RE.findall(segment)]
        lowered = [t.casefold() for t in tokens]
        for size in (2, 3):
            for start in range(0, len(tokens) - size + 1):
                window = lowered[start:start + size]
                if window[0] in STOPWORDS or window[-1] in STOPWORDS:
                    continue
                if any(len(w) < 3 for w in window):
                    continue
                counts[" ".join(window)] += 1
    excluded = {e.casefold() for e in exclude}
    ranked = [
        (phrase, count) for phrase, count in counts.most_common()
        if count >= MIN_PHRASE_COUNT
        and not any(part in excluded for part in (phrase, *phrase.split()))
    ]
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked[:limit]


def jd_keywords(jd_text: str, tax: semantic_match.SkillTaxonomy) -> list[dict[str, Any]]:
    """Taxonomy skills present in the JD plus repeated noun phrases."""
    by_name = {skill["name"]: skill for skill in tax.skills}
    keywords: list[dict[str, Any]] = []
    for name in tax.extract(jd_text):
        skill = by_name[name]
        aliases = list(skill.get("aliases", []))
        keywords.append({
            "term": name,
            "category": str(skill.get("category") or "skill"),
            "aliases": aliases,
            "count_jd": count_term(jd_text, name, aliases),
        })
    known = {k["term"] for k in keywords} | {a for k in keywords for a in k["aliases"]}
    for phrase, count in noun_phrases(jd_text, known):
        keywords.append({"term": phrase, "category": "phrase", "aliases": [], "count_jd": count})
    return keywords


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def build_highlight(
    jd_text: str,
    cv_text: str,
    profile: dict[str, Any],
    taxonomy_path: PathLike = TAXONOMY_PATH,
) -> dict[str, Any]:
    tax = semantic_match.taxonomy(taxonomy_path)
    evidence_text = profile["text"]
    rows: list[dict[str, Any]] = []
    for keyword in jd_keywords(jd_text, tax):
        term, aliases = keyword["term"], keyword["aliases"]
        count_cv = count_term(cv_text, term, aliases)
        evidenced = bool(citations_for(profile, term)) or count_term(evidence_text, term, aliases) > 0
        rows.append({
            "term": term,
            "in_cv": count_cv > 0,
            "count_jd": keyword["count_jd"],
            "count_cv": count_cv,
            "category": keyword["category"],
            "evidenced": evidenced,
            "evidence": citations_for(profile, term)[:5],
        })
    covered = sum(1 for row in rows if row["in_cv"])
    coverage = round(covered / len(rows) * 100, 1) if rows else 0.0
    return {
        "jd_keywords": rows,
        "cv_coverage_pct": coverage,
        "missing_but_evidenced": [r["term"] for r in rows if not r["in_cv"] and r["evidenced"]],
        "missing_unevidenced": [r["term"] for r in rows if not r["in_cv"] and not r["evidenced"]],
        "policy": "Only missing_but_evidenced terms may be added to the CV; missing_unevidenced must never be invented.",
        "method": "taxonomy_plus_repeated_phrases_v1",
    }


def _cached_profile(*paths: PathLike) -> dict[str, Any]:
    """evidence_profile() memoised on the source files' mtimes."""
    stamps = []
    for p in paths:
        try:
            stamps.append(Path(p).stat().st_mtime_ns)
        except OSError:
            stamps.append(0)
    key = (tuple(str(p) for p in paths), tuple(stamps))
    profile = _PROFILE_CACHE.get(key)
    if profile is None:
        profile = evidence_profile(*paths)
        _PROFILE_CACHE.clear()
        _PROFILE_CACHE[key] = profile
    return profile


def highlight(
    db_path: PathLike,
    opportunity_id: str,
    root: PathLike = ROOT,
    career_master_path: PathLike = CAREER_MASTER_PATH,
    evidence_register_path: PathLike = EVIDENCE_REGISTER_PATH,
    knowledge_path: PathLike = KNOWLEDGE_PATH,
    taxonomy_path: PathLike = TAXONOMY_PATH,
) -> dict[str, Any]:
    with closing(connect(db_path)) as connection:
        opportunity = load_opportunity(connection, opportunity_id)
        artifact = select_artifact(connection, opportunity_id)
    jd_text = vacancy_text(opportunity)
    if not str(opportunity.get("description") or "").strip():
        raise NotFoundError("opportunity has no description")
    cv_text = artifact_text(artifact, root)
    if artifact is None or not cv_text.strip():
        raise NotFoundError("no CV text available for opportunity")
    profile = _cached_profile(career_master_path, evidence_register_path, knowledge_path, taxonomy_path)
    result = build_highlight(jd_text, cv_text, profile, taxonomy_path)
    result.update({
        "opportunity_id": opportunity["id"],
        "cv_artifact_id": artifact["id"],
        "cv_artifact_path": artifact["path"],
    })
    return result
