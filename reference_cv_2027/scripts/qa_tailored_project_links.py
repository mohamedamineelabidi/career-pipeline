from __future__ import annotations

import json
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
CV_ROOT = ROOT.parent
DIGEST = CV_ROOT / "jobs_digest.json"
EXPECTED_LINKS = {
    "CasaMotion": "https://github.com/your-github-handle/CasaMotion",
    "Radian · Job Intelligent": "https://github.com/Ridadata/job-intelligent",
    "AetherSignal": "https://github.com/your-github-handle/ethereum-whale-tracker-predictor",
}
BLOCKED = {"AUC 0.913", "PR-AUC 0.867", "35% matching", "PDF reporting", "Solo Creator"}


def uris(reader: PdfReader) -> set[str]:
    values: set[str] = set()
    for page in reader.pages:
        for ref in page.get("/Annots", []):
            annot = ref.get_object()
            action = annot.get("/A")
            if action and action.get("/URI"):
                values.add(str(action["/URI"]))
    return values


def resolve_cv(value: str) -> Path:
    normalized = value.replace("\\", "/")
    if normalized.startswith("reference_cv_2027/"):
        return CV_ROOT / normalized
    path = Path(value)
    return path if path.is_absolute() else CV_ROOT / path


def main() -> int:
    payload = json.loads(DIGEST.read_text(encoding="utf-8"))
    jobs = payload.get("jobs", payload.get("opportunities", []))
    failures: list[dict[str, object]] = []
    linked_counts = {name: 0 for name in EXPECTED_LINKS}
    layouts = {1: 0, 2: 0}
    seen: set[Path] = set()
    pending_without_cv = 0
    for index, job in enumerate(jobs, start=1):
        raw = job.get("tailored_cv")
        if not raw:
            if job.get("tailoring_manifest"):
                failures.append({"index": index, "error": "manifest exists without tailored_cv"})
            else:
                pending_without_cv += 1
            continue
        path = resolve_cv(raw).resolve()
        if path in seen:
            failures.append({"index": index, "error": "duplicate PDF path", "path": str(path)})
            continue
        seen.add(path)
        if not path.exists():
            failures.append({"index": index, "error": "PDF not found", "path": str(path)})
            continue
        reader = PdfReader(path)
        page_count = len(reader.pages)
        layouts[page_count] = layouts.get(page_count, 0) + 1
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        pdf_uris = uris(reader)
        issues: list[str] = []
        if page_count not in {1, 2}:
            issues.append(f"unexpected page count {page_count}")
        if len(text.strip()) < 1000:
            issues.append(f"short ATS text {len(text.strip())}")
        for term in BLOCKED:
            if term in text:
                issues.append(f"blocked claim: {term}")
        for name, url in EXPECTED_LINKS.items():
            if name in text:
                linked_counts[name] += 1
                if url not in pdf_uris:
                    issues.append(f"missing URI for {name}")
        if issues:
            failures.append({"index": index, "path": str(path), "issues": issues})
    result = {
        "ok": not failures and len(jobs) > 0 and len(seen) == len(jobs) - pending_without_cv,
        "jobs": len(jobs),
        "pending_without_cv": pending_without_cv,
        "unique_pdfs": len(seen),
        "layouts": layouts,
        "project_linked_pdf_counts": linked_counts,
        "failures": failures,
    }
    output = ROOT / "out" / "tailored_project_link_qa.json"
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
