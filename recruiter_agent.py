"""Senior Recruiter Agent: deterministic, local, review-only CV assessment.

Reads an opportunity, its CV artifact, and the job description from the
career_pipeline_v2 SQLite database plus reference_cv_2027 data, and produces a
structured senior-recruiter review: strengths, gaps vs requirements, ATS
keyword coverage, red flags, concrete evidence-backed improvement actions, and
a recommendation. It never sends or applies to anything, and it never suggests
inventing skills, metrics, or seniority.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pipeline_v2
from pipeline_v2 import (
    ConflictError,
    NotFoundError,
    PathLike,
    ValidationError,
    connect,
    stable_id,
)

ROOT = Path(__file__).resolve().parent
REFERENCE_ROOT = ROOT / "reference_cv_2027"
KNOWLEDGE_PATH = REFERENCE_ROOT / "data" / "tailoring_knowledge.yaml"
EVIDENCE_REGISTER_PATH = REFERENCE_ROOT / "data" / "evidence_register.yaml"
CAREER_MASTER_PATH = REFERENCE_ROOT / "data" / "career_master.yaml"
IMPROVED_DIR = REFERENCE_ROOT / "out" / "tailored" / "improved"
REVIEW_SCHEMA_VERSION = 2
RECOMMENDATIONS = ("ready_to_send", "needs_edits", "regenerate")
MAX_IMPROVEMENT_ROUNDS = 5

EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_PATTERN = re.compile(r"\+?\d[\d\s().-]{7,}\d")
TOKEN_PATTERN = re.compile(r"[^\W_]+(?:[+#.\-/][^\W_]+)*", re.UNICODE)
WIDE_GAP_PATTERN = re.compile(r"\S {6,}\S")
SKILLS_HEADING_PATTERN = re.compile(
    r"^\s*(technical\s+skills|skills|core\s+skills|comp[ée]tences(\s+techniques)?)\s*$",
    re.IGNORECASE,
)
SENIORITY_TOKENS = frozenset({
    "senior", "principal", "staff", "director", "head", "manager", "architect", "expert",
    "vp", "chief", "cto", "confirmé", "confirme", "sénior",
})
FR_STOPWORDS = frozenset(
    "le la les des une un et du de en pour avec vous nous sur dans au aux est sont votre "
    "nos vos ce cette ces ou qui que par plus afin ainsi être poste équipe compétences "
    "expérience recherchons mission missions profil".split()
)
EN_STOPWORDS = frozenset(
    "the and for with you we our your of to in on a an is are will be as at by this that "
    "or from role team skills experience requirements responsibilities looking join".split()
)


def detect_language(text: str) -> str:
    """'fr' or 'en' by stopword ratio (deterministic; ties resolve to 'en')."""
    tokens = [token.casefold() for token in re.findall(r"[^\W\d_]+", text or "")]
    if not tokens:
        return "en"
    fr = sum(token in FR_STOPWORDS for token in tokens)
    en = sum(token in EN_STOPWORDS for token in tokens)
    return "fr" if fr > en else "en"


def text_tokens(text: str) -> set[str]:
    """Case-folded word tokens used by the truthfulness guard."""
    return {token.casefold() for token in TOKEN_PATTERN.findall(text or "") if len(token) > 1}


def _string_leaves(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _string_leaves(item)] + [
            str(key) for key in value
        ]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _string_leaves(item)]
    if isinstance(value, (str, int, float)):
        return [str(value)]
    return []


def evidence_corpus_tokens(*sources: PathLike | dict[str, Any]) -> set[str]:
    """Every token that appears in the truth sources (career_master, knowledge, register)."""
    tokens: set[str] = set()
    for source in sources:
        document = source if isinstance(source, dict) else _load_yaml(Path(source))
        tokens |= text_tokens("\n".join(_string_leaves(document)))
    return tokens


def profile_visible_text(profile: dict[str, Any]) -> str:
    """Human-visible strings of a career-master-shaped tailored profile."""
    parts: list[str] = []
    identity = profile.get("identity") or {}
    for key in ("name", "location", "email", "phone", "linkedin_url", "github_url"):
        if identity.get(key):
            parts.append(str(identity[key]))
    variant = profile.get("data_ai_variant") or {}
    for key in ("headline", "summary"):
        if variant.get(key):
            parts.append(str(variant[key]))
    tailoring = profile.get("tailoring") or {}
    if tailoring.get("availability_statement"):
        parts.append(str(tailoring["availability_statement"]))
    for group in ("experience", "projects"):
        for entry in profile.get(group) or []:
            if entry.get("selected_for_data_ai") is False:
                continue
            for key in ("title", "company", "product", "name", "role", "location"):
                if entry.get(key):
                    parts.append(str(entry[key]))
            technologies: list[str] = []
            bullets = entry.get("bullets") or []
            visible = bullets if group == "experience" else bullets[:1]
            for bullet in visible:
                parts.append(str(bullet.get("statement") or ""))
            for bullet in bullets:
                for tech in bullet.get("technologies") or []:
                    if tech not in technologies:
                        technologies.append(str(tech))
            if technologies:
                parts.append(", ".join(technologies))
    for entry in profile.get("education") or []:
        parts.append(" ".join(str(entry.get(k) or "") for k in ("institution", "degree", "field")))
    for entry in (profile.get("certifications") or [])[:4]:
        parts.append(" ".join(str(entry.get(k) or "") for k in ("name", "issuer")))
    return "\n".join(part for part in parts if part.strip())


def _load_tailor_helpers():
    """Reuse requirement/evidence matching logic from tailor_cv_agent."""
    scripts_dir = str(REFERENCE_ROOT / "scripts")
    reference_dir = str(REFERENCE_ROOT)
    for entry in (reference_dir, scripts_dir):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    from scripts import tailor_cv_agent  # type: ignore

    return tailor_cv_agent


try:
    _tailor = _load_tailor_helpers()
    requirement_evidence_report = _tailor.requirement_evidence_report
    _contains_term = _tailor._contains_term
    COMMON_UNEVIDENCED_TECHNOLOGIES = tuple(_tailor.COMMON_UNEVIDENCED_TECHNOLOGIES)
    BLOCKED_PUBLIC_TERMS = tuple(_tailor.BLOCKED_PUBLIC_TERMS)
except Exception:  # pragma: no cover - deterministic local fallback
    _tailor = None
    COMMON_UNEVIDENCED_TECHNOLOGIES = (
        "Kubernetes", "Terraform", "Databricks", "Snowflake", "dbt", "Scala",
        "Golang", "Go", "Rust", "C++", "Jenkins", "Looker", "MongoDB",
        "Django", ".NET",
    )
    BLOCKED_PUBLIC_TERMS = (
        "YOLO", "blink-rate", "AUC 0.913", "PR-AUC 0.867", "PDF reporting",
        "35% matching", "Orange Summer Challenge", "Netix",
    )

    def _contains_term(text: str, term: str) -> bool:
        return bool(
            re.search(rf"(?<!\w){re.escape(term.casefold())}(?!\w)", text.casefold())
        )

    def requirement_evidence_report(text: str, knowledge: dict[str, Any]) -> dict[str, Any]:
        evidence_skills = knowledge.get("evidence_linked_skills", {}) or {}
        synonyms: dict[str, list[str]] = {}
        for entry in ((knowledge.get("safe_keyword_synonyms", {}) or {}).get("equivalents", []) or []):
            canonical = str(entry.get("canonical", "")).strip()
            if canonical:
                synonyms[canonical] = [str(t) for t in entry.get("safe_terms", []) if str(t).strip()]
        matched: list[dict[str, Any]] = []
        recognized_terms: set[str] = set()
        for canonical, evidence in evidence_skills.items():
            terms = [str(canonical), *synonyms.get(str(canonical), [])]
            present = next(
                (term for term in sorted(terms, key=len, reverse=True) if _contains_term(text, term)),
                None,
            )
            if not present:
                continue
            recognized_terms.add(str(canonical).casefold())
            matched.append({
                "vacancy_term": present,
                "canonical_skill": str(canonical),
                "evidence_status": str((evidence or {}).get("strongest_status", "unknown")),
                "evidence_sources": list((evidence or {}).get("sources", [])),
                "caveat": str((evidence or {}).get("caveat", "")),
            })
        missing = [
            term for term in COMMON_UNEVIDENCED_TECHNOLOGIES
            if _contains_term(text, term) and term.casefold() not in recognized_terms
        ]
        recognized = len(matched) + len(missing)
        coverage = round((len(matched) / recognized) * 100, 2) if recognized else 0.0
        return {
            "matched_requirements": matched,
            "missing_skills": missing,
            "recognized_requirements": recognized,
            "keyword_coverage_percent": coverage,
            "method": "curated_technology_terms_and_canonical_evidence_v1",
        }


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    import yaml

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _resolve_artifact_paths(artifact_path: str, root: Path) -> dict[str, Path]:
    pdf = Path(artifact_path)
    if not pdf.is_absolute():
        pdf = root / pdf
    return {
        "pdf": pdf,
        "text": pdf.with_suffix(".txt"),
        "manifest": pdf.with_suffix(".manifest.json"),
    }


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.is_file():
        return {}
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _layout_findings(cv_text: str, artifact_paths: dict[str, Path], manifest: dict[str, Any], root: Path) -> dict[str, bool]:
    """Detect a standalone skills section and two-column layouts from yaml + text sidecar."""
    standalone_skills = False
    two_column = False
    lines = [line for line in cv_text.splitlines() if line.strip()]
    if lines:
        if any(SKILLS_HEADING_PATTERN.match(line) for line in lines):
            standalone_skills = True
        gap_lines = sum(bool(WIDE_GAP_PATTERN.search(line.strip())) for line in lines)
        # right-aligned dates produce a few gap lines; a sidebar produces gaps on most lines.
        two_column = gap_lines / len(lines) > 0.5
    yaml_candidates = [artifact_paths["pdf"].with_suffix(".yaml")]
    source_profile = str((manifest.get("files") or {}).get("source_profile") or "")
    if source_profile:
        candidate = Path(source_profile)
        yaml_candidates.append(candidate if candidate.is_absolute() else root / candidate)
    for yaml_path in yaml_candidates:
        document = _load_yaml(yaml_path)
        if not document:
            continue
        cv = document.get("cv") if isinstance(document.get("cv"), dict) else None
        if cv is not None:
            sections = cv.get("sections") if isinstance(cv.get("sections"), dict) else {}
            if any(SKILLS_HEADING_PATTERN.match(str(name).replace("_", " ")) for name in sections):
                standalone_skills = True
            design = document.get("design") if isinstance(document.get("design"), dict) else {}
            theme = str(design.get("theme") or "")
            if theme and theme not in ("engineeringresumes", "classic", "sb2nov", "moderncv"):
                two_column = True
            if "columns" in design or "sidebar" in design:
                two_column = True
        if str(manifest.get("layout") or "").startswith("two_column"):
            two_column = True
    return {"standalone_skills_section": standalone_skills, "two_column_layout": two_column}


def _vacancy_text(opportunity: dict[str, Any]) -> str:
    try:
        source = json.loads(opportunity.get("source_json") or "{}")
    except json.JSONDecodeError:
        source = {}
    parts = [
        str(opportunity.get("title") or ""),
        str(opportunity.get("description") or ""),
        str(opportunity.get("requirements") or ""),
        str(source.get("full_job_description") or source.get("job_description") or ""),
    ]
    return "\n".join(part for part in parts if part.strip())


def _contact_info_findings(cv_text: str) -> dict[str, bool]:
    return {
        "email": bool(EMAIL_PATTERN.search(cv_text)),
        "phone": bool(PHONE_PATTERN.search(cv_text)),
    }


def _unbacked_claims(cv_text: str, knowledge: dict[str, Any]) -> list[str]:
    """Terms in the CV text with no backing in the evidence register knowledge."""
    evidenced = {
        str(skill).casefold()
        for skill in (knowledge.get("evidence_linked_skills", {}) or {})
    }
    flagged = [
        term for term in COMMON_UNEVIDENCED_TECHNOLOGIES
        if _contains_term(cv_text, term) and term.casefold() not in evidenced
    ]
    flagged.extend(
        term for term in BLOCKED_PUBLIC_TERMS if _contains_term(cv_text, term)
    )
    return flagged


def build_review(
    opportunity: dict[str, Any],
    artifact: dict[str, Any],
    root: PathLike = ROOT,
    knowledge_path: PathLike = KNOWLEDGE_PATH,
    evidence_register_path: PathLike = EVIDENCE_REGISTER_PATH,
    cv_text: str | None = None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic senior-recruiter review of one CV artifact vs one opportunity."""
    root = Path(root)
    knowledge = _load_yaml(Path(knowledge_path))
    evidence_register = _load_yaml(Path(evidence_register_path))
    paths = _resolve_artifact_paths(str(artifact.get("path") or ""), root)
    if manifest is None:
        manifest = _read_manifest(paths["manifest"])
    if cv_text is None:
        cv_text = ""
        if paths["text"].is_file():
            cv_text = paths["text"].read_text(encoding="utf-8", errors="replace")

    vacancy = _vacancy_text(opportunity)
    report = requirement_evidence_report(vacancy, knowledge)
    ats_score = float(report.get("keyword_coverage_percent") or 0.0)

    strengths: list[str] = []
    gaps: list[str] = []
    red_flags: list[str] = []
    actions: list[str] = []
    citations: list[dict[str, str]] = []
    covered_in_cv = 0

    for match in report.get("matched_requirements", []):
        candidate_terms = [
            str(match.get("vacancy_term") or ""),
            str(match.get("canonical_skill") or ""),
        ]
        if any(term and _contains_term(cv_text, term) for term in candidate_terms):
            covered_in_cv += 1
            claim = (
                f"Requirement '{match['vacancy_term']}' is covered by evidence-backed skill "
                f"'{match['canonical_skill']}' ({match['evidence_status']}) and appears in the CV."
            )
            strengths.append(claim)
            sources = [str(s) for s in (match.get("evidence_sources") or [])]
            citations.append({
                "claim": claim,
                "source": "; ".join(sources) if sources else "tailoring_knowledge.evidence_linked_skills",
            })
        else:
            gaps.append(
                f"Requirement '{match['vacancy_term']}' is evidence-backed as "
                f"'{match['canonical_skill']}' but absent from the CV text."
            )
            actions.append(
                f"Surface the already evidence-backed skill '{match['canonical_skill']}' "
                f"in the CV to cover the vacancy term '{match['vacancy_term']}'. "
                "Use only wording supported by the evidence register."
            )
    for missing in report.get("missing_skills", []):
        gaps.append(
            f"Vacancy asks for '{missing}', which has no backing in the evidence register."
        )
        actions.append(
            f"Do NOT add '{missing}' to the CV: it is not evidence-backed. "
            "If genuinely relevant, note transferable evidence-backed skills instead."
        )

    company = str(opportunity.get("company") or "")
    if company and _contains_term(cv_text, company):
        claim = f"CV is visibly tailored: target company '{company}' appears in the text."
        strengths.append(claim)
        citations.append({"claim": claim, "source": "cv text sidecar"})

    contact = _contact_info_findings(cv_text)
    if contact["email"] and contact["phone"]:
        claim = "Contact information (email and phone) is present and machine-readable."
        strengths.append(claim)
        citations.append({"claim": claim, "source": "career_master.identity"})
    else:
        missing_bits = [key for key, present in contact.items() if not present]
        red_flags.append("Missing contact info in CV text: " + ", ".join(missing_bits))
        actions.append(
            "Add the missing contact details (" + ", ".join(missing_bits) + ") to the CV header."
        )
    if not cv_text:
        red_flags.append("No extractable ATS text found for the CV artifact.")
        actions.append("Regenerate the CV so a text sidecar (.txt) exists for ATS parsing.")

    layout = _layout_findings(cv_text, paths, manifest, root)
    if layout["standalone_skills_section"]:
        red_flags.append("Standalone skills section detected: ATS and recruiters prefer skills tied to experience bullets.")
        actions.append("Fold the standalone skills list into the 'Skills & technologies' lines of the experience entries.")
    if layout["two_column_layout"]:
        red_flags.append("Two-column layout detected: sidebars break ATS text order; use a one-column layout.")
        actions.append("Re-render with a one-column theme (engineeringresumes/classic).")

    jd_language = detect_language(vacancy)
    cv_language = str(manifest.get("output_language") or "").strip().casefold()
    if cv_language not in ("fr", "en"):
        cv_language = detect_language(cv_text) if cv_text else jd_language
    language_mismatch = bool(cv_text) and cv_language != jd_language
    if language_mismatch:
        red_flags.append(f"Language mismatch: vacancy is '{jd_language}' but the CV reads as '{cv_language}'.")
        actions.append(f"Regenerate the CV in the vacancy language ('{jd_language}') from canonical evidence only.")

    page_count = manifest.get("page_count")
    approved_exception = (
        manifest.get("layout") == "international_two_page_approved_exception"
        or manifest.get("approved_two_page_exception") is True
    )
    if isinstance(page_count, int) and page_count >= 2 and not approved_exception:
        red_flags.append(
            f"CV is {page_count} pages without an approved two-page exception."
        )
        actions.append("Regenerate the CV as one page or record an approved two-page exception.")

    unbacked = _unbacked_claims(cv_text, knowledge)
    if unbacked:
        red_flags.append(
            "Claims not backed by evidence_register.yaml: " + ", ".join(sorted(set(unbacked)))
        )
        actions.append(
            "Remove or rewrite the unbacked claims ("
            + ", ".join(sorted(set(unbacked)))
            + ") — never keep skills or metrics that the evidence register cannot support."
        )

    severe = any(
        flag.startswith("Claims not backed")
        or flag.startswith("No extractable ATS text")
        or "pages without an approved" in flag
        for flag in red_flags
    )
    if severe:
        recommendation = "regenerate"
    elif red_flags or gaps or ats_score < 50.0:
        recommendation = "needs_edits"
    else:
        recommendation = "ready_to_send"

    recognized = int(report.get("recognized_requirements") or 0)
    cv_coverage = round((covered_in_cv / recognized) * 100, 2) if recognized else 0.0

    return {
        "review_schema_version": REVIEW_SCHEMA_VERSION,
        "opportunity_id": opportunity.get("id"),
        "cv_artifact_id": artifact.get("id"),
        "cv_artifact_path": str(artifact.get("path") or ""),
        "artifact_type": artifact.get("artifact_type"),
        "strengths": strengths,
        "gaps": gaps,
        "ats_keyword_coverage_percent": ats_score,
        "ats_cv_coverage_percent": cv_coverage,
        "requirement_evidence": report,
        "red_flags": red_flags,
        "improvement_actions": actions,
        "recommendation": recommendation,
        "jd_language": jd_language,
        "cv_language": cv_language,
        "language_mismatch": language_mismatch,
        "layout_findings": layout,
        "evidence_citations": citations,
        "truthfulness_policy": (
            "Review only. Actions never suggest inventing skills, metrics, or seniority; "
            "all additions must be backed by evidence_register.yaml."
        ),
        "evidence_register_version": evidence_register.get("version"),
        "method": "deterministic_recruiter_review_v2",
    }


