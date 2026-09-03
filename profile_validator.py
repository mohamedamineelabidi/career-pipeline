"""Validate a career profile and explain exactly how to fix it.

The profile schema is the single hardest thing about adopting this tool. Getting a
value wrong does not raise: evidence loading simply yields zero facts and a later
step fails with an opaque HTTP 400. That happened with `evidence_status: confirmed`,
which looks obviously correct and is not in the accepted set.

Every message here names the location, what was found, and what to use instead.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from semantic_match import ACCEPTED_EVIDENCE_STATUSES

REQUIRED_EXPERIENCE_FIELDS = ("title", "company")

# Each section names its fields differently. Checked against the real profile
# schema rather than assumed: a validator that invents requirements is worse than
# none, because it trains people to ignore it.
SECTION_LABEL_FIELDS = {
    "experiences": (("title", "role", "position"), ("company", "employer", "organisation")),
    "projects": (("name", "title"), ("role", "company")),
    "education": (("degree", "title"), ("institution", "school", "university")),
    "certifications": (("name", "title"), ("issuer", "organisation")),
}

FACT_SECTIONS = tuple(SECTION_LABEL_FIELDS)


def _first_present(entry: dict, names: tuple[str, ...]) -> str:
    for name in names:
        value = str(entry.get(name) or "").strip()
        if value:
            return value
    return ""


def _validate_entry(section: str, index: int, entry: object, errors: list[str]) -> bool:
    """Return True when the entry can contribute a usable fact."""
    where = f"{section}[{index}]"
    if not isinstance(entry, dict):
        errors.append(f"{where}: expected a mapping of fields, found {type(entry).__name__}.")
        return False

    usable = True
    label_fields, owner_fields = SECTION_LABEL_FIELDS.get(
        section, (("title",), ("company",)))
    if not _first_present(entry, label_fields):
        errors.append(f"{where}.{label_fields[0]} is missing: add a {label_fields[0]}.")
        usable = False
    if not _first_present(entry, owner_fields):
        errors.append(f"{where}.{owner_fields[0]} is missing: add a {owner_fields[0]}.")
        usable = False

    status = str(entry.get("evidence_status") or entry.get("status") or "").strip()
    accepted = ", ".join(sorted(ACCEPTED_EVIDENCE_STATUSES))
    if not status:
        errors.append(
            f"{where}.evidence_status is missing: use one of {accepted}."
        )
        usable = False
    elif status not in ACCEPTED_EVIDENCE_STATUSES:
        errors.append(
            f"{where}.evidence_status is {status!r}, which is not accepted, so this "
            f"entry contributes no facts: use one of {accepted}."
        )
        usable = False
    return usable


def validate(profile: object) -> dict:
    """Check a loaded profile and return {ok, errors, warnings, usable_facts}."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(profile, dict):
        return {
            "ok": False,
            "errors": [f"profile: expected a mapping at the top level, "
                       f"found {type(profile).__name__}."],
            "warnings": [],
            "usable_facts": 0,
        }

    usable_facts = 0
    present_sections = 0
    for section in FACT_SECTIONS:
        entries = profile.get(section)
        if entries is None:
            continue
        if not isinstance(entries, list):
            errors.append(f"{section}: expected a list of entries, "
                          f"found {type(entries).__name__}.")
            continue
        present_sections += 1
        for index, entry in enumerate(entries):
            if _validate_entry(section, index, entry, errors):
                usable_facts += 1

    if present_sections == 0:
        errors.append(
            "profile contains no facts: add at least one entry under "
            f"{' or '.join(FACT_SECTIONS)}."
        )
    elif usable_facts == 0:
        errors.append(
            "profile yields no facts, so scoring and cover letters will fail: fix the "
            "entry errors above, or add one entry with an accepted evidence_status."
        )

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "usable_facts": usable_facts,
    }


def validate_file(path: str | Path) -> dict:
    """Validate a YAML or JSON profile on disk."""
    path = Path(path)
    if not path.exists():
        return {"ok": False, "usable_facts": 0, "warnings": [],
                "errors": [f"{path}: file not found: create it or pass --profile."]}
    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix.lower() in (".yaml", ".yml"):
            import yaml
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
    except Exception as error:  # noqa: BLE001 - surfaced to the user verbatim
        return {"ok": False, "usable_facts": 0, "warnings": [],
                "errors": [f"{path}: could not be parsed: {error}"]}
    report = validate(data)
    report["path"] = str(path)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="path to career_master.yaml")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    report = validate_file(args.profile)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    elif report["ok"]:
        print(f"Profile is valid: {report['usable_facts']} usable facts.")
    else:
        print(f"Profile has {len(report['errors'])} problem(s):\n")
        for error in report["errors"]:
            print(f"  - {error}")
        print("\nNothing was changed. Fix the entries above and run this again.")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
