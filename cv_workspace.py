"""CV Workspace: list, inspect, and locally generate tailored CV artifacts.

Reads career_pipeline_v2.sqlite3 plus reference_cv_2027 tailoring manifests and
exposes pure functions that pipeline_v2's HTTP layer dispatches to. Generation
runs the local tailor_cv_agent pipeline only (no network, no sending) and
registers results through pipeline_v2.register_cv_artifact.
"""
from __future__ import annotations

import importlib
import json
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pipeline_v2
from pipeline_v2 import (
    ConflictError,
    NotFoundError,
    ValidationError,
    connect,
)

PROJECT_ROOT = Path(__file__).resolve().parent
SUPPORTED_LANGUAGES = {"en", "fr"}
ALLOWED_FILTERS = {"company", "status", "classification", "language", "has_cv", "q"}


def safe_artifact_path(project_root: Path, relative_path: str) -> Path:
    """Resolve an artifact path and refuse anything outside the project root."""
    candidate = str(relative_path or "").strip()
    if not candidate:
        raise ValidationError("artifact path is required")
    root = Path(project_root).resolve()
    resolved = (root / candidate).resolve()
    if root != resolved and root not in resolved.parents:
        raise ValidationError("artifact paths must stay inside the project root")
    return resolved


def _load_manifest(project_root: Path, manifest_path: str) -> dict[str, Any]:
    try:
        resolved = safe_artifact_path(project_root, manifest_path)
    except ValidationError:
        return {}
    if not resolved.is_file():
        return {}
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _artifact_manifest(project_root: Path, artifact_path: str) -> dict[str, Any]:
    candidate = str(artifact_path or "")
    if candidate.casefold().endswith(".pdf"):
        manifest_path = candidate[: -len(".pdf")] + ".manifest.json"
        return _load_manifest(project_root, manifest_path)
    return {}


def _row_summary(project_root: Path, row: dict[str, Any], artifacts: list[dict]) -> dict[str, Any]:
    manifest: dict[str, Any] = {}
    for artifact in artifacts:
        manifest = _artifact_manifest(project_root, artifact.get("path", ""))
        if manifest:
            break
    classification = str(manifest.get("tailoring_basis") or row.get("role_kind") or "role_family")
    language = str(manifest.get("output_language") or "")
    return {
        "opportunity_id": row["id"],
        "company": row["company"],
        "title": row["title"],
        "location": row.get("location", ""),
        "status": row["status"],
        "version": row["updated_at"],
        "classification": classification,
        "language": language,
        "archetype": str(manifest.get("archetype_display") or manifest.get("archetype") or ""),
        "artifacts": artifacts,
        "manifest_found": bool(manifest),
    }


def _fetch_artifacts(db_path, opportunity_id: str) -> list[dict]:
    with closing(connect(db_path)) as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT id, opportunity_id, path, label, artifact_type FROM cv_artifacts "
                "WHERE opportunity_id=? ORDER BY artifact_type, id",
                (opportunity_id,),
            )
        ]


def list_cvs(db_path, project_root: Path | None = None, filters: dict | None = None) -> list[dict]:
    root = Path(project_root or PROJECT_ROOT).resolve()
    filters = {k: str(v) for k, v in (filters or {}).items() if k in ALLOWED_FILTERS and v}
    with closing(connect(db_path)) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM opportunities ORDER BY priority_score DESC, fit_score DESC, id"
            )
        ]
    results = []
    for row in rows:
        artifacts = _fetch_artifacts(db_path, row["id"])
        summary = _row_summary(root, row, artifacts)
        if "company" in filters and filters["company"].casefold() not in summary["company"].casefold():
            continue
        if "status" in filters and summary["status"] != filters["status"]:
            continue
        if "classification" in filters and summary["classification"] != filters["classification"]:
            continue
        if "language" in filters and summary["language"] != filters["language"]:
            continue
        if "has_cv" in filters:
            wants = filters["has_cv"].casefold() in {"true", "1", "yes"}
            if bool(artifacts) != wants:
                continue
        if "q" in filters:
            haystack = " ".join(
                [summary["company"], summary["title"], summary["location"]]
            ).casefold()
            if filters["q"].casefold() not in haystack:
                continue
        results.append(summary)
    return results


