from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

try:
    from scripts.build_reference_cv import render_reference
except ModuleNotFoundError:
    from build_reference_cv import render_reference

ROOT = Path(__file__).resolve().parents[1]
CV_ROOT = ROOT.parent
PROFILE_PATH = ROOT / "data" / "career_master.yaml"
EVIDENCE_PATH = ROOT / "data" / "evidence_register.yaml"
TAILORING_KNOWLEDGE_PATH = ROOT / "data" / "tailoring_knowledge.yaml"
JOBS_PATH = CV_ROOT / "jobs_digest.json"
OUTPUT_DIR = ROOT / "out" / "tailored"
PROFILE_OUTPUT_DIR = OUTPUT_DIR / "source_profiles"
PHOTO_TEMPLATE = ROOT / "templates" / "data_ai_cv_morocco_photo.html.j2"
INTERNATIONAL_TEMPLATE = ROOT / "templates" / "data_ai_cv_international.html.j2"
INTERNATIONAL_ONE_PAGE_TEMPLATE = ROOT / "templates" / "data_ai_cv.html.j2"
TAILORING_VERSION = 4

ARCHETYPES: dict[str, dict[str, Any]] = {
    "ai_engineer_genai": {
        "display_name": "AI Engineer / GenAI",
        "headline": "AI Engineer | RAG, AI Agents, Applied AI & Cloud",
        "summary": (
            "AI Engineer building RAG applications, AI agents, and applied AI services with Python, "
            "FastAPI, LangGraph, LangChain, and vector search. Experience spans cloud deployment, "
            "client delivery, production support, and technical team leadership."
        ),
        "projects": ["realestate_rag", "litflow", "casamotion"],
        "keywords": {
            "ai": 3, "artificial intelligence": 4, "ia": 3, "genai": 5, "generative": 4,
            "llm": 5, "agent": 4, "rag": 5, "retrieval": 3, "nlp": 3, "vision": 2,
            "machine learning": 2, "ml": 1,
        },
    },
    "data_engineer": {
        "display_name": "Data Engineer",
        "headline": "Data Engineer | Streaming, Data Platforms, APIs & Cloud",
        "summary": (
            "Data Engineer building streaming pipelines, data platforms, and APIs with Python, SQL, "
            "Kafka, Flink, Spark, PostgreSQL, and cloud services. Experience spans client delivery, "
            "data-quality workflows, production support, and applied AI products."
        ),
        "projects": ["casamotion", "job_intelligent", "litflow"],
        "keywords": {
            "data engineer": 7, "data engineering": 7, "pipeline": 4, "ingestion": 4,
            "etl": 5, "elt": 5, "streaming": 4, "kafka": 4, "flink": 4, "spark": 4,
            "pyspark": 4, "sql": 3, "database": 3, "warehouse": 4, "big data": 4,
            "data governance": 4, "erp": 2, "bi": 2, "cloud data": 3,
        },
    },
    "ml_data_science": {
        "display_name": "ML / Data Science",
        "headline": "ML & Data Engineer | Applied ML, Time Series, Geospatial & Data Products",
        "summary": (
            "ML and Data Engineer building machine-learning and data products for time-series, "
            "geospatial, streaming, and computer-vision use cases. Uses Python, XGBoost, TensorFlow, "
            "Spark MLlib, RF-DETR, and FastAPI to turn data workflows into usable applications."
        ),
        "projects": ["aethersignal", "casamotion", "job_intelligent"],
        "keywords": {
            "data scientist": 7, "data science": 7, "machine learning": 6, "ml": 3,
            "model": 3, "predictive": 4, "analytics": 4, "analyst": 4, "research": 2,
            "statistics": 4, "forecast": 4, "xgboost": 4, "tensorflow": 4,
            "computer vision": 3,
        },
    },
    "software_backend_ai": {
        "display_name": "Software / Backend AI",
        "headline": "Backend AI Engineer | FastAPI, Agent Services, Cloud & Product Delivery",
        "summary": (
            "Backend AI Engineer building Python APIs, agent services, and full-stack AI products "
            "with FastAPI, PostgreSQL, Docker, React, and Angular. Experience includes cloud "
            "deployment, production support, client delivery, and technical team leadership."
        ),
        "projects": ["job_intelligent", "litflow", "realestate_rag"],
        "keywords": {
            "software": 6, "developer": 5, "development": 3, "backend": 6,
            "full stack": 6, "fullstack": 6, "api": 4, "fastapi": 5, "microservice": 4,
            "react": 3, "angular": 3, "java": 2, "spring": 2, "automation": 3,
            "product": 2, "docker": 2, "cloud": 2,
        },
    },
}

