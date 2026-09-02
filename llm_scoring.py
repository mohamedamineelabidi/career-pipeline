"""LLM rubric scoring (third signal next to rule score and semantic score).

Idea borrowed from brightdata's linkedin-job-hunting-assistant: score each job with
an LLM against a FIXED rubric. Safety rules:

* Input is only JD text + evidence text (career_master / evidence register /
  tailoring knowledge), each truncated to ~6000 chars. No other personal data.
* Every skill/term the model returns is validated: it must literally appear in the
  JD text or the evidence text, otherwise it is dropped. Nothing is invented.
* Degrades gracefully: when llm_client.llm_available() is False or the call fails
  we raise LLMUnavailable (HTTP 503) and never fake a score.
* Never logs the API key.
"""

from __future__ import annotations

import json
import re
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import keyword_highlight
import llm_client
from pipeline_v2 import ConflictError, NotFoundError, PathLike, ValidationError, connect

ROOT = Path(__file__).resolve().parent
MAX_TEXT_CHARS = 6000
MIN_DESCRIPTION_CHARS = 300
SLEEP_BETWEEN_CALLS = 1.2
RUBRIC_VERSION = 1

RUBRIC = (
    "You are a strict technical recruiter. Score how well the CANDIDATE EVIDENCE fits the JOB "
    "DESCRIPTION. Use ONLY the two texts below; never assume skills that are not written in the "
    "evidence. Return a JSON object with exactly these keys:\n"
    '  "fit": integer 0-100 (0 = no fit, 100 = perfect fit; treat seniority > 3 years as a strong penalty '
    "because the candidate is a 2027 graduate / junior),\n"
    '  "reasons": array of at most 3 short strings,\n'
    '  "missing_skills": array of skills required by the job that are NOT in the evidence (copy the exact words from the job description),\n'
    '  "matching_skills": array of skills required by the job that ARE in the evidence (copy the exact words),\n'
    '  "seniority_ok": boolean (true if a junior / new graduate profile is acceptable for this job),\n'
    '  "red_flags": array of short strings (e.g. required years of experience, on-site country restrictions, unrelated domain).\n'
)


class LLMUnavailable(RuntimeError):
    """Raised when the LLM cannot be used; handlers map this to HTTP 503."""


