from __future__ import annotations

import json
from pathlib import Path

from pypdf import PdfReader
import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
EXPECTED_URLS = {
    "https://github.com/your-github-handle/CasaMotion",
    "https://github.com/Ridadata/job-intelligent",
    "https://github.com/your-github-handle/ethereum-whale-tracker-predictor",
}
EXPECTED_NAMES = {"CasaMotion", "Radian · Job Intelligent", "AetherSignal"}
BLOCKED = {"AUC 0.913", "PR-AUC 0.867", "35% matching", "PDF reporting", "Solo Creator"}
FILES = {
    "reference": ("Mohamed_Amine_El_Abidi_Reference_CV_One_Column.pdf", 1),
    "compact": ("Mohamed_Amine_El_Abidi_Data_AI_CV.pdf", 1),
    "morocco_photo": ("Mohamed_Amine_El_Abidi_Data_AI_CV_Morocco_Photo.pdf", 1),
    "international": ("Mohamed_Amine_El_Abidi_Data_AI_CV_International.pdf", 2),
}


def annotation_uris(reader: PdfReader) -> list[str]:
    uris: list[str] = []
    for page in reader.pages:
        for ref in page.get("/Annots", []):
            annot = ref.get_object()
            action = annot.get("/A")
            if action and action.get("/URI"):
                uris.append(str(action["/URI"]))
    return uris


def main() -> int:
    report: dict[str, object] = {"ok": True, "files": {}}
    preview_dir = OUT / "project_link_qa"
    preview_dir.mkdir(parents=True, exist_ok=True)
    for key, (filename, expected_pages) in FILES.items():
        path = OUT / filename
        reader = PdfReader(path)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        uris = annotation_uris(reader)
        project_urls = sorted(set(uris) & EXPECTED_URLS)
        missing_urls = sorted(EXPECTED_URLS - set(uris))
        missing_names = sorted(name for name in EXPECTED_NAMES if name not in text)
        blocked_found = sorted(term for term in BLOCKED if term in text)
        page_ok = len(reader.pages) == expected_pages
        file_ok = page_ok and not missing_urls and not missing_names and not blocked_found and len(text.strip()) >= 1000
        previews: list[str] = []
        doc = pdfium.PdfDocument(str(path))
        for index in range(len(doc)):
            preview = preview_dir / f"{key}_page_{index + 1}.png"
            doc[index].render(scale=150 / 72).to_pil().save(preview)
            previews.append(str(preview))
        report["files"][key] = {
            "path": str(path),
            "pages": len(reader.pages),
            "expected_pages": expected_pages,
            "text_chars": len(text),
            "annotation_count": len(uris),
            "project_urls": project_urls,
            "missing_project_urls": missing_urls,
            "missing_project_names": missing_names,
            "blocked_terms_found": blocked_found,
            "previews": previews,
            "ok": file_ok,
        }
        report["ok"] = bool(report["ok"] and file_ok)
    report_path = OUT / "project_link_qa.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