BLOCKED_PUBLIC_TERMS = [
    "YOLO", "blink-rate", "AUC 0.913", "PR-AUC 0.867", "PDF reporting",
    "35% matching", "Orange Summer Challenge", "Netix",
]

TOKEN_PATTERN = re.compile(r"[a-z0-9+#.]+", re.IGNORECASE)


def slugify(value: str, limit: int = 72) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_")
    return value[:limit].strip("_") or "role"


def is_morocco_location(location: str) -> bool:
    text = location.casefold()
    return any(token in text for token in ("morocco", "maroc", "casablanca", "rabat", "marrakech", "tanger"))


def layout_policy(job: dict[str, Any]) -> tuple[Path, int, str]:
    if job.get("approved_two_page_exception") is True:
        return INTERNATIONAL_TEMPLATE, 2, "international_two_page_approved_exception"
    if is_morocco_location(str(job.get("location", ""))):
        return PHOTO_TEMPLATE, 1, "morocco_photo_one_page"
    return INTERNATIONAL_ONE_PAGE_TEMPLATE, 1, "international_one_page"


def detect_output_language(job: dict[str, Any], text: str) -> str:
    for key in ("output_language", "job_language", "language"):
        explicit = str(job.get(key, "")).strip().casefold()
        if explicit:
            if explicit == "fr" or explicit.startswith("fr-") or "french" in explicit or "français" in explicit or "francais" in explicit:
                return "fr"
            if explicit == "en" or explicit.startswith("en-") or "english" in explicit or "anglais" in explicit:
                return "en"

    normalized = text.casefold()
    french_signals = (
        "missions", "profil recherché", "profil recherche", "compétences", "competences",
        "expérience", "experience", "vous serez", "vous aurez", "nous recherchons",
        "développer", "developper", "ingénieur", "ingenieur",
    )
    english_signals = (
        "responsibilities", "requirements", "qualifications", "you will", "we are looking",
        "experience with", "skills", "engineer",
    )
    french_score = sum(signal in normalized for signal in french_signals)
    english_score = sum(signal in normalized for signal in english_signals)
    return "fr" if french_score > english_score else "en"


def is_substantive_vacancy_description(text: str) -> bool:
    normalized = text.casefold()
    responsibility_signal = re.search(
        r"\b(responsibilit(?:y|ies)|missions?|your role|what you(?:'|’)ll do|vous (?:serez|aurez)|rôle)\b",
        normalized,
    )
    requirement_signal = re.search(
        r"\b(requirements?|qualifications?|skills?|profile|profil|compétences?|expérience)\b",
        normalized,
    )
    return len(text.strip()) >= 200 and bool(responsibility_signal and requirement_signal)


def vacancy_text(job: dict[str, Any], description_override: str | None = None) -> tuple[str, str]:
    if description_override and description_override.strip():
        text = description_override.strip()
        return text, "exact_vacancy" if is_substantive_vacancy_description(text) else "role_family"
    description = str(job.get("description", "")).strip()
    if description:
        return description, "exact_vacancy" if is_substantive_vacancy_description(description) else "role_family"
    summary = str(job.get("summary", "")).strip()
    if summary:
        return " ".join([str(job.get("title", "")), summary]), "role_family"
    return str(job.get("title", "")).strip(), "role_family"


