"""Application prep: pre-fill an ATS application form, NEVER submit.

Offlyn-Apply idea, local only. Opens the opportunity's apply page in Playwright,
detects the ATS, fills known identity fields from career_master.yaml (evidence
only, nothing invented), attaches the tailored PDF, takes a screenshot, records a
row in application_preps and stops. Mohamed clicks the final button himself.

Safety invariants (enforced by tests/test_application_prep.py):
* linkedin.com is refused -> status 'manual_only'.
* no click calls in this module except on ALLOWED_CLICK_SELECTORS (none submit-like).
* no CAPTCHA handling, no programmatic form submission, no Enter keypress.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import yaml

from pipeline_v2 import ConflictError, NotFoundError, ValidationError, connect

PROJECT_ROOT = Path(__file__).resolve().parent
PROFILES_PATH = PROJECT_ROOT / "ats_form_profiles.json"
def _profile_path(base, filename):
    """Return the personal profile file, or the shipped example when absent."""
    import pathlib as _pathlib
    real = _pathlib.Path(base) / filename
    if real.exists():
        return real
    example = _pathlib.Path(base) / filename.replace(".yaml", ".example.yaml")
    return example if example.exists() else real


CAREER_MASTER_PATH = _profile_path(PROJECT_ROOT / "reference_cv_2027" / "data", "career_master.yaml")
SCREENSHOT_DIR = PROJECT_ROOT / "application_prep"

ATS_NAMES = ("greenhouse", "workday", "lever", "ashby", "smartrecruiters", "workable", "linkedin", "unknown")
STATUSES = ("prepared_awaiting_user", "manual_only", "failed")
FORBIDDEN_CLICK = re.compile(r"submit|apply|send|envoyer|postuler|candidater|soumettre", re.I)
# The only selectors this module is ever allowed to click (cookie/consent dismissal only).
ALLOWED_CLICK_SELECTORS: tuple[str, ...] = (
    "button[aria-label='Close']",
    "button[aria-label='Fermer']",
    "#onetrust-reject-all-handler",
)
PREFILL_FIELDS = (
    "first_name", "last_name", "full_name", "email", "phone", "linkedin",
    "github", "portfolio", "location", "resume_path",
)

APPLICATION_PREPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS application_preps (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    ats TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    filled_fields_json TEXT NOT NULL DEFAULT '{}',
    screenshot_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('prepared_awaiting_user', 'manual_only', 'failed')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS application_preps_opportunity ON application_preps(opportunity_id, created_at);
"""


def ensure_schema(db_path) -> None:
    with closing(connect(db_path)) as connection:
        connection.executescript(APPLICATION_PREPS_SCHEMA)
        connection.commit()


