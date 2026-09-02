"""Analytics / insights over the Career Pipeline v2 SQLite database.

Pure, read-only functions (idea ported from santifer/career-ops ``stats.mjs``,
``analyze-patterns.mjs``, ``detect-reposts.mjs`` and jobsync analytics).
No schema changes, stdlib only.
"""

from __future__ import annotations

import math
import re
import statistics
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pipeline_v2

PathLike = str | Path

STAGES = ["discovered", "verified_active", "eligible", "shortlisted", "user_applied"]
STAGE_INDEX = {name: index for index, name in enumerate(STAGES)}
ALL_STATUSES = STAGES + ["closed"]
REPOST_MIN_COUNT = 2
REPOST_MIN_SPAN_DAYS = 21
DISAGREEMENT_THRESHOLD = 25
SUMMARY_TTL_SECONDS = 60

_LEVEL_WORDS = {
    "junior", "senior", "sr", "jr", "lead", "principal", "staff", "intern", "internship",
    "stagiaire", "stage", "alternance", "alternant", "apprenti", "apprentice", "h/f", "f/h",
    "m/f", "f/m", "m/w/d", "w/m/d", "i", "ii", "iii", "iv", "1", "2", "3", "confirmé",
    "confirme", "débutant", "debutant", "mid", "midlevel", "entry", "level", "graduate",
}
_PAREN_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso_week(moment: datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def _week_start(moment: datetime) -> datetime:
    day = moment.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _table_columns(connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _latest_llm_fit(connection) -> dict[str, float]:
    """opportunity_id -> most recent llm fit (any model)."""
    rows = connection.execute(
        "SELECT opportunity_id, fit, created_at FROM llm_scores WHERE fit IS NOT NULL "
        "ORDER BY created_at ASC"
    ).fetchall()
    latest: dict[str, float] = {}
    for row in rows:  # ascending -> last write wins
        latest[row["opportunity_id"]] = float(row["fit"])
    return latest


def normalize_title(title: object) -> str:
    """Lowercase, strip parentheses/brackets, punctuation and seniority/level words."""
    text = _PAREN_RE.sub(" ", str(title or "").casefold())
    text = _NON_WORD_RE.sub(" ", text)
    tokens = [token for token in text.split() if token not in _LEVEL_WORDS]
    return " ".join(tokens)


def _normalize_company(company: object) -> str:
    text = _NON_WORD_RE.sub(" ", str(company or "").casefold())
    return " ".join(text.split())


# --------------------------------------------------------------------------- #
# 1. funnel
# --------------------------------------------------------------------------- #
def funnel_stats(db_path: PathLike) -> dict[str, Any]:
    with closing(pipeline_v2.connect(db_path)) as connection:
        counts = {status: 0 for status in ALL_STATUSES}
        for row in connection.execute("SELECT status, COUNT(*) AS n FROM opportunities GROUP BY status"):
            counts[row["status"]] = counts.get(row["status"], 0) + int(row["n"])
        total = sum(counts.values())

        # Reached-stage counts: an opportunity at stage k has passed stages 0..k.
        reached = {stage: 0 for stage in STAGES}
        for status, n in counts.items():
            index = STAGE_INDEX.get(status)
            if index is None:
                continue
            for stage in STAGES[: index + 1]:
                reached[stage] += n

        conversions = []
        for previous, current in zip(STAGES, STAGES[1:]):
            conversions.append({
                "from": previous, "to": current,
                "from_count": reached[previous], "to_count": reached[current],
                "pct": _pct(reached[current], reached[previous]),
            })

        # Median days spent in each stage from lifecycle_events.
        durations: dict[str, list[float]] = defaultdict(list)
        events = connection.execute(
            "SELECT entity_id, from_status, to_status, occurred_at FROM lifecycle_events "
            "WHERE entity_type = 'opportunity' ORDER BY entity_id, occurred_at"
        ).fetchall()
        entered: dict[tuple[str, str], datetime] = {}
        for event in events:
            when = _parse_ts(event["occurred_at"])
            if when is None:
                continue
            key = (event["entity_id"], event["from_status"] or "")
            started = entered.pop(key, None)
            if started is not None:
                durations[event["from_status"]].append((when - started).total_seconds() / 86400)
            entered[(event["entity_id"], event["to_status"] or "")] = when
        source = "lifecycle_events"
        if not durations:
            source = "created_at_updated_at"
            for row in connection.execute("SELECT status, created_at, updated_at FROM opportunities"):
                start, end = _parse_ts(row["created_at"]), _parse_ts(row["updated_at"])
                if start and end and row["status"] in STAGE_INDEX:
                    durations[row["status"]].append(max(0.0, (end - start).total_seconds() / 86400))
        median_days = {
            stage: (_round(statistics.median(durations[stage]), 2) if durations.get(stage) else None)
            for stage in STAGES
        }
        return {
            "total": total,
            "counts": counts,
            "reached": reached,
            "conversions": conversions,
            "median_days_in_stage": median_days,
            "median_days_source": source,
        }


# --------------------------------------------------------------------------- #
# 2. weekly activity
# --------------------------------------------------------------------------- #
def weekly_activity(db_path: PathLike, weeks: int = 8, now: datetime | None = None) -> dict[str, Any]:
    weeks = max(1, int(weeks))
    now = now or datetime.now(timezone.utc)
    first_week = _week_start(now) - timedelta(weeks=weeks - 1)
    buckets: dict[str, dict[str, int]] = {}
    for offset in range(weeks):
        label = _iso_week(first_week + timedelta(weeks=offset))
        buckets[label] = {
            "week": label, "week_start": (first_week + timedelta(weeks=offset)).date().isoformat(),
            "discovered": 0, "verified": 0, "cvs_generated": 0, "applied": 0, "outreach_steps_marked_sent": 0,
        }

    def bump(when: object, key: str) -> None:
        moment = _parse_ts(when)
        if moment is None or moment < first_week:
            return
        label = _iso_week(moment)
        if label in buckets:
            buckets[label][key] += 1

    with closing(pipeline_v2.connect(db_path)) as connection:
        for row in connection.execute("SELECT created_at, updated_at, status, source_verification_status FROM opportunities"):
            bump(row["created_at"], "discovered")
        events = connection.execute(
            "SELECT to_status, occurred_at FROM lifecycle_events WHERE entity_type = 'opportunity'"
        ).fetchall()
        applied_from_events = verified_from_events = False
        for event in events:
            if event["to_status"] == "user_applied":
                bump(event["occurred_at"], "applied")
                applied_from_events = True
            if event["to_status"] == "verified_active":
                bump(event["occurred_at"], "verified")
                verified_from_events = True
        if not applied_from_events:
            for row in connection.execute("SELECT applied_at, created_at FROM applications"):
                bump(row["applied_at"] or row["created_at"], "applied")
        if not verified_from_events:
            for row in connection.execute(
                "SELECT updated_at FROM opportunities WHERE status IN ('verified_active','eligible','shortlisted','user_applied')"
            ):
                bump(row["updated_at"], "verified")
        columns = _table_columns(connection, "cv_artifacts")
        if "created_at" in columns:
            for row in connection.execute("SELECT created_at FROM cv_artifacts"):
                bump(row["created_at"], "cvs_generated")
        else:
            for row in connection.execute(
                "SELECT o.updated_at AS at FROM cv_artifacts a JOIN opportunities o ON o.id = a.opportunity_id"
            ):
                bump(row["at"], "cvs_generated")
        for row in connection.execute(
            "SELECT marked_at, updated_at FROM outreach_steps WHERE state IN ('sent_by_user','sent')"
        ):
            bump(row["marked_at"] or row["updated_at"], "outreach_steps_marked_sent")
    series = list(buckets.values())
    totals = {key: sum(week[key] for week in series)
              for key in ("discovered", "verified", "cvs_generated", "applied", "outreach_steps_marked_sent")}
    return {"weeks": weeks, "series": series, "totals": totals}


# --------------------------------------------------------------------------- #
# 3. source performance
# --------------------------------------------------------------------------- #
def source_performance(db_path: PathLike) -> list[dict[str, Any]]:
    with closing(pipeline_v2.connect(db_path)) as connection:
        llm = _latest_llm_fit(connection)
        per_source: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
            "SELECT id, source, status, fit_score, description FROM opportunities"
        ):
            source = (row["source"] or "unknown").strip() or "unknown"
            bucket = per_source.setdefault(source, {"count": 0, "with_description": 0, "fits": [], "llm": [], "user_applied": 0})
            bucket["count"] += 1
            if (row["description"] or "").strip():
                bucket["with_description"] += 1
            if row["fit_score"] is not None:
                bucket["fits"].append(float(row["fit_score"]))
            if row["id"] in llm:
                bucket["llm"].append(llm[row["id"]])
            if row["status"] == "user_applied":
                bucket["user_applied"] += 1
    result = []
    for source, bucket in per_source.items():
        result.append({
            "source": source,
            "count": bucket["count"],
            "pct_with_description": _pct(bucket["with_description"], bucket["count"]),
            "avg_fit_score": _round(statistics.fmean(bucket["fits"])) if bucket["fits"] else None,
            "avg_llm_fit": _round(statistics.fmean(bucket["llm"])) if bucket["llm"] else None,
            "llm_scored": len(bucket["llm"]),
            "user_applied": bucket["user_applied"],
        })
    result.sort(key=lambda item: (-item["count"], item["source"]))
    return result


# --------------------------------------------------------------------------- #
# 4. skill demand / gaps
# --------------------------------------------------------------------------- #
_EXTRACTOR_CACHE: dict[int, Any] = {}


def _fast_extractor(tax):
    """One-pass alias scanner equivalent to ``tax.extract`` (set semantics).

    ``SkillTaxonomy.extract`` runs one lookbehind regex per skill; over ~1 MB of
    JD text that is several seconds. Here we tokenise once with a single
    alternation and map aliases back to canonical names.
    """
    cached = _EXTRACTOR_CACHE.get(id(tax))
    if cached is not None:
        return cached
    alias_to_name: dict[str, str] = {}
    for skill in tax.skills:
        aliases = set(skill.get("aliases", []))
        if len(skill["name"]) >= 4:
            aliases.add(skill["name"])
        for alias in aliases:
            if alias:
                alias_to_name.setdefault(alias.casefold(), skill["name"])
    ordered = sorted(alias_to_name, key=len, reverse=True)
    boundary = set("abcdefghijklmnopqrstuvwxyz0123456789+#")

    def extract(text: str) -> set[str]:
        # str.find is C-speed; 660 aliases x one JD is far cheaper than 173 lookbehind regexes.
        low = text.casefold()
        found: set[str] = set()
        for alias in ordered:
            name = alias_to_name[alias]
            if name in found:
                continue
            start = low.find(alias)
            while start != -1:
                end = start + len(alias)
                if (start == 0 or low[start - 1] not in boundary) and (end >= len(low) or low[end] not in boundary):
                    found.add(name)
                    break
                start = low.find(alias, start + 1)
        return found

    _EXTRACTOR_CACHE.clear()
    _EXTRACTOR_CACHE[id(tax)] = extract
    return extract


def skill_demand(
    db_path: PathLike,
    top: int = 25,
    taxonomy_path: PathLike | None = None,
    profile: dict[str, Any] | None = None,
    **profile_paths: PathLike,
) -> dict[str, Any]:
    """Skill demand across job descriptions vs the evidence profile.

    ``profile`` may be passed directly (tests); otherwise ``keyword_highlight``'s
    evidence profile is loaded (optionally with ``career_master_path`` /
    ``evidence_register_path`` / ``knowledge_path`` keyword overrides).
    """
    import keyword_highlight
    import semantic_match

    tax = semantic_match.taxonomy(taxonomy_path) if taxonomy_path else semantic_match.taxonomy()
    if profile is None:
        try:
            if profile_paths:
                profile = keyword_highlight.evidence_profile(
                    profile_paths.get("career_master_path", keyword_highlight.CAREER_MASTER_PATH),
                    profile_paths.get("evidence_register_path", keyword_highlight.EVIDENCE_REGISTER_PATH),
                    profile_paths.get("knowledge_path", keyword_highlight.KNOWLEDGE_PATH),
                    taxonomy_path or keyword_highlight.TAXONOMY_PATH,
                )
            else:
                profile = keyword_highlight._cached_profile(
                    keyword_highlight.CAREER_MASTER_PATH, keyword_highlight.EVIDENCE_REGISTER_PATH,
                    keyword_highlight.KNOWLEDGE_PATH,
                )
        except Exception:  # missing evidence files -> nothing is "had"
            profile = {}
    have = {key.casefold() for key in (profile.get("skill_citations") or {})}

    counts: dict[str, int] = defaultdict(int)
    jobs_with_text = 0
    extractor = _fast_extractor(tax)
    with closing(pipeline_v2.connect(db_path)) as connection:
        for row in connection.execute("SELECT title, description, requirements FROM opportunities"):
            text = " ".join(str(part or "") for part in (row["title"], row["description"], row["requirements"]))
            if not text.strip():
                continue
            jobs_with_text += 1
            for name in extractor(text):
                counts[name] += 1
    skills = []
    for name, n in counts.items():
        you_have = name.casefold() in have
        skills.append({
            "skill": name, "jobs_requesting": n, "pct_of_jobs": _pct(n, jobs_with_text),
            "you_have": you_have, "gap_priority": n * (0 if you_have else 1),
        })
    skills.sort(key=lambda item: (-item["jobs_requesting"], item["skill"]))
    top_skills = skills[: max(0, int(top))]
    gaps = sorted((s for s in skills if s["gap_priority"] > 0), key=lambda s: (-s["gap_priority"], s["skill"]))
    return {"jobs_analyzed": jobs_with_text, "skills": top_skills, "top_gaps": gaps[: max(0, int(top))]}


# --------------------------------------------------------------------------- #
# 5. reposts / ghost jobs
# --------------------------------------------------------------------------- #
def detect_reposts(db_path: PathLike) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    with closing(pipeline_v2.connect(db_path)) as connection:
        for row in connection.execute(
            "SELECT id, title, company, url, status, created_at, publication_date FROM opportunities"
        ):
            key = (_normalize_company(row["company"]), normalize_title(row["title"]))
            if not key[0] or not key[1]:
                continue
            groups[key].append({
                "id": row["id"], "title": row["title"], "url": row["url"], "status": row["status"],
                "seen_at": row["publication_date"] or row["created_at"],
            })
    result = []
    for (company, norm_title), items in groups.items():
        distinct = {(item["id"], item["url"]) for item in items}
        if len(items) < 2 or len(distinct) < 2:
            continue
        stamps = [ts for ts in (_parse_ts(item["seen_at"]) for item in items) if ts]
        first_seen = min(stamps) if stamps else None
        last_seen = max(stamps) if stamps else None
        span = round((last_seen - first_seen).total_seconds() / 86400, 1) if first_seen and last_seen else 0.0
        flagged = len(items) >= REPOST_MIN_COUNT and span >= REPOST_MIN_SPAN_DAYS
        result.append({
            "company": company, "normalized_title": norm_title, "count": len(items),
            "first_seen": first_seen.isoformat() if first_seen else None,
            "last_seen": last_seen.isoformat() if last_seen else None,
            "span_days": span,
            "flag": "possible repost/ghost job" if flagged else None,
            "opportunities": sorted(items, key=lambda item: str(item["seen_at"] or "")),
        })
    result.sort(key=lambda group: (group["flag"] is None, -group["count"], -group["span_days"], group["company"]))
    return {"groups": result, "flagged": sum(1 for group in result if group["flag"])}


# --------------------------------------------------------------------------- #
# 6. heuristic fit vs LLM fit
# --------------------------------------------------------------------------- #
def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return round(cov / math.sqrt(var_x * var_y), 4)


def fit_vs_llm(db_path: PathLike, threshold: int = DISAGREEMENT_THRESHOLD) -> dict[str, Any]:
    pairs, disagreements = [], []
    with closing(pipeline_v2.connect(db_path)) as connection:
        llm = _latest_llm_fit(connection)
        if llm:
            placeholders = ",".join("?" for _ in llm)
            rows = connection.execute(
                f"SELECT id, title, company, status, fit_score FROM opportunities WHERE id IN ({placeholders})",
                list(llm),
            ).fetchall()
        else:
            rows = []
    for row in rows:
        if row["fit_score"] is None:
            continue
        fit, llm_fit = float(row["fit_score"]), llm[row["id"]]
        pair = {"opportunity_id": row["id"], "title": row["title"], "company": row["company"],
                "status": row["status"], "fit_score": fit, "llm_fit": llm_fit, "delta": round(llm_fit - fit, 1)}
        pairs.append(pair)
        if abs(fit - llm_fit) >= threshold:
            disagreements.append(pair)
    disagreements.sort(key=lambda pair: -abs(pair["delta"]))
    return {
        "n": len(pairs),
        "pearson_r": pearson([p["fit_score"] for p in pairs], [p["llm_fit"] for p in pairs]),
        "threshold": threshold,
        "pairs": pairs,
        "disagreements": disagreements,
    }


# --------------------------------------------------------------------------- #
# summary (60s in-process cache)
# --------------------------------------------------------------------------- #
_SUMMARY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def summary(db_path: PathLike, ttl: float = SUMMARY_TTL_SECONDS, **skill_kwargs: Any) -> dict[str, Any]:
    key = str(Path(db_path).resolve()) if str(db_path) != ":memory:" else ":memory:"
    now = time.monotonic()
    cached = _SUMMARY_CACHE.get(key)
    if cached and now - cached[0] < ttl and not skill_kwargs:
        return cached[1]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "funnel": funnel_stats(db_path),
        "weekly": weekly_activity(db_path),
        "sources": source_performance(db_path),
        "skills": skill_demand(db_path, **skill_kwargs),
        "reposts": detect_reposts(db_path),
        "fit_vs_llm": fit_vs_llm(db_path),
    }
    if not skill_kwargs:
        _SUMMARY_CACHE[key] = (now, payload)
    return payload


def clear_cache() -> None:
    _SUMMARY_CACHE.clear()


ENDPOINTS = {
    "funnel": funnel_stats,
    "weekly": weekly_activity,
    "sources": source_performance,
    "skills": skill_demand,
    "reposts": detect_reposts,
    "fit-vs-llm": fit_vs_llm,
    "summary": summary,
}
