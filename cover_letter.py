"""Cover letter drafts: local, plain-text, assembled only from evidence facts.

Deterministic (no LLM). The body is built from career_master statements whose
technologies or wording match the job description keywords. Drafts are saved
locally with status draft_local and are never sent by this system.
"""

from __future__ import annotations

import json
import re
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

import keyword_highlight as kh
import semantic_match
from pipeline_v2 import ConflictError, NotFoundError, PathLike, ValidationError, connect, stable_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS cover_letter_drafts (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    language TEXT NOT NULL CHECK(language IN ('fr', 'en')),
    body TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft_local',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cover_letter_drafts_opportunity ON cover_letter_drafts(opportunity_id);
"""

MAX_WORDS = 250
MAX_FACTS = 3
LANGUAGES = {"fr", "en"}
FRENCH_MARKERS = frozenset("""
le la les des une pour avec dans vous nous notre votre poste mission équipe compétences
expérience recherchons candidat stage alternance rejoindre maîtrise connaissances
""".split())
EMOJI_RE = re.compile(r"[\U0001F000-\U0001FFFF\u2600-\u27BF\u2B00-\u2BFF]")


def ensure_schema(connection) -> None:
    connection.executescript(SCHEMA)


def detect_language(text: str) -> str:
    tokens = re.findall(r"[a-zà-ÿ]+", (text or "").casefold())
    if not tokens:
        return "en"
    french = sum(1 for t in tokens if t in FRENCH_MARKERS)
    return "fr" if french / len(tokens) > 0.04 else "en"


def _clean(text: str) -> str:
    text = text.replace("\u2014", ",").replace("\u2013", "-")
    text = EMOJI_RE.sub("", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _first_sentence(statement: str) -> str:
    statement = _clean(statement)
    parts = re.split(r"(?<=[.!?])\s+", statement)
    return parts[0] if parts else statement


def select_facts(profile: dict[str, Any], jd_text: str, taxonomy_path: PathLike = kh.TAXONOMY_PATH) -> tuple[list[dict[str, Any]], list[str]]:
    """Facts ranked by overlap with JD taxonomy skills. Returns (facts, matched_skills)."""
    tax = semantic_match.taxonomy(taxonomy_path)
    required = tax.extract(jd_text)
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for order, fact in enumerate(profile["facts"]):
        if fact["kind"] not in {"experience", "projects", "leadership"}:
            continue
        haystack = fact["statement"] + " " + " ".join(fact["technologies"])
        hits = [s for s in required if kh.count_term(haystack, s) > 0]
        if hits:
            scored.append((-len(hits), order, fact))
    scored.sort(key=lambda item: (item[0], item[1]))
    chosen = [fact for _, _, fact in scored[:MAX_FACTS]]
    matched: list[str] = []
    for fact in chosen:
        haystack = fact["statement"] + " " + " ".join(fact["technologies"])
        for skill in required:
            if skill not in matched and kh.count_term(haystack, skill) > 0:
                matched.append(skill)
    return chosen, matched


def _trim_words(text: str, limit: int) -> str:
    words = text.split()
    if len(words) <= limit:
        return text
    return " ".join(words[:limit]).rstrip(",;:") + "."


def compose(
    opportunity: dict[str, Any],
    profile: dict[str, Any],
    facts: list[dict[str, Any]],
    matched_skills: list[str],
    language: str,
) -> str:
    identity = profile.get("identity") or {}
    name = str(identity.get("name") or "").strip()
    company = _clean(str(opportunity.get("company") or ""))
    title = _clean(str(opportunity.get("title") or ""))
    targets = profile.get("targets") or {}
    role = _clean(str(targets.get("primary_identity") or "engineer"))
    skills_text = ", ".join(matched_skills[:6])

    if language == "fr":
        lines = [
            "Madame, Monsieur,",
            "",
            f"Je vous propose ma candidature au poste de {title} chez {company}. "
            f"Je suis {role} et je travaille avec {skills_text}." if skills_text else
            f"Je vous propose ma candidature au poste de {title} chez {company}. Je suis {role}.",
            "",
        ]
        for fact in facts:
            where = fact["company"] or fact["title"]
            prefix = f"Chez {where}, " if fact["kind"] == "experience" and where else ""
            lines.append(_first_sentence(prefix + _lower_first(fact["statement"]) if prefix else fact["statement"]))
        lines += [
            "",
            f"Je serais heureux d'échanger sur la façon dont cette expérience peut servir votre équipe.",
            "",
            "Cordialement,",
            name,
        ]
    else:
        lines = [
            "Dear Hiring Team,",
            "",
            f"I am applying for the {title} position at {company}. "
            f"I am a {role} and I work with {skills_text}." if skills_text else
            f"I am applying for the {title} position at {company}. I am a {role}.",
            "",
        ]
        for fact in facts:
            where = fact["company"] or fact["title"]
            prefix = f"At {where}, " if fact["kind"] == "experience" and where else ""
            lines.append(_first_sentence(prefix + _lower_first(fact["statement"]) if prefix else fact["statement"]))
        lines += [
            "",
            "I would welcome the chance to discuss how this experience can support your team.",
            "",
            "Kind regards,",
            name,
        ]
    body = "\n".join(_clean(line) for line in lines)
    return _trim_words(body, MAX_WORDS)


def _lower_first(text: str) -> str:
    text = _clean(text)
    if not text:
        return text
    first = text.split(" ", 1)[0]
    if first.isupper() or any(c.isupper() for c in first[1:]):
        return text  # acronym or proper technology name: keep case
    return text[0].lower() + text[1:]


def lint_body(body: str) -> list[str]:
    problems = []
    if len(body.split()) > MAX_WORDS:
        problems.append("too long")
    if "\u2014" in body:
        problems.append("em dash")
    if EMOJI_RE.search(body):
        problems.append("emoji")
    return problems


def _serialize(row) -> dict[str, Any]:
    record = dict(row)
    record["evidence_ids"] = json.loads(record.pop("evidence_ids_json") or "[]")
    record["is_draft"] = True
    record["send_policy"] = "Local draft only. This system never sends or submits."
    return record


def list_drafts(db_path: PathLike, opportunity_id: str | None = None) -> dict[str, Any]:
    with closing(connect(db_path)) as connection:
        ensure_schema(connection)
        if opportunity_id:
            kh.load_opportunity(connection, opportunity_id)
            rows = connection.execute(
                "SELECT * FROM cover_letter_drafts WHERE opportunity_id=? ORDER BY created_at DESC, id",
                (opportunity_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM cover_letter_drafts ORDER BY created_at DESC, id"
            ).fetchall()
        return {"drafts": [_serialize(r) for r in rows], "count": len(rows)}


def generate(
    db_path: PathLike,
    payload: dict[str, Any],
    root: PathLike = kh.ROOT,
    career_master_path: PathLike = kh.CAREER_MASTER_PATH,
    evidence_register_path: PathLike = kh.EVIDENCE_REGISTER_PATH,
    knowledge_path: PathLike = kh.KNOWLEDGE_PATH,
    taxonomy_path: PathLike = kh.TAXONOMY_PATH,
) -> dict[str, Any]:
    unknown = set(payload) - {"opportunity_id", "version", "language"}
    if unknown:
        raise ValidationError("only opportunity_id, version and language are accepted")
    opportunity_id = payload.get("opportunity_id")
    if not isinstance(opportunity_id, str) or not opportunity_id:
        raise ValidationError("opportunity_id is required")
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        raise ValidationError("version is required")
    language = payload.get("language")
    if language is not None and language not in LANGUAGES:
        raise ValidationError("language must be fr or en")
    with closing(connect(db_path)) as connection:
        ensure_schema(connection)
        opportunity = kh.load_opportunity(connection, opportunity_id)
        if version != opportunity["updated_at"]:
            raise ConflictError("opportunity changed; reload before retrying")
    jd_text = kh.vacancy_text(opportunity)
    if not str(opportunity.get("description") or "").strip():
        raise ValidationError("opportunity has no description to draft from")
    if language is None:
        language = detect_language(jd_text)
    profile = kh.evidence_profile(career_master_path, evidence_register_path, knowledge_path, taxonomy_path)
    facts, matched = select_facts(profile, jd_text, taxonomy_path)
    if not facts:
        raise ValidationError("no evidence-backed facts match this job description; draft not created")
    body = compose(opportunity, profile, facts, matched, language)
    problems = lint_body(body)
    if problems:
        raise ValidationError("draft failed lint: " + ", ".join(problems))
    now = _now()
    draft_id = stable_id("cl", opportunity_id, language, now)
    evidence_ids = [fact["citation"] for fact in facts]
    with closing(connect(db_path)) as connection:
        ensure_schema(connection)
        connection.execute(
            """INSERT INTO cover_letter_drafts(id, opportunity_id, language, body, evidence_ids_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'draft_local', ?)""",
            (draft_id, opportunity_id, language, body, json.dumps(evidence_ids, ensure_ascii=False), now),
        )
        connection.commit()
        record = _serialize(connection.execute(
            "SELECT * FROM cover_letter_drafts WHERE id=?", (draft_id,)
        ).fetchone())
    record["matched_skills"] = matched
    record["word_count"] = len(body.split())
    return record


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