def load_profiles(path: Path | None = None) -> dict[str, Any]:
    return json.loads(Path(path or PROFILES_PATH).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- A1: detect
def detect_ats(url: str, profiles: dict | None = None) -> str:
    host = (urlsplit(str(url or "").strip()).hostname or "").casefold()
    if not host:
        return "unknown"
    for rule in (profiles or load_profiles()).get("detection", []):
        if re.search(rule["host_pattern"], host):
            return rule["ats"]
    return "unknown"


# --------------------------------------------------------------- A2: prefill
def _clean(value: object) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def build_prefill(career_master: dict, ats: str, resume_path: str | None = None) -> dict[str, Optional[str]]:
    """Map identity facts to form fields. Missing -> None. Nothing invented."""
    identity = (career_master or {}).get("identity") or {}
    name = _clean(identity.get("name"))
    first = last = None
    if name:
        parts = name.split()
        first, last = parts[0], (" ".join(parts[1:]) or None)
    linkedin = _clean(identity.get("linkedin_url"))
    if not linkedin and _clean(identity.get("linkedin_handle")):
        linkedin = f"https://www.linkedin.com/in/{identity['linkedin_handle'].strip()}"
    prefill = {
        "first_name": first,
        "last_name": last,
        "full_name": name,
        "email": _clean(identity.get("email")),
        "phone": _clean(identity.get("phone")),
        "linkedin": linkedin,
        "github": _clean(identity.get("github_url")),
        "portfolio": _clean(identity.get("portfolio_url")),
        "location": _clean(identity.get("location")),
        "resume_path": _clean(resume_path),
    }
    if ats == "linkedin":
        return {key: None for key in prefill}
    return prefill


def selectors_for(ats: str, profiles: dict | None = None) -> dict[str, list[str]]:
    profiles = profiles or load_profiles()
    generic = profiles.get("generic", {})
    specific = profiles.get("profiles", {}).get(ats, {})
    merged: dict[str, list[str]] = {}
    for field in PREFILL_FIELDS:
        seen: list[str] = []
        for selector in list(specific.get(field, [])) + list(generic.get(field, [])):
            if selector not in seen:
                seen.append(selector)
        merged[field] = seen
    return merged


def assert_safe_selector(selector: str) -> None:
    if FORBIDDEN_CLICK.search(selector) or re.search(r"type=.?submit|<button|button\[", selector, re.I):
        raise ValidationError(f"forbidden selector targets a submit-like control: {selector}")


# ------------------------------------------------------------------ DB rows
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(row: sqlite3.Row | None) -> dict:
    if row is None:
        return {}
    value = dict(row)
    try:
        value["filled_fields"] = json.loads(value.get("filled_fields_json") or "{}")
    except json.JSONDecodeError:
        value["filled_fields"] = {}
    return value


def record_prep(db_path, opportunity_id: str, ats: str, url: str, filled: dict, screenshot: str,
                status: str, note: str = "") -> dict:
    if status not in STATUSES:
        raise ValidationError("invalid prep status")
    ensure_schema(db_path)
    prep_id = "prep_" + uuid.uuid4().hex[:24]
    with closing(connect(db_path)) as connection:
        connection.execute(
            "INSERT INTO application_preps(id, opportunity_id, ats, url, filled_fields_json, screenshot_path, status, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (prep_id, opportunity_id, ats, url, json.dumps(filled, ensure_ascii=False), screenshot, status, note, _now()),
        )
        connection.commit()
        return _row(connection.execute("SELECT * FROM application_preps WHERE id=?", (prep_id,)).fetchone())


def latest_prep(db_path, opportunity_id: str) -> dict:
    ensure_schema(db_path)
    with closing(connect(db_path)) as connection:
        row = connection.execute(
            "SELECT * FROM application_preps WHERE opportunity_id=? ORDER BY created_at DESC LIMIT 1",
            (str(opportunity_id),),
        ).fetchone()
    if row is None:
        raise NotFoundError("no application prep for this opportunity")
    return _row(row)


def list_preps(db_path) -> list[dict]:
    ensure_schema(db_path)
    with closing(connect(db_path)) as connection:
        return [_row(r) for r in connection.execute(
            "SELECT p.*, o.company, o.title FROM application_preps p JOIN opportunities o ON o.id=p.opportunity_id"
            " ORDER BY p.created_at DESC")]


def _tailored_pdf(db_path, opportunity_id: str, project_root: Path) -> Optional[Path]:
    with closing(connect(db_path)) as connection:
        row = connection.execute(
            "SELECT path FROM cv_artifacts WHERE opportunity_id=? AND artifact_type='tailored'", (opportunity_id,)
        ).fetchone()
    if not row:
        return None
    candidate = (project_root / str(row["path"])).resolve()
    if project_root.resolve() not in candidate.parents or not candidate.is_file():
        return None
    return candidate


# ------------------------------------------------------------- A3: prepare
def _default_browser_factory(headless: bool):
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless)
    return pw, browser


def fill_page(page, prefill: dict, ats: str, profiles: dict | None = None) -> dict[str, dict]:
    """Fill mapped text/file inputs. Returns {field: {selector, value}}. Never clicks."""
    filled: dict[str, dict] = {}
    for field, selectors in selectors_for(ats, profiles).items():
        value = prefill.get(field)
        if not value:
            continue
        for selector in selectors:
            assert_safe_selector(selector)
            locator = page.locator(selector).first
            try:
                if locator.count() == 0:
                    continue
                if field == "resume_path":
                    locator.set_input_files(str(value))
                else:
                    input_type = (locator.get_attribute("type") or "text").casefold()
                    if input_type in {"submit", "button", "checkbox", "radio", "hidden", "file"}:
                        continue
                    locator.fill(str(value))
                filled[field] = {"selector": selector, "value": str(value)}
                break
            except Exception:  # selector present but not fillable; try the next one
                continue
    return filled