def cv_detail(db_path, opportunity_id: str, project_root: Path | None = None) -> dict:
    root = Path(project_root or PROJECT_ROOT).resolve()
    with closing(connect(db_path)) as connection:
        row = connection.execute(
            "SELECT * FROM opportunities WHERE id=?", (str(opportunity_id),)
        ).fetchone()
    if row is None:
        raise NotFoundError("opportunity not found")
    row = dict(row)
    artifacts = _fetch_artifacts(db_path, row["id"])
    detail = _row_summary(root, row, artifacts)
    manifest: dict[str, Any] = {}
    for artifact in artifacts:
        manifest = _artifact_manifest(root, artifact.get("path", ""))
        if manifest:
            break
    detail["manifest"] = manifest
    detail["requirement_evidence_report"] = manifest.get("requirement_evidence") or {}
    detail["description"] = row.get("description", "")
    detail["url"] = row.get("url", "")
    return detail


def _load_builder(project_root: Path):
    """Import the local tailoring pipeline (build_one + canonical profile).

    Returns (builder, profile, module). Patched in tests to avoid rendering.
    """
    scripts_dir = project_root / "reference_cv_2027" / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    module = importlib.import_module("tailor_cv_agent")
    import yaml

    profile = yaml.safe_load(module.PROFILE_PATH.read_text(encoding="utf-8"))
    return module.build_one, profile, module


def generate_cv(db_path, payload: dict, project_root: Path | None = None) -> dict:
    root = Path(project_root or PROJECT_ROOT).resolve()
    if not isinstance(payload, dict):
        raise ValidationError("JSON body must be an object")
    unknown = set(payload) - {"opportunity_id", "job_description", "language", "version"}
    if unknown:
        raise ValidationError("unknown generation fields: " + ", ".join(sorted(unknown)))
    opportunity_id = str(payload.get("opportunity_id") or "").strip()
    if not opportunity_id:
        raise ValidationError("opportunity_id is required")
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        raise ValidationError("version is required for every CV generation")
    language = payload.get("language")
    if language is not None:
        language = str(language).strip().casefold()
        if language not in SUPPORTED_LANGUAGES:
            raise ValidationError("language must be one of: en, fr")
    job_description = payload.get("job_description")
    if job_description is not None and not isinstance(job_description, str):
        raise ValidationError("job_description must be a string")

    with closing(connect(db_path)) as connection:
        row = connection.execute(
            "SELECT * FROM opportunities WHERE id=?", (opportunity_id,)
        ).fetchone()
    if row is None:
        raise NotFoundError("opportunity not found")
    row = dict(row)
    if version != row["updated_at"]:
        raise ConflictError("opportunity changed; reload before retrying")

    try:
        source = json.loads(row.get("source_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        source = {}
    job = {
        "title": row["title"],
        "company": row["company"],
        "location": row.get("location", ""),
        "link": row.get("url", "") or source.get("link", ""),
        "summary": row.get("description", "") or source.get("summary", ""),
        "requirements": row.get("requirements", "") or source.get("requirements", ""),
        "stable_id": row["id"],
    }
    if language:
        job["output_language"] = language

    builder, profile, _module = _load_builder(root)
    manifest = builder(job, profile, job_description or None)
    if not isinstance(manifest, dict):
        raise ValidationError("generation did not produce a manifest")
    files = manifest.get("files") or {}
    pdf_relative = str(files.get("pdf") or "")
    artifact_file = safe_artifact_path(root, pdf_relative)
    if root not in artifact_file.parents:
        raise ValidationError("generated artifact escaped the project root")
    label = "Tailored · " + str(
        manifest.get("archetype_display") or manifest.get("archetype") or "CV"
    )
    artifact = pipeline_v2.register_cv_artifact(db_path, opportunity_id, pdf_relative, label)
    return {
        "artifact": artifact,
        "manifest": manifest,
        "classification": str(manifest.get("tailoring_basis") or "role_family"),
        "language": str(manifest.get("output_language") or ""),
        "requirement_evidence_report": manifest.get("requirement_evidence") or {},
    }


def parse_filters(query_string: str) -> dict:
    from urllib.parse import parse_qs

    parsed = parse_qs(query_string or "", keep_blank_values=False)
    return {key: values[0] for key, values in parsed.items() if key in ALLOWED_FILTERS}