def opportunity_track(job: dict[str, Any], text: str = "") -> str:
    title = str(job.get("title", "")).casefold()
    contract = " ".join([
        title, str(job.get("opportunity_type", "")),
        str(job.get("job_type", "")), str(job.get("type", "")),
    ]).casefold()
    pfe_terms = ("pfe", "stage de fin d'études", "stage de fin d'etudes", "final-year internship", "final year internship", "end-of-studies internship")
    internship_terms = ("internship", "intern ", " intern", "stage ", "stagiaire", "co-op", "coop", "student placement", "alternance")
    professional_terms = ("permanent", "full-time", "full time", "fixed-term", "fixed term", "contractor", "normal job", "graduate programme", "graduate program", "cdi")
    if any(term in contract for term in pfe_terms):
        return "pfe_internship"
    if any(term in contract for term in internship_terms):
        return "internship"
    if any(term in contract for term in professional_terms):
        return "professional_role"
    context = " ".join([str(job.get("pfe_fit", "")), text]).casefold()
    context = re.sub(r"\b(?:not|non|pas)\s+(?:an?\s+|un\s+)?pfe(?:\s+internship)?", "", context)
    if any(term in context for term in pfe_terms):
        return "pfe_internship"
    if any(term in context for term in internship_terms):
        return "internship"
    return "professional_role"