def prepare_application(db_path, opportunity_id: str, headless: bool = False, browser_factory: Callable | None = None,
                        project_root: Path | None = None, career_master_path: Path | None = None,
                        screenshot_dir: Path | None = None, url_override: str | None = None) -> dict:
    root = Path(project_root or PROJECT_ROOT).resolve()
    opportunity_id = str(opportunity_id or "").strip()
    with closing(connect(db_path)) as connection:
        row = connection.execute("SELECT id, url, company, title FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
    if row is None:
        raise NotFoundError("opportunity not found")
    url = url_override or str(row["url"] or "")
    profiles = load_profiles()
    ats = detect_ats(url, profiles)

    if ats == "linkedin" or (urlsplit(url).hostname or "").casefold().endswith("linkedin.com"):
        prep = record_prep(db_path, opportunity_id, "linkedin", url, {}, "", "manual_only",
                           "LinkedIn Easy Apply is out of scope; apply manually.")
        return {"prep": prep, "browser": None}

    pdf = _tailored_pdf(db_path, opportunity_id, root)
    if pdf is None:
        raise NotFoundError("no tailored CV PDF registered for this opportunity")
    master = yaml.safe_load(Path(career_master_path or CAREER_MASTER_PATH).read_text(encoding="utf-8")) or {}
    prefill = build_prefill(master, ats, resume_path=str(pdf))

    shots = Path(screenshot_dir or SCREENSHOT_DIR)
    shots.mkdir(parents=True, exist_ok=True)
    screenshot = shots / f"{opportunity_id}.png"

    factory = browser_factory or _default_browser_factory
    pw, browser = factory(headless)
    page = browser.new_page()
    filled: dict = {}
    status, note = "prepared_awaiting_user", ""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        filled = fill_page(page, prefill, ats, profiles)
        page.screenshot(path=str(screenshot), full_page=True)
        if not filled:
            note = "page opened but no known fields found; fill manually"
        else:
            note = "Prepared. Review the browser window and submit yourself. Nothing was submitted."
    except Exception as error:  # navigation or page failure
        status, note = "failed", f"{type(error).__name__}: {error}"[:500]
        try:
            page.screenshot(path=str(screenshot))
        except Exception:
            pass
    handle = None
    if headless:
        browser.close()
        if pw is not None:
            pw.stop()
    else:
        handle = {"kept_open": True, "note": "browser left open for manual review and submission"}
    prep = record_prep(db_path, opportunity_id, ats, url, filled,
                       str(screenshot.relative_to(root)) if root in screenshot.parents else str(screenshot),
                       status, note)
    return {"prep": prep, "browser": handle, "prefill_available": {k: bool(v) for k, v in prefill.items()}}


# ---------------------------------------------------------------- A4: HTTP
def prepare_endpoint(db_path, payload: dict, project_root: Path | None = None, **kwargs) -> dict:
    if not isinstance(payload, dict):
        raise ValidationError("JSON body must be an object")
    unknown = set(payload) - {"opportunity_id", "version", "headless"}
    if unknown:
        raise ValidationError("unknown fields: " + ", ".join(sorted(unknown)))
    opportunity_id = str(payload.get("opportunity_id") or "").strip()
    if not opportunity_id:
        raise ValidationError("opportunity_id is required")
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        raise ValidationError("version is required")
    with closing(connect(db_path)) as connection:
        row = connection.execute("SELECT updated_at FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
    if row is None:
        raise NotFoundError("opportunity not found")
    if row["updated_at"] != version:
        raise ConflictError("opportunity changed; reload before retrying")
    result = prepare_application(db_path, opportunity_id, headless=bool(payload.get("headless", False)),
                                 project_root=project_root, **kwargs)
    return {"prep": result["prep"], "browser": result.get("browser"), "nothing_submitted": True}
