from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

import yaml

PLACEHOLDERS = ("[x]", "tbd", "xxx", "month year")


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)


def _selected_bullets(profile: dict):
    for item in profile.get("experience", []):
        if item.get("selected_for_reference"):
            yield item.get("id", item.get("company", "experience")), item.get("bullets", [])
    for item in profile.get("projects", []):
        if item.get("selected_for_reference"):
            yield item.get("id", item.get("name", "project")), item.get("bullets", [])


def validate_profile(profile: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(profile, dict):
        return ["Profile root must be a mapping"]
    all_text = "\n".join(_walk_strings(profile)).lower()

    for token in PLACEHOLDERS:
        if token in all_text:
            errors.append(f"Placeholder token found: {token}")

    approved_metrics = {str(metric) for metric in profile.get("approved_metrics", [])}
    for item_id, bullets in _selected_bullets(profile):
        for index, bullet in enumerate(bullets):
            for metric in bullet.get("metrics", []):
                if str(metric) not in approved_metrics:
                    errors.append(
                        f"Unapproved metric in {item_id} bullet {index + 1}: {metric}"
                    )

    selected_text = " ".join(
        bullet.get("statement", "")
        for _, bullets in _selected_bullets(profile)
        for bullet in bullets
    ).lower()
    for phrase in profile.get("public_claim_blocklist", []):
        if str(phrase).lower() in selected_text:
            errors.append(f"Blocklisted public claim found: {phrase}")

    identity = profile.get("identity", {})
    for field in ("name", "email", "phone", "linkedin_url", "github_url"):
        if not identity.get(field):
            errors.append(f"Missing identity field: {field}")

    for field in ("linkedin_url", "github_url", "portfolio_url"):
        value = identity.get(field)
        if value and urlparse(str(value)).scheme not in {"http", "https"}:
            errors.append(f"Unsafe URL scheme in identity.{field}")

    seen_ids: set[str] = set()
    for section in ("experience", "projects"):
        for item in profile.get(section, []):
            item_id = item.get("id")
            if item_id in seen_ids:
                errors.append(f"Duplicate ID: {item_id}")
            elif item_id:
                seen_ids.add(item_id)
            if section == "projects" and item.get("github_url"):
                project_url = urlparse(str(item["github_url"]))
                if project_url.scheme != "https" or project_url.netloc.casefold() != "github.com":
                    errors.append(f"Unsafe GitHub URL in projects.{item_id}.github_url")
            for field in ("start_date", "end_date"):
                value = item.get(field)
                if value in (None, "present"):
                    continue
                text = str(value)
                if re.fullmatch(r"\d{4}", text):
                    continue
                match = re.fullmatch(r"(\d{4})-(\d{2})", text)
                if not match or not 1 <= int(match.group(2)) <= 12:
                    errors.append(f"Invalid date in {section}.{item_id}.{field}: {value}")

    if profile.get("approval", {}).get("reference_cv_status") not in {
        "candidate_pending_user_approval",
        "approved",
    }:
        errors.append("Invalid reference CV approval status")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate canonical CV profile")
    parser.add_argument("profile", type=Path)
    args = parser.parse_args()
    profile = yaml.safe_load(args.profile.read_text(encoding="utf-8"))
    errors = validate_profile(profile)
    print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