def tailoring_source_hash(
    job: dict[str, Any],
    description_override: str | None = None,
    *,
    profile_path: Path | None = None,
    evidence_path: Path | None = None,
    knowledge_path: Path | None = None,
    template_path: Path | None = None,
) -> str:
    text, basis = vacancy_text(job, description_override)
    selected_template, expected_pages, layout_name = layout_policy(job)
    sources = {
        "career_master": profile_path or PROFILE_PATH,
        "evidence_register": evidence_path or EVIDENCE_PATH,
        "tailoring_knowledge": knowledge_path or TAILORING_KNOWLEDGE_PATH,
        "selected_template": template_path or selected_template,
    }
    source_digests = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in sources.items()
    }
    vacancy_input = {
        key: value for key, value in job.items()
        if not key.startswith("tailoring_") and key not in {
            "tailored_cv", "tailored_cv_label", "tailored_cv_reason"
        }
    }
    payload = {
        "generator_version": TAILORING_VERSION,
        "vacancy_input": vacancy_input,
        "description_override": description_override,
        "resolved_text": text,
        "classification": basis,
        "opportunity_track": opportunity_track(job, text),
        "output_language": detect_output_language(job, text),
        "page_policy": {
            "one_page_required": job.get("one_page_required"),
            "approved_two_page_exception": job.get("approved_two_page_exception") is True,
            "expected_pages": expected_pages,
            "layout": layout_name,
        },
        "source_digests": source_digests,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def classify_archetype(job: dict[str, Any], text: str) -> tuple[str, dict[str, int]]:
    haystack = " ".join([
        str(job.get("title", "")), str(job.get("company", "")), text
    ]).casefold()
    scores: dict[str, int] = {}
    for key, config in ARCHETYPES.items():
        score = 0
        for keyword, weight in config["keywords"].items():
            if re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", haystack):
                score += weight
        scores[key] = score

    title = str(job.get("title", "")).casefold()
    if "data engineer" in title:
        scores["data_engineer"] += 8
    if "data scientist" in title or "data analyst" in title:
        scores["ml_data_science"] += 8
    if any(term in title for term in ("software", "developer", "full stack", "fullstack", "backend")):
        scores["software_backend_ai"] += 8
    if any(term in title for term in ("ai engineer", "ia engineer", "intelligence artificielle", "generative ai", "générative")):
        scores["ai_engineer_genai"] += 8

    priority = ["ai_engineer_genai", "data_engineer", "software_backend_ai", "ml_data_science"]
    winner = max(priority, key=lambda key: (scores[key], -priority.index(key)))
    if scores[winner] == 0:
        winner = "ai_engineer_genai"
    return winner, scores


def text_tokens(value: str) -> set[str]:
    stop = {
        "and", "the", "for", "with", "from", "into", "that", "this", "role", "intern",
        "internship", "engineer", "engineering", "stage", "stagiaire", "junior", "entry",
        "level", "data", "ai", "work", "team", "build", "built", "developed", "developing",
    }
    return {token.casefold() for token in TOKEN_PATTERN.findall(value) if len(token) > 2 and token.casefold() not in stop}


COMMON_UNEVIDENCED_TECHNOLOGIES = (
    "Kubernetes", "Terraform", "Databricks", "Snowflake", "dbt", "Scala", "Golang",
    "Go", "Rust", "C++", "Jenkins", "Looker", "MongoDB", "Django", ".NET",
)


def _contains_term(text: str, term: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(term.casefold())}(?!\w)", text.casefold()))


def requirement_evidence_report(text: str, knowledge: dict[str, Any]) -> dict[str, Any]:
    """Map recognized vacancy technologies to canonical evidence without inferring skills."""
    evidence_skills = knowledge.get("evidence_linked_skills", {}) or {}
    synonyms: dict[str, list[str]] = {}
    for entry in ((knowledge.get("safe_keyword_synonyms", {}) or {}).get("equivalents", []) or []):
        canonical = str(entry.get("canonical", "")).strip()
        if canonical:
            synonyms[canonical] = [str(term) for term in entry.get("safe_terms", []) if str(term).strip()]

    matched: list[dict[str, Any]] = []
    recognized_terms: set[str] = set()
    for canonical, evidence in evidence_skills.items():
        terms = [str(canonical), *synonyms.get(str(canonical), [])]
        present = next((term for term in sorted(terms, key=len, reverse=True) if _contains_term(text, term)), None)
        if not present:
            continue
        recognized_terms.add(str(canonical).casefold())
        matched.append({
            "vacancy_term": present,
            "canonical_skill": str(canonical),
            "evidence_status": str(evidence.get("strongest_status", "unknown")),
            "evidence_sources": list(evidence.get("sources", [])),
            "caveat": str(evidence.get("caveat", "")),
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


def bullet_score(bullet: dict[str, Any], target_tokens: set[str]) -> int:
    source = " ".join([
        str(bullet.get("statement", "")),
        " ".join(map(str, bullet.get("technologies", []))),
        " ".join(map(str, bullet.get("role_tags", []))),
    ])
    overlap = len(text_tokens(source) & target_tokens)
    status_bonus = {
        "verified": 4,
        "verified_from_local_repository": 4,
        "verified_from_git_history": 4,
        "user_confirmed": 3,
        "resolved_for_candidate": 2,
        "user_provided_cv": 1,
    }.get(str(bullet.get("evidence_status", "")), 0)
    outcome_bonus = 2 if bullet.get("metrics") else 0
    return overlap * 5 + status_bonus + outcome_bonus


def project_score(project: dict[str, Any], target_tokens: set[str], preferred_rank: int) -> int:
    source = " ".join([
        str(project.get("name", "")), str(project.get("role", "")),
        " ".join(str(b.get("statement", "")) for b in project.get("bullets", [])),
        " ".join(str(t) for b in project.get("bullets", []) for t in b.get("technologies", [])),
    ])
    return (30 - preferred_rank * 5) + len(text_tokens(source) & target_tokens) * 4


def tailor_profile(profile: dict[str, Any], job: dict[str, Any], archetype: str, text: str, basis: str) -> tuple[dict[str, Any], list[str]]:
    tailored = copy.deepcopy(profile)
    config = ARCHETYPES[archetype]
    target_tokens = text_tokens(" ".join([str(job.get("title", "")), text]))
    tailored["data_ai_variant"]["headline"] = config["headline"]
    tailored["data_ai_variant"]["summary"] = config["summary"]

    for experience in tailored["experience"]:
        experience["selected_for_data_ai"] = True
        indexed = list(enumerate(experience.get("bullets", [])))
        indexed.sort(key=lambda pair: (-bullet_score(pair[1], target_tokens), pair[0]))
        experience["bullets"] = [bullet for _, bullet in indexed]

    projects_by_id = {project["id"]: project for project in tailored["projects"]}
    preferred = list(config["projects"])
    candidates = [projects_by_id[project_id] for project_id in preferred if project_id in projects_by_id]
    ranked = sorted(
        enumerate(candidates),
        key=lambda pair: (-project_score(pair[1], target_tokens, pair[0]), pair[0]),
    )
    selected_ids = [project["id"] for _, project in ranked[:3]]
    for project in tailored["projects"]:
        project["selected_for_data_ai"] = project["id"] in selected_ids
        indexed = list(enumerate(project.get("bullets", [])))
        indexed.sort(key=lambda pair: (-bullet_score(pair[1], target_tokens), pair[0]))
        project["bullets"] = [bullet for _, bullet in indexed]
    tailored["data_ai_variant"]["project_order"] = selected_ids
    track = opportunity_track(job, text)
    availability_statement = {
        "pfe_internship": " Available for a PFE internship from January 2027.",
        "internship": " Available for an internship from January 2027.",
        "professional_role": "",
    }[track]
    tailored["tailoring"] = {
        "target_role": job.get("title"),
        "target_company": job.get("company"),
        "source_url": job.get("link"),
        "archetype": archetype,
        "basis": basis,
        "opportunity_track": track,
        "positioning": "experienced_professional" if track == "professional_role" else "role_relevant_candidate",
        "availability_statement": availability_statement,
        "manual_approval_required": True,
        "submission_mode": "manual_only",
    }
    return tailored, selected_ids


def find_chrome() -> Path:
    candidates = [
        os.environ.get("CHROME_PATH"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise FileNotFoundError("Chrome or Edge was not found; set CHROME_PATH.")


def render_pdf(html_path: Path, pdf_path: Path) -> None:
    chrome = find_chrome()
    command = [
        str(chrome), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
        "--allow-file-access-from-files", f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri(),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode != 0 or not pdf_path.exists():
        raise RuntimeError(f"PDF rendering failed ({result.returncode}): {result.stderr}")


def pdf_page_count(pdf_path: Path) -> int:
    result = subprocess.run(["pdfinfo", str(pdf_path)], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    match = re.search(r"^Pages:\s+(\d+)", result.stdout, re.MULTILINE)
    if not match:
        raise RuntimeError(f"Could not read page count for {pdf_path}")
    return int(match.group(1))


def extract_pdf_text(pdf_path: Path, text_path: Path) -> str:
    result = subprocess.run(["pdftotext", "-enc", "UTF-8", "-layout", str(pdf_path), str(text_path)], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return text_path.read_text(encoding="utf-8", errors="replace")


def build_one(
    job: dict[str, Any],
    profile: dict[str, Any],
    description_override: str | None = None,
    *,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    text, basis = vacancy_text(job, description_override)
    output_language = detect_output_language(job, text)
    canonical_profile_language = str(
        profile.get("document_language") or profile.get("language") or "en"
    ).strip().casefold()
    canonical_profile_language = "fr" if canonical_profile_language.startswith("fr") else "en"
    language_policy_status = (
        "matched" if output_language == canonical_profile_language else "manual_translation_required"
    )
    knowledge = yaml.safe_load(TAILORING_KNOWLEDGE_PATH.read_text(encoding="utf-8")) or {}
    requirement_report = requirement_evidence_report(text, knowledge) if basis == "exact_vacancy" else {
        "matched_requirements": [],
        "missing_skills": [],
        "recognized_requirements": 0,
        "keyword_coverage_percent": 0.0,
        "method": "not_run_role_family_requires_complete_job_description",
    }
    source_hash = tailoring_source_hash(job, description_override, profile_path=profile_path)
    archetype, scores = classify_archetype(job, text)
    tailored_profile, selected_projects = tailor_profile(profile, job, archetype, text, basis)
    template, expected_pages, layout_name = layout_policy(job)

    digest = hashlib.sha256(str(job.get("link", job.get("title", ""))).encode("utf-8")).hexdigest()[:8]
    stem = slugify(f"Mohamed_Amine_El_Abidi_{job.get('company', '')}_{job.get('title', '')}", 86) + f"_{digest}"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    profile_path = PROFILE_OUTPUT_DIR / f"{stem}.yaml"
    html_path = OUTPUT_DIR / f"{stem}.html"
    pdf_path = OUTPUT_DIR / f"{stem}.pdf"
    text_path = OUTPUT_DIR / f"{stem}.txt"
    manifest_path = OUTPUT_DIR / f"{stem}.manifest.json"

    profile_path.write_text(yaml.safe_dump(tailored_profile, allow_unicode=True, sort_keys=False), encoding="utf-8")
    html = render_reference(profile_path, template, "selected_for_data_ai")
    html_path.write_text(html, encoding="utf-8")
    visible_html = re.sub(r"data:image/[^;]+;base64,[^\"']+", "", html, flags=re.IGNORECASE)
    for blocked in BLOCKED_PUBLIC_TERMS:
        if blocked.casefold() in visible_html.casefold():
            raise ValueError(f"Blocked public term found in rendered CV: {blocked}")
    render_pdf(html_path, pdf_path)
    pages = pdf_page_count(pdf_path)
    if pages != expected_pages:
        raise ValueError(f"Expected {expected_pages} page(s), got {pages}: {pdf_path.name}")
    extracted = extract_pdf_text(pdf_path, text_path)
    if job.get("company") and str(job["company"]).casefold() in extracted.casefold():
        # Company names are not intentionally inserted into CV content; canonical experience may legitimately match.
        target_company_in_text = True
    else:
        target_company_in_text = False
    if len(extracted.strip()) < 1000:
        raise ValueError(f"ATS text extraction is unexpectedly short: {pdf_path.name}")

    relative_pdf = pdf_path.relative_to(CV_ROOT).as_posix()
    relative_manifest = manifest_path.relative_to(CV_ROOT).as_posix()
    manifest = {
        "candidate": profile["identity"]["name"],
        "target": {
            "title": job.get("title"), "company": job.get("company"),
            "location": job.get("location"), "link": job.get("link"),
        },
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tailoring_basis": basis,
        "output_language": output_language,
        "language_policy": {
            "vacancy_language": output_language,
            "canonical_profile_language": canonical_profile_language,
            "status": language_policy_status,
        },
        "requirement_evidence": requirement_report,
        "tailoring_version": TAILORING_VERSION,
        "opportunity_track": tailored_profile["tailoring"]["opportunity_track"],
        "positioning": tailored_profile["tailoring"]["positioning"],
        "archetype": archetype,
        "archetype_display": ARCHETYPES[archetype]["display_name"],
        "classification_scores": scores,
        "selected_projects": selected_projects,
        "layout": layout_name,
        "page_count": pages,
        "ats_text_characters": len(extracted.strip()),
        "target_company_appears_in_text": target_company_in_text,
        "guardrails": {
            "canonical_profile_only": True,
            "blocked_claims_checked": BLOCKED_PUBLIC_TERMS,
            "manual_approval_required": True,
            "application_submitted": False,
            "message_sent": False,
        },
        "files": {
            "pdf": relative_pdf,
            "html": html_path.relative_to(CV_ROOT).as_posix(),
            "text": text_path.relative_to(CV_ROOT).as_posix(),
            "source_profile": profile_path.relative_to(CV_ROOT).as_posix(),
        },
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    job["tailored_cv"] = relative_pdf
    job["tailored_cv_label"] = f"Tailored · {ARCHETYPES[archetype]['display_name']}"
    job["tailored_cv_reason"] = (
        f"Generated from {basis.replace('_', ' ')} using verified career evidence only; "
        f"{expected_pages} page{'s' if expected_pages != 1 else ''}."
    )
    job["tailoring_basis"] = basis
    job["tailoring_archetype"] = archetype
    job["tailoring_manifest"] = relative_manifest
    job["tailoring_status"] = (
        "language_review_required"
        if language_policy_status != "matched"
        else "candidate_pending_user_approval"
    )
    job["tailoring_manual_approval_required"] = True
    job["tailoring_source_hash"] = source_hash
    return manifest


def persist_jobs_payload(path: Path, payload: dict[str, Any]) -> bool:
    """Persist only legacy mutable inputs; generated v2 snapshots are read-only."""
    if payload.get("generated_read_only") is True:
        return False
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def sync_generated_artifacts(jobs_path: Path, payload: dict[str, Any], jobs: list[dict]) -> int:
    if payload.get("generated_read_only") is not True or not jobs:
        return 0
    project_root = jobs_path.parent
    database = project_root / "career_pipeline_v2.sqlite3"
    if not database.is_file():
        raise RuntimeError("Career Pipeline v2 database is required for generated snapshot inputs")
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    import pipeline_v2

    count = 0
    for job in jobs:
        opportunity_id = str(job.get("stable_id") or "")
        artifact_path = str(job.get("tailored_cv") or "")
        if not opportunity_id or not artifact_path:
            continue
        pipeline_v2.register_cv_artifact(
            database, opportunity_id, artifact_path, str(job.get("tailored_cv_label") or "Tailored CV")
        )
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate evidence-safe role-tailored CV candidates and update the opportunity dashboard data.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="Tailor a CV candidate for every job in jobs_digest.json")
    target.add_argument("--job-link", help="Tailor one dashboard job selected by its exact link")
    parser.add_argument("--description", help="Full job description for a single --job-link")
    parser.add_argument("--description-file", type=Path, help="UTF-8 job-description file for a single --job-link")
    parser.add_argument("--changed-only", action="store_true", help="Generate only new, missing, or source-changed tailored CVs")
    parser.add_argument("--jobs", type=Path, default=JOBS_PATH)
    parser.add_argument("--profile", type=Path, default=PROFILE_PATH)
    args = parser.parse_args()

    if args.all and (args.description or args.description_file):
        parser.error("--description and --description-file can only be used with --job-link")
    if args.description and args.description_file:
        parser.error("Use only one of --description or --description-file")

    jobs_path = args.jobs.resolve()
    profile_path = args.profile.resolve()
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs = payload.get("jobs", [])
    if args.job_link:
        selected = [job for job in jobs if job.get("link") == args.job_link]
        if not selected:
            raise SystemExit(f"No dashboard job found for link: {args.job_link}")
    else:
        selected = jobs

    description_override = args.description
    if args.description_file:
        description_override = args.description_file.read_text(encoding="utf-8")

    if args.changed_only:
        selected = [
            job for job in selected
            if job.get("tailoring_source_hash") != tailoring_source_hash(
                job,
                description_override if args.job_link else None,
                profile_path=profile_path,
            )
            or not job.get("tailored_cv")
            or not (CV_ROOT / str(job.get("tailored_cv", ""))).is_file()
        ]

    manifests = []
    for index, job in enumerate(selected, 1):
        manifests.append(build_one(
            job,
            profile,
            description_override if args.job_link else None,
            profile_path=profile_path,
        ))
        print(f"[{index}/{len(selected)}] {job.get('company')} · {job.get('title')} -> {job.get('tailored_cv')}")

    if selected:
        payload["updated"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    payload["tailoring_agent"] = {
        "version": 1,
        "script": "reference_cv_2027/scripts/tailor_cv_agent.py",
        "generated_count": len(selected),
        "manual_approval_required": True,
        "submission_mode": "manual_only",
        "note": "Generates candidate CV files only. It never applies, submits, sends, or changes application status.",
    }
    sqlite_artifacts_registered = sync_generated_artifacts(jobs_path, payload, selected)
    persisted = persist_jobs_payload(jobs_path, payload)
    print(json.dumps({
        "generated": len(manifests),
        "morocco_one_page": sum(m["page_count"] == 1 for m in manifests),
        "international_two_page": sum(m["page_count"] == 2 for m in manifests),
        "manual_approval_required": True,
        "json_snapshot_updated": persisted,
        "sqlite_artifacts_registered": sqlite_artifacts_registered,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
