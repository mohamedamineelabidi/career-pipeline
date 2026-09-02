from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CV_ROOT = ROOT.parent
JOBS_PATH = CV_ROOT / "jobs_digest.json"
SUPPORTED_VERSIONS = {2, 4}


def main() -> int:
    payload = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
    failures: list[str] = []
    counts: Counter[str] = Counter()
    pending_without_cv = 0

    for job in payload.get("jobs", []):
        label = f"{job.get('company')} · {job.get('title')}"
        expected_track = job.get("opportunity_track")
        manifest_rel = job.get("tailoring_manifest")
        if not manifest_rel:
            if job.get("tailored_cv"):
                failures.append(f"{label}: generated tailored_cv is missing tailoring_manifest")
            else:
                pending_without_cv += 1
            continue
        manifest_path = CV_ROOT / manifest_rel
        if not manifest_path.is_file():
            failures.append(f"{label}: manifest not found: {manifest_rel}")
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        track = manifest.get("opportunity_track")
        counts[track] += 1
        if manifest.get("tailoring_version") not in SUPPORTED_VERSIONS:
            failures.append(f"{label}: stale tailoring version {manifest.get('tailoring_version')}")
        if track != expected_track:
            failures.append(f"{label}: job track {expected_track!r} != manifest track {track!r}")
        expected_positioning = "experienced_professional" if track == "professional_role" else "role_relevant_candidate"
        if manifest.get("positioning") != expected_positioning:
            failures.append(f"{label}: wrong positioning {manifest.get('positioning')!r}")

        text_rel = manifest.get("files", {}).get("text")
        text_path = CV_ROOT / str(text_rel or "")
        if not text_path.is_file():
            failures.append(f"{label}: extracted ATS text missing")
            continue
        text = text_path.read_text(encoding="utf-8", errors="replace")
        normalized_text = re.sub(r"\s+", " ", text).strip()
        has_pfe = bool(re.search(r"\bPFE\b", text, flags=re.IGNORECASE))
        if track in {"professional_role", "internship"} and has_pfe:
            failures.append(f"{label}: {track} CV incorrectly contains PFE")
        if track == "pfe_internship" and not has_pfe:
            failures.append(f"{label}: explicit PFE CV is missing PFE availability")
        if track == "internship" and "Available for an internship from January 2027." not in normalized_text:
            failures.append(f"{label}: generic internship availability statement missing")

    result = {
        "ok": not failures,
        "jobs": len(payload.get("jobs", [])),
        "tracks": dict(counts),
        "evaluated_manifests": sum(counts.values()),
        "pending_without_cv": pending_without_cv,
        "professional_and_generic_internship_pfe_leaks": sum("incorrectly contains PFE" in item for item in failures),
        "failures": failures,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