class RateLimited(LLMUnavailable):
    """HTTP 429 from the provider: stop batch scoring."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _truncate(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def evidence_text() -> str:
    """Evidence-only candidate text (career_master + knowledge), via keyword_highlight."""
    profile = keyword_highlight._cached_profile(
        keyword_highlight.CAREER_MASTER_PATH,
        keyword_highlight.EVIDENCE_REGISTER_PATH,
        keyword_highlight.KNOWLEDGE_PATH,
        keyword_highlight.TAXONOMY_PATH,
    )
    identity = profile.get("identity") or {}
    targets = profile.get("targets") or {}
    header = [
        str(targets.get("primary_identity") or ""),
        str(targets.get("headline") or ""),
        "Graduation: " + str(targets.get("graduation") or ""),
        "Location: " + str(identity.get("location") or ""),
        "Languages: " + ", ".join(
            f"{lang.get('name')} {lang.get('level') or ''}".strip()
            for lang in (profile.get("languages") or []) if isinstance(lang, dict)
        ),
    ]
    return "\n".join(line for line in header if line.strip()) + "\n" + str(profile.get("text") or "")


def _term_in(text_lower: str, term: str) -> bool:
    term = str(term or "").strip()
    if len(term) < 2:
        return False
    return re.search(rf"(?<![a-z0-9+#]){re.escape(term.casefold())}(?![a-z0-9+#])", text_lower) is not None


def validate_payload(raw: dict[str, Any], jd_text: str, evidence: str) -> dict[str, Any]:
    """Coerce the LLM output into the fixed rubric shape and drop unverifiable terms."""
    if not isinstance(raw, dict):
        raise ValidationError("LLM payload must be an object")
    jd_lower, ev_lower = jd_text.casefold(), evidence.casefold()
    try:
        fit = int(round(float(raw.get("fit", 0))))
    except (TypeError, ValueError):
        fit = 0
    fit = max(0, min(100, fit))

    def strings(key: str, limit: int) -> list[str]:
        value = raw.get(key)
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in out:
                out.append(text[:240])
            if len(out) >= limit:
                break
        return out

    dropped: list[str] = []
    missing: list[str] = []
    for term in strings("missing_skills", 15):
        if _term_in(jd_lower, term) and not _term_in(ev_lower, term):
            missing.append(term)
        else:
            dropped.append(term)
    matching: list[str] = []
    for term in strings("matching_skills", 15):
        if _term_in(jd_lower, term) and _term_in(ev_lower, term):
            matching.append(term)
        else:
            dropped.append(term)
    return {
        "fit": fit,
        "reasons": strings("reasons", 3),
        "missing_skills": missing,
        "matching_skills": matching,
        "seniority_ok": bool(raw.get("seniority_ok", False)),
        "red_flags": strings("red_flags", 6),
        "dropped_unverified_terms": dropped,
        "rubric_version": RUBRIC_VERSION,
    }


def _messages(jd_text: str, evidence: str) -> list[dict[str, str]]:
    return [{
        "role": "user",
        "content": (
            RUBRIC
            + "\n=== JOB DESCRIPTION ===\n" + _truncate(jd_text)
            + "\n\n=== CANDIDATE EVIDENCE ===\n" + _truncate(evidence)
        ),
    }]


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(row.get("payload_json") or "{}")
    payload.update({
        "opportunity_id": row["opportunity_id"],
        "model": row["model"],
        "fit": int(row["fit"]),
        "created_at": row["created_at"],
        "status": "computed",
    })
    return payload


def score_opportunity(db_path: PathLike, opportunity_id: str) -> dict[str, Any]:
    """Call the LLM once for one opportunity, validate, persist, return the stored score."""
    if not llm_client.llm_available():
        raise LLMUnavailable("LLM not configured (GROQ_API_KEY missing or LLM_DISABLED=1)")
    with closing(connect(db_path)) as connection:
        opportunity = keyword_highlight.load_opportunity(connection, opportunity_id)
    jd_text = keyword_highlight.vacancy_text(opportunity)
    if not str(opportunity.get("description") or "").strip():
        raise NotFoundError("opportunity has no description")
    evidence = evidence_text()
    try:
        raw = llm_client.chat_json(_messages(jd_text, evidence), max_tokens=700)
    except llm_client.LLMError as error:
        if "429" in str(error):
            raise RateLimited(str(error)) from error
        raise LLMUnavailable(str(error)) from error
    payload = validate_payload(raw, _truncate(jd_text), _truncate(evidence))
    now = _now()
    model = llm_client.model_name()
    with closing(connect(db_path)) as connection:
        connection.execute(
            """INSERT INTO llm_scores(opportunity_id, model, fit, payload_json, created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(opportunity_id) DO UPDATE SET model=excluded.model, fit=excluded.fit,
                 payload_json=excluded.payload_json, created_at=excluded.created_at""",
            (str(opportunity_id), model, payload["fit"], json.dumps(payload, ensure_ascii=False), now),
        )
        connection.commit()
    return get_score(db_path, opportunity_id)


def get_score(db_path: PathLike, opportunity_id: str) -> dict[str, Any]:
    with closing(connect(db_path)) as connection:
        if not connection.execute("SELECT 1 FROM opportunities WHERE id=?", (str(opportunity_id),)).fetchone():
            raise NotFoundError("opportunity not found")
        row = connection.execute(
            "SELECT * FROM llm_scores WHERE opportunity_id=?", (str(opportunity_id),)
        ).fetchone()
    if row is None:
        raise NotFoundError("no llm score for opportunity")
    return _serialize(dict(row))


def score_all(db_path: PathLike, limit: int = 40, only_missing: bool = True,
              min_description_chars: int = MIN_DESCRIPTION_CHARS,
              sleep_seconds: float = SLEEP_BETWEEN_CALLS, backoff_seconds: float = 15.0) -> dict[str, Any]:
    """Score up to ``limit`` opportunities; sleeps between calls; stops on HTTP 429."""
    limit = max(1, min(int(limit or 40), 500))
    if not llm_client.llm_available():
        raise LLMUnavailable("LLM not configured")
    closed = ("archived", "closed", "rejected", "withdrawn")
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            f"""SELECT o.id FROM opportunities o
                LEFT JOIN llm_scores ls ON ls.opportunity_id = o.id
                WHERE LENGTH(COALESCE(o.description,'')) >= ?
                  AND o.status NOT IN ({",".join("?" * len(closed))})
                  {"AND ls.opportunity_id IS NULL" if only_missing else ""}
                ORDER BY o.priority_score DESC, o.id LIMIT ?""",
            (int(min_description_chars), *closed, limit),
        ).fetchall()
    scored, failed, fits = [], [], []
    stopped_reason = None
    for index, row in enumerate(rows):
        if index:
            time.sleep(sleep_seconds)
        try:
            result = None
            for attempt in range(4):
                try:
                    result = score_opportunity(db_path, row["id"])
                    break
                except RateLimited:
                    if attempt == 3:
                        raise
                    time.sleep(backoff_seconds * (attempt + 1))  # free tier: tokens-per-minute; back off then retry
            scored.append(row["id"])
            fits.append(result["fit"])
        except RateLimited as error:
            stopped_reason = f"rate limited (429): {str(error)[:120]}"
            break
        except (LLMUnavailable, NotFoundError, ValidationError) as error:
            failed.append({"id": row["id"], "error": str(error)[:200]})
    return {
        "candidates": len(rows),
        "scored": len(scored),
        "failed": failed,
        "stopped_reason": stopped_reason,
        "fit_distribution": {
            "min": min(fits) if fits else None,
            "max": max(fits) if fits else None,
            "mean": round(sum(fits) / len(fits), 1) if fits else None,
            "buckets": {
                "0-39": sum(f < 40 for f in fits),
                "40-59": sum(40 <= f < 60 for f in fits),
                "60-79": sum(60 <= f < 80 for f in fits),
                "80-100": sum(f >= 80 for f in fits),
            },
        },
        "model": llm_client.model_name(),
    }


# --------------------------------------------------------------------------- #
# HTTP endpoint helpers (pipeline_v2.make_handler dispatches here)
# --------------------------------------------------------------------------- #
class ServiceUnavailable(ValidationError):
    """Mapped to HTTP 503 by pipeline_v2 (see _error)."""


def score_endpoint(db_path: PathLike, opportunity_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    unknown = set(payload) - {"version"}
    if unknown:
        raise ValidationError("only version is accepted")
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        raise ValidationError("version is required")
    with closing(connect(db_path)) as connection:
        row = connection.execute("SELECT updated_at FROM opportunities WHERE id=?", (str(opportunity_id),)).fetchone()
    if row is None:
        raise NotFoundError("opportunity not found")
    if row["updated_at"] != version:
        raise ConflictError("opportunity changed; reload before retrying")
    if not llm_client.llm_available():
        raise ServiceUnavailable("llm unavailable")
    try:
        return score_opportunity(db_path, opportunity_id)
    except LLMUnavailable as error:
        raise ServiceUnavailable(f"llm unavailable: {error}") from error


def recompute_endpoint(db_path: PathLike, payload: dict[str, Any]) -> dict[str, Any]:
    unknown = set(payload) - {"limit", "only_missing"}
    if unknown:
        raise ValidationError("only limit and only_missing are accepted")
    limit = payload.get("limit", 20)
    if not isinstance(limit, int) or limit < 1 or limit > 200:
        raise ValidationError("limit must be an integer 1-200")
    if not llm_client.llm_available():
        raise ServiceUnavailable("llm unavailable")
    try:
        return score_all(db_path, limit=limit, only_missing=bool(payload.get("only_missing", True)))
    except LLMUnavailable as error:
        raise ServiceUnavailable(f"llm unavailable: {error}") from error


if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="LLM rubric scoring")
    parser.add_argument("--db", default=str(ROOT / "career_pipeline_v2.sqlite3"))
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--all", action="store_true", help="rescore even already-scored rows")
    args = parser.parse_args()
    print(json.dumps(score_all(args.db, limit=args.limit, only_missing=not args.all), indent=2, ensure_ascii=False))