def _select_artifact(connection, opportunity_id: str) -> dict[str, Any]:
    row = connection.execute(
        """SELECT * FROM cv_artifacts WHERE opportunity_id=?
           ORDER BY CASE artifact_type WHEN 'tailored' THEN 0 ELSE 1 END, id LIMIT 1""",
        (opportunity_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("no CV artifact found for opportunity")
    return dict(row)


def persist_review(db_path: PathLike, review: dict[str, Any]) -> dict[str, Any]:
    """Upsert so exactly one current review exists per (opportunity_id, cv_artifact_id)."""
    now = datetime.now(timezone.utc).isoformat()
    review_id = stable_id("rev", review["opportunity_id"], review["cv_artifact_id"])
    with closing(connect(db_path)) as connection:
        connection.execute(
            """INSERT INTO recruiter_reviews(
                   id, opportunity_id, cv_artifact_id, recommendation,
                   ats_score, review_json, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(opportunity_id, cv_artifact_id) DO UPDATE SET
                   recommendation=excluded.recommendation,
                   ats_score=excluded.ats_score,
                   review_json=excluded.review_json,
                   updated_at=excluded.updated_at""",
            (
                review_id,
                review["opportunity_id"],
                review["cv_artifact_id"],
                review["recommendation"],
                review["ats_keyword_coverage_percent"],
                json.dumps(review, ensure_ascii=False),
                now,
                now,
            ),
        )
        connection.commit()
        return _review_row(
            connection.execute(
                "SELECT * FROM recruiter_reviews WHERE id=?", (review_id,)
            ).fetchone()
        )


def _review_row(row) -> dict[str, Any]:
    record = dict(row)
    try:
        record["review"] = json.loads(record.pop("review_json") or "{}")
    except json.JSONDecodeError:
        record["review"] = {}
    return record


def list_reviews(db_path: PathLike) -> list[dict[str, Any]]:
    with closing(connect(db_path)) as connection:
        return [
            _review_row(row)
            for row in connection.execute(
                "SELECT * FROM recruiter_reviews ORDER BY updated_at DESC, id"
            )
        ]


def reviews_for_opportunity(db_path: PathLike, opportunity_id: str) -> list[dict[str, Any]]:
    with closing(connect(db_path)) as connection:
        exists = connection.execute(
            "SELECT 1 FROM opportunities WHERE id=?", (opportunity_id,)
        ).fetchone()
        if exists is None:
            raise NotFoundError("opportunity not found")
        return [
            _review_row(row)
            for row in connection.execute(
                "SELECT * FROM recruiter_reviews WHERE opportunity_id=? ORDER BY updated_at DESC, id",
                (opportunity_id,),
            )
        ]


def run_review(
    db_path: PathLike,
    payload: dict[str, Any],
    root: PathLike = ROOT,
    knowledge_path: PathLike = KNOWLEDGE_PATH,
    evidence_register_path: PathLike = EVIDENCE_REGISTER_PATH,
) -> dict[str, Any]:
    """Run and persist a review for an opportunity. Review only; never sends."""
    unknown = set(payload) - {"opportunity_id", "version"}
    if unknown:
        raise ValidationError("only opportunity_id and version are accepted")
    opportunity_id = payload.get("opportunity_id")
    if not isinstance(opportunity_id, str) or not opportunity_id:
        raise ValidationError("opportunity_id is required")
    expected_version = payload.get("version")
    if not isinstance(expected_version, str) or not expected_version:
        raise ValidationError("version is required for every recruiter review request")
    with closing(connect(db_path)) as connection:
        row = connection.execute(
            "SELECT * FROM opportunities WHERE id=?", (opportunity_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("opportunity not found")
        opportunity = dict(row)
        if expected_version != opportunity["updated_at"]:
            raise ConflictError("opportunity changed; reload before retrying")
        artifact = _select_artifact(connection, opportunity_id)
    review = build_review(
        opportunity,
        artifact,
        root=root,
        knowledge_path=knowledge_path,
        evidence_register_path=evidence_register_path,
    )
    return persist_review(db_path, review)


# --------------------------------------------------------------------------- #
# Improvement loop: evidence-only edits, render, re-review, keep the best round #
# --------------------------------------------------------------------------- #

Renderer = Callable[[Path, Path, str], "dict[str, Any] | None"]


def _profile_tokens_sources(knowledge_path: PathLike, evidence_register_path: PathLike,
                            career_master_path: PathLike) -> set[str]:
    return evidence_corpus_tokens(
        Path(career_master_path), Path(knowledge_path), Path(evidence_register_path)
    )


def truthfulness_check(before_text: str, after_text: str, corpus_tokens: set[str]) -> dict[str, Any]:
    """Every token added to the visible CV text must already exist in the evidence corpus.

    Seniority words that were not already in the CV are rejected even when the corpus
    happens to contain them (never inflate seniority).
    """
    added = text_tokens(after_text) - text_tokens(before_text)
    invented = sorted(token for token in added if token not in corpus_tokens)
    seniority = sorted(token for token in added if token in SENIORITY_TOKENS)
    return {
        "ok": not invented and not seniority,
        "added_tokens": sorted(added),
        "invented_tokens": invented,
        "seniority_tokens": seniority,
    }


def _matched_skill_terms(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        match for match in report.get("matched_requirements", [])
        if str(match.get("canonical_skill") or "").strip()
    ]


def _bullet_mentions(bullet: dict[str, Any], terms: list[str]) -> int:
    haystack = " ".join([str(bullet.get("statement") or ""), *map(str, bullet.get("technologies") or [])])
    return sum(_contains_term(haystack, term) for term in terms)


def _source_bullet(profile: dict[str, Any], source: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
    """Resolve 'experience.<id>.bullet_<n>' / 'projects.<id>.bullet_<n>' to (entry, bullet, visible)."""
    parts = str(source).split(".")
    if len(parts) != 3 or parts[0] not in ("experience", "projects") or not parts[2].startswith("bullet_"):
        return None, None, False
    try:
        index = int(parts[2].removeprefix("bullet_")) - 1
    except ValueError:
        return None, None, False
    for entry in profile.get(parts[0]) or []:
        if str(entry.get("id")) != parts[1]:
            continue
        bullets = entry.get("bullets") or []
        target = bullets[index] if 0 <= index < len(bullets) else None
        if target is None:
            return entry, None, False
        visible = entry.get("selected_for_data_ai") is not False
        if parts[0] == "projects":
            visible = visible and bullets.index(target) == 0
        return entry, target, visible
    return None, None, False


def section_texts(profile: dict[str, Any]) -> dict[str, str]:
    """Visible text per CV section of a career-master-shaped (or RenderCV-shaped) profile."""
    if isinstance(profile.get("cv"), dict):  # RenderCV document
        cv = profile["cv"]
        sections = cv.get("sections") if isinstance(cv.get("sections"), dict) else {}
        out = {"summary": "", "experience": "", "projects": "", "education": ""}
        for name, entries in sections.items():
            key = str(name).casefold()
            bucket = next((k for k in ("summary", "experience", "project", "education") if k in key), None)
            if bucket is None:
                continue
            bucket = "projects" if bucket == "project" else bucket
            out[bucket] += "\n" + "\n".join(_string_leaves(entries))
        return out
    variant = profile.get("data_ai_variant") or {}
    summary = "\n".join(str(variant.get(k) or "") for k in ("headline", "summary"))

    def group_text(group: str) -> str:
        parts: list[str] = []
        for entry in profile.get(group) or []:
            if not isinstance(entry, dict) or entry.get("selected_for_data_ai") is False:
                continue
            parts.extend(str(entry.get(k) or "") for k in ("title", "company", "product", "name", "role"))
            for bullet in entry.get("bullets") or []:
                if isinstance(bullet, dict):
                    parts.append(str(bullet.get("statement") or ""))
                    parts.extend(map(str, bullet.get("technologies") or []))
                else:
                    parts.append(str(bullet))
        return "\n".join(parts)

    education = "\n".join(
        " ".join(str(e.get(k) or "") for k in ("institution", "degree", "field"))
        + " " + " ".join(_string_leaves(e.get("highlights") or e.get("courses") or []))
        for e in profile.get("education") or [] if isinstance(e, dict)
    )
    return {"summary": summary, "experience": group_text("experience"),
            "projects": group_text("projects"), "education": education}


def section_coverage(cv_yaml: dict[str, Any], jd_keywords: list[str]) -> dict[str, float]:
    """ResumeCraftr idea: % of JD keywords each section mentions -> {summary, experience, projects, education}."""
    terms = [str(t).strip() for t in jd_keywords or [] if str(t).strip()]
    texts = section_texts(cv_yaml or {})
    if not terms:
        return {section: 0.0 for section in texts}
    return {
        section: round(sum(_contains_term(text, term) for term in terms) / len(terms) * 100, 1)
        for section, text in texts.items()
    }


def weakest_section(coverage: dict[str, float]) -> str:
    """Lowest-coverage section among those propose_edits can act on (experience/projects first)."""
    order = ("experience", "projects", "summary", "education")
    return min(order, key=lambda s: (coverage.get(s, 0.0), order.index(s)))


def propose_edits(
    profile: dict[str, Any],
    opportunity: dict[str, Any],
    review: dict[str, Any],
    corpus_tokens: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (edited profile copy, list of edits). Only evidence-backed, non-inflating edits.

    Section-level ATS: the weakest section (by section_coverage) is targeted first when
    surfacing evidence-backed skills.
    """
    edited = deepcopy(profile)
    edits: list[dict[str, Any]] = []
    report = review.get("requirement_evidence") or {}
    matches = _matched_skill_terms(report)
    jd_terms = sorted({
        term for match in matches
        for term in (str(match.get("canonical_skill")), str(match.get("vacancy_term")))
        if term
    })
    cv_text = profile_visible_text(profile)
    coverage_before = section_coverage(profile, jd_terms)
    target_section = weakest_section(coverage_before)
    # Not appended to ``edits``: the improvement loop stops when edits is empty. Exposed via
    # propose_edits.last_coverage for callers/tests that want the section picture.
    propose_edits.last_coverage = {"before": coverage_before, "target_section": target_section}

    # 1) Surface evidence-backed skills that the vacancy asks for but the CV does not show:
    #    add the canonical skill to the technologies of the very bullet the knowledge cites,
    #    preferring bullets that live in the weakest section.
    for match in matches:
        canonical = str(match["canonical_skill"])
        if _contains_term(cv_text, canonical) or _contains_term(cv_text, str(match.get("vacancy_term") or "")):
            continue
        if not text_tokens(canonical) <= corpus_tokens:
            continue
        sources = [str(s) for s in match.get("evidence_sources") or []]
        sources.sort(key=lambda s: 0 if s.startswith(target_section + ".") else 1)
        for source in sources:
            entry, bullet, visible = _source_bullet(edited, str(source))
            if bullet is None or not visible:
                continue
            technologies = [str(t) for t in (bullet.get("technologies") or [])]
            if any(_contains_term(t, canonical) for t in technologies):
                continue
            technologies.append(canonical)
            bullet["technologies"] = technologies
            edits.append({
                "type": "surface_skill",
                "skill": canonical,
                "vacancy_term": match.get("vacancy_term"),
                "target": str(source),
                "evidence_status": match.get("evidence_status"),
            })
            cv_text = profile_visible_text(edited)
            break

    # 2) Reorder bullets so those mentioning JD keywords the candidate truly has come first.
    if jd_terms:
        for group in ("experience", "projects"):
            for entry in edited.get(group) or []:
                bullets = entry.get("bullets") or []
                if len(bullets) < 2:
                    continue
                ranked = sorted(
                    enumerate(bullets),
                    key=lambda pair: (-_bullet_mentions(pair[1], jd_terms), pair[0]),
                )
                new_order = [index for index, _ in ranked]
                if new_order != list(range(len(bullets))):
                    entry["bullets"] = [bullet for _, bullet in ranked]
                    edits.append({
                        "type": "reorder_bullets",
                        "target": f"{group}.{entry.get('id')}",
                        "order": new_order,
                    })

    # 3) Title line: adopt the vacancy title only when every token is already evidenced and
    #    it does not inflate seniority.
    variant = edited.setdefault("data_ai_variant", {})
    vacancy_title = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", str(opportunity.get("title") or "")).strip(" -–|")
    headline = str(variant.get("headline") or "")
    if vacancy_title and headline:
        title_tokens = text_tokens(vacancy_title)
        head, sep, tail = headline.partition(" | ")
        if (
            title_tokens
            and title_tokens <= corpus_tokens
            and not (title_tokens & SENIORITY_TOKENS)
            and not (title_tokens & text_tokens(" ".join(map(str, COMMON_UNEVIDENCED_TECHNOLOGIES))))
            and head.strip().casefold() != vacancy_title.casefold()
        ):
            variant["headline"] = vacancy_title + (sep + tail if sep else "")
            edits.append({"type": "title_line", "from": headline, "to": variant["headline"]})

    # 4) Tighten summary: drop trailing sentences that carry none of the JD keywords.
    summary = str(variant.get("summary") or "")
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", summary) if s.strip()]
    if len(sentences) > 1 and jd_terms:
        kept = [sentences[0]] + [
            s for s in sentences[1:] if any(_contains_term(s, term) for term in jd_terms)
        ]
        if len(kept) < len(sentences):
            variant["summary"] = " ".join(kept)
            edits.append({"type": "tighten_summary", "removed_sentences": len(sentences) - len(kept)})

    return edited, edits


def profile_to_rendercv(profile: dict[str, Any], language: str = "en") -> dict[str, Any]:
    """Convert a career-master-shaped tailored profile into a one-column RenderCV document."""
    identity = profile.get("identity") or {}
    variant = profile.get("data_ai_variant") or {}
    labels = {
        "en": {"summary": "Summary", "experience": "Professional Experience", "projects": "Selected Projects",
               "education": "Education", "certifications": "Certifications", "tech": "Skills & technologies"},
        "fr": {"summary": "Profil", "experience": "Expérience professionnelle", "projects": "Projets",
               "education": "Formation", "certifications": "Certifications", "tech": "Compétences et technologies"},
    }[language if language in ("en", "fr") else "en"]

    def date(value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value)

    def technologies(entry: dict[str, Any]) -> list[str]:
        seen: list[str] = []
        for bullet in entry.get("bullets") or []:
            for tech in bullet.get("technologies") or []:
                if str(tech) not in seen:
                    seen.append(str(tech))
        return seen

    experience: list[dict[str, Any]] = []
    for entry in profile.get("experience") or []:
        if entry.get("selected_for_data_ai") is False:
            continue
        highlights = [str(b.get("statement") or "") for b in entry.get("bullets") or [] if b.get("statement")]
        tech = technologies(entry)
        if tech:
            highlights.append(f"**{labels['tech']}:** " + ", ".join(tech))
        company = str(entry.get("company") or "")
        if entry.get("product"):
            company += f" · {entry['product']}"
        item = {
            "company": company,
            "position": str(entry.get("title") or ""),
            "location": str(entry.get("location") or ""),
            "start_date": date(entry.get("start_date")),
            "end_date": date(entry.get("end_date")),
            "highlights": highlights,
        }
        experience.append({k: v for k, v in item.items() if v not in (None, "")})

    projects: list[dict[str, Any]] = []
    order = variant.get("project_order") or []
    by_id = {str(p.get("id")): p for p in profile.get("projects") or []}
    ordered = [by_id[i] for i in order if i in by_id] or [
        p for p in profile.get("projects") or [] if p.get("selected_for_data_ai") is not False
    ]
    for entry in ordered:
        bullets = entry.get("bullets") or []
        highlights = [str(bullets[0].get("statement") or "")] if bullets else []
        tech = technologies(entry)
        if tech:
            highlights.append(f"**{labels['tech']}:** " + ", ".join(tech))
        name = str(entry.get("name") or "")
        if entry.get("role"):
            name += f" — {entry['role']}"
        item = {
            "name": name,
            "start_date": date(entry.get("start_date")),
            "end_date": date(entry.get("end_date")),
            "highlights": highlights,
        }
        projects.append({k: v for k, v in item.items() if v not in (None, "")})

    education = []
    for entry in profile.get("education") or []:
        item = {
            "institution": str(entry.get("institution") or ""),
            "area": str(entry.get("field") or ""),
            "degree": str(entry.get("degree") or "")[:20],
            "location": str(entry.get("location") or ""),
            "start_date": date(entry.get("start_date")),
            "end_date": date(entry.get("cv_end_date") or entry.get("end_date")),
        }
        education.append({k: v for k, v in item.items() if v not in (None, "")})

    certifications = [
        f"{c.get('name')} · {c.get('issuer')}" for c in (profile.get("certifications") or [])[:4] if c.get("name")
    ]
    summary_text = str(variant.get("summary") or "") + str((profile.get("tailoring") or {}).get("availability_statement") or "")
    sections: dict[str, Any] = {}
    if variant.get("headline"):
        sections[labels["summary"]] = [f"**{variant['headline']}** — {summary_text.strip()}"]
    elif summary_text.strip():
        sections[labels["summary"]] = [summary_text.strip()]
    if experience:
        sections[labels["experience"]] = experience
    if projects:
        sections[labels["projects"]] = projects
    if education:
        sections[labels["education"]] = education
    if certifications:
        sections[labels["certifications"]] = certifications

    social = []
    if identity.get("linkedin_handle"):
        social.append({"network": "LinkedIn", "username": str(identity["linkedin_handle"])})
    if identity.get("github_handle"):
        social.append({"network": "GitHub", "username": str(identity["github_handle"])})
    cv: dict[str, Any] = {
        "name": str(identity.get("name") or ""),
        "location": str(identity.get("location") or ""),
        "email": str(identity.get("email") or ""),
        "phone": str(identity.get("phone") or ""),
        "social_networks": social,
        "sections": sections,
    }
    return {"cv": {k: v for k, v in cv.items() if v not in ("", [], None)}}


def default_renderer(yaml_path: Path, out_dir: Path, language: str) -> dict[str, Any] | None:
    """Render via cv_render; return None when the renderer toolchain is unavailable."""
    try:
        import cv_render
        import rendercv  # noqa: F401
    except ImportError:
        return None
    try:
        return cv_render.render_cv_yaml(yaml_path, out_dir, language)
    except cv_render.CvRenderError:
        # One-page rule: retry once with the compact density before giving up.
        return cv_render.render_cv_yaml(yaml_path, out_dir, language, density="compact")


def _tailored_profile_path(artifact: dict[str, Any], manifest: dict[str, Any], root: Path) -> Path | None:
    source_profile = str((manifest.get("files") or {}).get("source_profile") or "")
    candidates: list[Path] = []
    if source_profile:
        candidate = Path(source_profile)
        candidates.append(candidate if candidate.is_absolute() else root / candidate)
    pdf = Path(str(artifact.get("path") or ""))
    pdf = pdf if pdf.is_absolute() else root / pdf
    candidates.append(pdf.parent / "source_profiles" / pdf.with_suffix(".yaml").name)
    candidates.append(pdf.with_suffix(".yaml"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _relative(path: Path | str | None, root: Path) -> str | None:
    if path is None:
        return None
    path = Path(path)
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def improvement_loop(
    db_path: PathLike,
    opportunity_id: str,
    max_rounds: int = 3,
    *,
    root: PathLike = ROOT,
    knowledge_path: PathLike = KNOWLEDGE_PATH,
    evidence_register_path: PathLike = EVIDENCE_REGISTER_PATH,
    career_master_path: PathLike = CAREER_MASTER_PATH,
    renderer: Renderer | None = None,
    out_dir: PathLike | None = None,
) -> dict[str, Any]:
    """Review -> evidence-only edits -> render -> re-review; stop on no ATS gain or truth failure.

    Never sends or applies. Persists every round in cv_improvement_rounds and registers the
    best rendered round as the current tailored artifact.
    """
    import yaml

    root = Path(root)
    max_rounds = max(1, min(int(max_rounds), MAX_IMPROVEMENT_ROUNDS))
    renderer = renderer if renderer is not None else default_renderer
    with closing(connect(db_path)) as connection:
        row = connection.execute("SELECT * FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
        if row is None:
            raise NotFoundError("opportunity not found")
        opportunity = dict(row)
        artifact = _select_artifact(connection, opportunity_id)

    paths = _resolve_artifact_paths(str(artifact.get("path") or ""), root)
    manifest = _read_manifest(paths["manifest"])
    profile_path = _tailored_profile_path(artifact, manifest, root)
    if profile_path is None:
        raise NotFoundError("no tailored CV yaml found for the artifact")
    profile = _load_yaml(profile_path)
    if not profile:
        raise ValidationError("tailored CV yaml is empty or invalid")
    language = str(manifest.get("output_language") or "").casefold()
    if language not in ("en", "fr"):
        language = detect_language(_vacancy_text(opportunity))
    corpus = _profile_tokens_sources(knowledge_path, evidence_register_path, career_master_path)

    review_kwargs = dict(root=root, knowledge_path=knowledge_path, evidence_register_path=evidence_register_path)
    baseline = build_review(opportunity, artifact, **review_kwargs)
    baseline_text = ""
    if paths["text"].is_file():
        baseline_text = paths["text"].read_text(encoding="utf-8", errors="replace")
    if not baseline_text.strip():
        baseline_text = profile_visible_text(profile)
        baseline = build_review(opportunity, artifact, cv_text=baseline_text, manifest=manifest, **review_kwargs)

    stem = Path(str(artifact.get("path") or "cv")).stem
    work_dir = Path(out_dir) if out_dir is not None else IMPROVED_DIR / stem
    work_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()

    rounds: list[dict[str, Any]] = []
    current_profile = profile
    current_review = baseline
    stopped_reason = "max_rounds"
    for round_number in range(1, max_rounds + 1):
        ats_before = float(current_review.get("ats_cv_coverage_percent") or 0.0)
        edited, edits = propose_edits(current_profile, opportunity, current_review, corpus)
        if not edits:
            stopped_reason = "no_edits_proposed"
            break
        edited_text = profile_visible_text(edited)
        truth = truthfulness_check(profile_visible_text(current_profile), edited_text, corpus)
        record: dict[str, Any] = {
            "round": round_number,
            "ats_before": ats_before,
            "ats_after": ats_before,
            "edits": edits,
            "truthfulness": truth,
            "yaml_path": None,
            "pdf_path": None,
            "pages": None,
            "accepted": False,
        }
        if not truth["ok"]:
            record["stop"] = "truthfulness_failed"
            rounds.append(record)
            stopped_reason = "truthfulness_failed"
            break

        round_stem = f"{stem}_r{round_number}"
        profile_yaml = work_dir / f"{round_stem}.profile.yaml"
        profile_yaml.write_text(yaml.safe_dump(edited, allow_unicode=True, sort_keys=False), encoding="utf-8")
        rendercv_yaml = work_dir / f"{round_stem}.rendercv.yaml"
        rendercv_yaml.write_text(
            yaml.safe_dump(profile_to_rendercv(edited, language), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        record["yaml_path"] = _relative(profile_yaml, root)
        rendered: dict[str, Any] | None = None
        render_error = None
        try:
            rendered = renderer(rendercv_yaml, work_dir / "render", language)
        except Exception as error:  # CvRenderError (e.g. two pages) or toolchain failure
            render_error = str(error)
        after_text = edited_text
        pdf_path: Path | None = None
        if rendered and rendered.get("pdf_path"):
            pdf_path = work_dir / f"{round_stem}.pdf"
            shutil.copyfile(rendered["pdf_path"], pdf_path)
            text_source = Path(str(rendered.get("text_path") or ""))
            after_text = text_source.read_text(encoding="utf-8", errors="replace") if text_source.is_file() else edited_text
            pdf_path.with_suffix(".txt").write_text(after_text, encoding="utf-8")
            round_manifest = {
                **{k: v for k, v in manifest.items() if k != "files"},
                "page_count": rendered.get("pages"),
                "output_language": language,
                "layout": "international_one_page",
                "improvement_round": round_number,
                "files": {
                    "pdf": _relative(pdf_path, root),
                    "text": _relative(pdf_path.with_suffix(".txt"), root),
                    "source_profile": _relative(profile_yaml, root),
                    "rendercv_yaml": _relative(rendercv_yaml, root),
                },
            }
            pdf_path.with_suffix(".manifest.json").write_text(
                json.dumps(round_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            record["pdf_path"] = _relative(pdf_path, root)
            record["pages"] = rendered.get("pages")
        else:
            record["render_error"] = render_error
            round_manifest = {**manifest, "page_count": None, "output_language": language,
                              "files": {"source_profile": _relative(profile_yaml, root)}}
        round_artifact = {
            **artifact,
            "path": _relative(pdf_path, root) if pdf_path else str(artifact.get("path") or ""),
        }
        after_review = build_review(
            opportunity, round_artifact, cv_text=after_text, manifest=round_manifest, **review_kwargs
        )
        ats_after = float(after_review.get("ats_cv_coverage_percent") or 0.0)
        record["ats_after"] = ats_after
        record["recommendation"] = after_review.get("recommendation")
        if render_error and "pages" in render_error:
            record["stop"] = "page_limit"
            rounds.append(record)
            stopped_reason = "page_limit_exceeded"
            break
        if ats_after <= ats_before:
            record["stop"] = "no_gain"
            rounds.append(record)
            stopped_reason = "no_ats_gain"
            break
        record["accepted"] = True
        rounds.append(record)
        current_profile, current_review = edited, after_review

    accepted = [r for r in rounds if r.get("accepted")]
    best_round = max(accepted, key=lambda r: (r["ats_after"], r["round"]))["round"] if accepted else None
    registered = None
    if best_round is not None:
        best = next(r for r in rounds if r["round"] == best_round)
        if best.get("pdf_path"):
            label = f"Tailored · improved r{best_round}"
            registered = pipeline_v2.register_cv_artifact(db_path, opportunity_id, best["pdf_path"], label)
            persist_review(db_path, build_review(opportunity, registered, **review_kwargs))

    with closing(connect(db_path)) as connection:
        connection.execute("DELETE FROM cv_improvement_rounds WHERE opportunity_id=?", (opportunity_id,))
        for record in rounds:
            extra = {k: v for k, v in record.items()
                     if k not in ("round", "ats_before", "ats_after", "yaml_path", "pdf_path")}
            connection.execute(
                """INSERT INTO cv_improvement_rounds(
                       id, opportunity_id, round, ats_before, ats_after, edits_json, yaml_path, pdf_path, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    stable_id("imp", opportunity_id, record["round"], now),
                    opportunity_id,
                    record["round"],
                    record["ats_before"],
                    record["ats_after"],
                    json.dumps(extra, ensure_ascii=False),
                    record.get("yaml_path"),
                    record.get("pdf_path"),
                    now,
                ),
            )
        connection.commit()

    return {
        "opportunity_id": opportunity_id,
        "baseline_ats": float(baseline.get("ats_cv_coverage_percent") or 0.0),
        "baseline_keyword_coverage": float(baseline.get("ats_keyword_coverage_percent") or 0.0),
        "rounds": rounds,
        "best_round": best_round,
        "artifact": registered or artifact,
        "stopped_reason": stopped_reason,
        "policy": "evidence-only edits; never sends or applies",
    }


def improvements_for_opportunity(db_path: PathLike, opportunity_id: str) -> dict[str, Any]:
    with closing(connect(db_path)) as connection:
        if connection.execute("SELECT 1 FROM opportunities WHERE id=?", (opportunity_id,)).fetchone() is None:
            raise NotFoundError("opportunity not found")
        rows = connection.execute(
            "SELECT * FROM cv_improvement_rounds WHERE opportunity_id=? ORDER BY round",
            (opportunity_id,),
        ).fetchall()
        artifact_row = connection.execute(
            """SELECT * FROM cv_artifacts WHERE opportunity_id=?
               ORDER BY CASE artifact_type WHEN 'tailored' THEN 0 ELSE 1 END, id LIMIT 1""",
            (opportunity_id,),
        ).fetchone()
    rounds = []
    for row in rows:
        record = dict(row)
        try:
            extra = json.loads(record.pop("edits_json") or "{}")
        except json.JSONDecodeError:
            extra = {}
        if isinstance(extra, list):
            extra = {"edits": extra}
        record.update(extra)
        record.setdefault("edits", [])
        rounds.append(record)
    accepted = [r for r in rounds if r.get("accepted")]
    best_round = max(accepted, key=lambda r: (r["ats_after"], r["round"]))["round"] if accepted else None
    return {
        "opportunity_id": opportunity_id,
        "rounds": rounds,
        "best_round": best_round,
        "artifact": dict(artifact_row) if artifact_row else None,
    }


def run_improvement(
    db_path: PathLike,
    payload: dict[str, Any],
    root: PathLike = ROOT,
    **kwargs: Any,
) -> dict[str, Any]:
    """HTTP entry: validate version, then run the improvement loop. Never sends."""
    unknown = set(payload) - {"opportunity_id", "version", "max_rounds"}
    if unknown:
        raise ValidationError("only opportunity_id, version and max_rounds are accepted")
    opportunity_id = payload.get("opportunity_id")
    if not isinstance(opportunity_id, str) or not opportunity_id:
        raise ValidationError("opportunity_id is required")
    expected_version = payload.get("version")
    if not isinstance(expected_version, str) or not expected_version:
        raise ValidationError("version is required for every improvement request")
    max_rounds = payload.get("max_rounds", 3)
    if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or not 1 <= max_rounds <= MAX_IMPROVEMENT_ROUNDS:
        raise ValidationError(f"max_rounds must be an integer between 1 and {MAX_IMPROVEMENT_ROUNDS}")
    with closing(connect(db_path)) as connection:
        row = connection.execute("SELECT updated_at FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
        if row is None:
            raise NotFoundError("opportunity not found")
        if expected_version != row["updated_at"]:
            raise ConflictError("opportunity changed; reload before retrying")
        _select_artifact(connection, opportunity_id)  # 404 when no CV
    return improvement_loop(db_path, opportunity_id, max_rounds, root=root, **kwargs)
