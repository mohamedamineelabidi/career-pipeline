"""Interview prep: deterministic, evidence-grounded question and talking-point pack.

Port of Resume-Matcher style interview preparation as a local rule-based module
(no LLM calls). Every talking point cites a career_master path or evidence id.
Questions about skills the candidate lacks come with honest answering notes:
never claim experience that the evidence register does not support.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

import keyword_highlight as kh
import semantic_match
from pipeline_v2 import ConflictError, NotFoundError, PathLike, ValidationError, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS interview_preps (
    opportunity_id TEXT PRIMARY KEY REFERENCES opportunities(id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

MAX_TECHNICAL = 8
MAX_BEHAVIOURAL = 6
MAX_GAPS = 6
MAX_TALKING_POINTS = 8


def ensure_schema(connection) -> None:
    connection.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fact_index(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {fact["citation"]: fact for fact in profile["facts"]}


def _facts_for(profile: dict[str, Any], term: str) -> list[dict[str, Any]]:
    index = _fact_index(profile)
    facts = [index[c] for c in kh.citations_for(profile, term) if c in index]
    if facts:
        return facts
    aliases = None
    return [f for f in profile["facts"] if kh.count_term(f["statement"], term, aliases) > 0]


def build_prep(
    opportunity: dict[str, Any],
    jd_text: str,
    cv_text: str,
    profile: dict[str, Any],
    taxonomy_path: PathLike = kh.TAXONOMY_PATH,
) -> dict[str, Any]:
    tax = semantic_match.taxonomy(taxonomy_path)
    required = tax.extract(jd_text)
    have = [s for s in required if kh.citations_for(profile, s)]
    missing = [s for s in required if s not in have]
    company = str(opportunity.get("company") or "the company")
    title = str(opportunity.get("title") or "the role")

    technical: list[dict[str, Any]] = []
    for skill in have[:MAX_TECHNICAL]:
        facts = _facts_for(profile, skill)
        primary = facts[0] if facts else None
        technical.append({
            "kind": "technical",
            "skill": skill,
            "question": f"Tell me about a project where you used {skill}. What did you build and what was hard?",
            "answer_basis": primary["statement"] if primary else f"Listed skill: {skill}",
            "evidence": kh.citations_for(profile, skill)[:4],
            "in_cv": kh.count_term(cv_text, skill) > 0,
        })

    behavioural: list[dict[str, Any]] = []
    seen_entries: set[str] = set()
    for fact in profile["facts"]:
        if fact["kind"] not in {"experience", "projects", "leadership"}:
            continue
        entry_key = fact["citation"].rsplit(".bullet_", 1)[0]
        if entry_key in seen_entries:
            continue
        seen_entries.add(entry_key)
        where = fact["company"] or fact["title"] or "that project"
        behavioural.append({
            "kind": "behavioural",
            "question": f"Describe a challenge you faced at {where} and how you handled it.",
            "answer_basis": fact["statement"],
            "metrics_allowed": fact["metrics"],
            "evidence": [fact["citation"]],
        })
        if len(behavioural) >= MAX_BEHAVIOURAL:
            break

    gaps: list[dict[str, Any]] = []
    for skill in missing[:MAX_GAPS]:
        related = [s for s in have if tax_category(tax, s) == tax_category(tax, skill)][:3]
        gaps.append({
            "kind": "gap",
            "skill": skill,
            "question": f"How much hands-on experience do you have with {skill}?",
            "honest_answer_note": (
                f"No evidence of {skill} in career_master or the evidence register. "
                "Say so plainly, then point to related evidence-backed work"
                + (f" ({', '.join(related)})" if related else "")
                + " and your willingness to learn. Do not claim experience you cannot back."
            ),
            "related_evidenced_skills": related,
            "evidence": [c for s in related for c in kh.citations_for(profile, s)[:1]],
        })

    talking_points: list[dict[str, Any]] = []
    used: set[str] = set()
    for skill in have:
        for fact in _facts_for(profile, skill):
            if fact["citation"] in used or fact["kind"] == "summary":
                continue
            used.add(fact["citation"])
            talking_points.append({
                "point": fact["statement"],
                "relevant_to": [s for s in have if s.casefold() in {t.casefold() for t in fact["technologies"]} or kh.count_term(fact["statement"], s) > 0],
                "metrics_allowed": fact["metrics"],
                "evidence": fact["citation"],
            })
            break
        if len(talking_points) >= MAX_TALKING_POINTS:
            break

    questions_to_ask = [
        f"What does the day-to-day work of a {title} at {company} look like in the first three months?",
        "Which parts of the stack described in the posting are already in production, and which are planned?",
        "How does the team review code and validate data or model quality before release?",
        "What would success look like for this role after six months?",
    ]
    if missing:
        questions_to_ask.append(
            f"How central is {missing[0]} to the role, and is there room to learn it on the job?"
        )

    return {
        "prep_schema_version": 1,
        "opportunity_id": opportunity.get("id"),
        "company": company,
        "title": title,
        "skills_have": have,
        "skills_missing": missing,
        "likely_questions": technical + behavioural + gaps,
        "talking_points": talking_points,
        "questions_to_ask_them": questions_to_ask,
        "truthfulness_policy": (
            "Every talking point cites career_master/evidence. Gap questions must be answered "
            "honestly; never claim skills, metrics, or seniority not in the evidence register."
        ),
        "method": "deterministic_interview_prep_v1",
    }


def tax_category(tax: semantic_match.SkillTaxonomy, name: str) -> str:
    for skill in tax.skills:
        if skill["name"] == name:
            return str(skill.get("category") or "")
    return ""


def _serialize(row) -> dict[str, Any]:
    record = dict(row)
    record["prep"] = json.loads(record.pop("payload_json") or "{}")
    return record


def get_prep(db_path: PathLike, opportunity_id: str) -> dict[str, Any]:
    with closing(connect(db_path)) as connection:
        ensure_schema(connection)
        kh.load_opportunity(connection, opportunity_id)
        row = connection.execute(
            "SELECT * FROM interview_preps WHERE opportunity_id=?", (opportunity_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("no interview prep generated for opportunity")
        return _serialize(row)


def generate(
    db_path: PathLike,
    payload: dict[str, Any],
    root: PathLike = kh.ROOT,
    career_master_path: PathLike = kh.CAREER_MASTER_PATH,
    evidence_register_path: PathLike = kh.EVIDENCE_REGISTER_PATH,
    knowledge_path: PathLike = kh.KNOWLEDGE_PATH,
    taxonomy_path: PathLike = kh.TAXONOMY_PATH,
) -> dict[str, Any]:
    unknown = set(payload) - {"opportunity_id", "version"}
    if unknown:
        raise ValidationError("only opportunity_id and version are accepted")
    opportunity_id = payload.get("opportunity_id")
    if not isinstance(opportunity_id, str) or not opportunity_id:
        raise ValidationError("opportunity_id is required")
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        raise ValidationError("version is required")
    with closing(connect(db_path)) as connection:
        ensure_schema(connection)
        opportunity = kh.load_opportunity(connection, opportunity_id)
        if version != opportunity["updated_at"]:
            raise ConflictError("opportunity changed; reload before retrying")
        artifact = kh.select_artifact(connection, opportunity_id)
    jd_text = kh.vacancy_text(opportunity)
    if not jd_text.strip():
        raise ValidationError("opportunity has no description to prepare from")
    cv_text = kh.artifact_text(artifact, root)
    profile = kh.evidence_profile(career_master_path, evidence_register_path, knowledge_path, taxonomy_path)
    prep = build_prep(opportunity, jd_text, cv_text, profile, taxonomy_path)
    now = _now()
    with closing(connect(db_path)) as connection:
        ensure_schema(connection)
        connection.execute(
            """INSERT INTO interview_preps(opportunity_id, payload_json, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(opportunity_id) DO UPDATE SET
                   payload_json=excluded.payload_json, created_at=excluded.created_at""",
            (opportunity_id, json.dumps(prep, ensure_ascii=False), now),
        )
        connection.commit()
        return _serialize(connection.execute(
            "SELECT * FROM interview_preps WHERE opportunity_id=?", (opportunity_id,)
        ).fetchone())
