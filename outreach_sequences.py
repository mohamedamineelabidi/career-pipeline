"""Outreach sequencer: local, draft-only, 3-step cadence per contact and opportunity.

Design (inspired by linki/inb-style sequencers, but with no automation):
- Day 0 connection note (LinkedIn, <= 300 chars) or email intro.
- Day 5 follow-up (<= 600 chars).
- Day 12 value-add follow-up (<= 600 chars).

Bodies are assembled only from evidence facts (career_master) that overlap the
job description keywords. An optional LLM may REPHRASE the deterministic draft;
its output is validated (every capitalised word or tech term must already exist
in the supplied facts, JD, contact or company names), otherwise the template is
kept. Nothing here sends anything: no mail, no HTTP to social networks, no
browser. A step becomes user_sent only through an explicit call carrying
confirmed=True, which the UI issues when the user ticks "I sent this myself".
"""

from __future__ import annotations

import json
import re
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import cover_letter
import keyword_highlight as kh
import pipeline_v2
from pipeline_v2 import ConflictError, NotFoundError, PathLike, ValidationError, connect, stable_id

CHANNELS = frozenset({"linkedin", "email"})
LANGUAGES = frozenset({"fr", "en"})
STEP_STATES = frozenset({"draft", "user_sent", "replied", "skipped"})
STEP_TRANSITIONS = {
    "draft": {"user_sent", "skipped"},
    "user_sent": {"replied", "skipped"},
    "replied": set(),
    "skipped": set(),
}
SEQUENCE_STATUSES = frozenset({"draft", "user_sent", "replied", "closed"})
CADENCE = ((0, "connection_note"), (5, "follow_up"), (12, "value_add"))
CONNECTION_MAX = 300
FOLLOW_UP_MAX = 600
MAX_FACTS = 2
SEND_POLICY = "Local draft only. This system never sends or connects; mark a step as sent only after you sent it yourself."

LLMFn = Callable[[list[dict[str, str]], int], dict[str, Any]]


def ensure_schema(connection) -> None:
    connection.executescript(pipeline_v2.OUTREACH_SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(text: str) -> str:
    return cover_letter._clean(text or "")


def _short(text: str, limit: int) -> str:
    text = _clean(text)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(",;:. ") + "."


def _first_name(name: str) -> str:
    parts = _clean(name).split()
    if not parts:
        return ""
    first = parts[0]
    return first if first.isupper() and len(first) > 3 else first.capitalize() if first.isupper() else first


def detect_contact_language(contact: dict[str, Any], opportunity: dict[str, Any]) -> str:
    source = contact.get("source") or {}
    explicit = str(source.get("language") or "").strip().casefold()[:2]
    if explicit in LANGUAGES:
        return explicit
    jd_text = " ".join(str(opportunity.get(k) or "") for k in ("description", "requirements"))
    if jd_text.strip():
        return cover_letter.detect_language(jd_text)
    hint = " ".join(str(source.get(k) or "") for k in ("contact", "role", "location", "note"))
    hint += " " + str(opportunity.get("location") or "") + " " + str(contact.get("role") or "")
    lowered = hint.casefold()
    if re.search(r"\b(france|paris|lyon|maroc|casablanca|rabat|qu[eé]bec|montr[eé]al|belgique|suisse)\b", lowered):
        return "fr"
    return cover_letter.detect_language(hint) if hint.strip() else "en"


# ---------------------------------------------------------------- templates


def _fact_sentence(fact: dict[str, Any], language: str) -> str:
    where = fact.get("company") or fact.get("title") or ""
    statement = cover_letter._first_sentence(str(fact.get("statement") or ""))
    if fact.get("kind") == "experience" and where:
        prefix = "Chez " if language == "fr" else "At "
        return _clean(prefix + where + ", " + cover_letter._lower_first(statement))
    return statement


def compose_step(
    n: int,
    template_id: str,
    channel: str,
    language: str,
    contact: dict[str, Any],
    opportunity: dict[str, Any],
    profile: dict[str, Any],
    facts: list[dict[str, Any]],
    matched_skills: list[str],
) -> str:
    """Deterministic body for one step, evidence facts only."""
    identity = profile.get("identity") or {}
    name = _clean(str(identity.get("name") or ""))
    role = _clean(str((profile.get("targets") or {}).get("primary_identity") or "engineer"))
    first = _first_name(str(contact.get("name") or ""))
    company = _clean(str(opportunity.get("company") or contact.get("company") or ""))
    title = _clean(str(opportunity.get("title") or ""))
    skills = ", ".join(matched_skills[:3])
    fact1 = _fact_sentence(facts[0], language) if facts else ""
    fact2 = _fact_sentence(facts[1], language) if len(facts) > 1 else fact1
    greet_fr = f"Bonjour {first}," if first else "Bonjour,"
    greet_en = f"Hello {first}," if first else "Hello,"

    if template_id == "connection_note":
        if language == "fr":
            core = f"{greet_fr} je suis {role} et je m'intéresse au poste {title} chez {company}."
            if skills:
                core += f" Je travaille avec {skills}."
            core += " Je serais heureux d'échanger avec vous. " + name
        else:
            core = f"{greet_en} I am a {role} interested in the {title} role at {company}."
            if skills:
                core += f" I work with {skills}."
            core += " I would be glad to connect. " + name
        if channel == "email":
            core = core.replace("Je serais heureux d'échanger avec vous.", "Je serais heureux d'échanger sur ce poste par email ou en visio.") \
                       .replace("I would be glad to connect.", "I would be glad to discuss this role by email or a short call.")
            if fact1:
                core = core.replace(" " + name, f" {fact1} " + name) if name else core + " " + fact1
        return _short(core, CONNECTION_MAX if channel == "linkedin" else FOLLOW_UP_MAX)

    if template_id == "follow_up":
        if language == "fr":
            lines = [greet_fr, f"Je reviens vers vous au sujet du poste {title} chez {company}."]
            if fact1:
                lines.append(fact1)
            lines.append("Si le profil correspond, je peux vous envoyer mon CV ou proposer un court appel. Merci pour votre temps.")
        else:
            lines = [greet_en, f"I am following up about the {title} role at {company}."]
            if fact1:
                lines.append(fact1)
            lines.append("If the profile fits, I can send my CV or set up a short call. Thank you for your time.")
        lines.append(name)
        return _short(" ".join(lines), FOLLOW_UP_MAX)

    # value_add
    if language == "fr":
        lines = [greet_fr, f"Un dernier message au sujet du poste {title} chez {company}."]
        if fact2:
            lines.append(fact2)
        if skills:
            lines.append(f"Je peux partager un exemple concret de mon travail avec {skills} si cela vous est utile.")
        lines.append("Je reste disponible si une opportunité se présente. Bonne continuation.")
    else:
        lines = [greet_en, f"One last note about the {title} role at {company}."]
        if fact2:
            lines.append(fact2)
        if skills:
            lines.append(f"I can share a concrete example of my work with {skills} if that is useful.")
        lines.append("I remain available if an opportunity comes up. All the best.")
    lines.append(name)
    return _short(" ".join(lines), FOLLOW_UP_MAX)


# ---------------------------------------------------------------- LLM rephrase


_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ][A-Za-z0-9À-ÿ+#.\-]*")
# Pronouns and greeting words that are capitalised without being proper nouns.
_NEUTRAL_WORDS = frozenset("""
i you we he she it they my your our me us hello hi dear bonjour merci je vous nous mon ma mes votre
un une le la les des du de si et ou at chez in on the a an one if thank thanks all best regards cordialement
""".split())


def _allowed_terms(facts: list[dict[str, Any]], jd_text: str, extra: list[str]) -> set[str]:
    """Allowed vocabulary for LLM output: evidence facts, names and titles.

    The JD text is deliberately NOT included: a JD skill the candidate lacks must
    never be claimed in an outreach message. Only the jd-derived company/title are
    passed through ``extra``.
    """
    corpus = " ".join(
        list(extra)
        + [str(f.get("statement") or "") + " " + " ".join(f.get("technologies") or []) + " " + str(f.get("company") or "") for f in facts]
    )
    return {t.casefold().strip(".") for t in _TOKEN_RE.findall(corpus)} | set(_NEUTRAL_WORDS)


def validate_rephrase(candidate: str, allowed: set[str], limit: int) -> bool:
    """Every capitalised or tech-like token of the candidate must exist in the allowed corpus."""
    if not candidate or not candidate.strip():
        return False
    if len(candidate) > limit or "\u2014" in candidate or cover_letter.EMOJI_RE.search(candidate):
        return False
    for token in _TOKEN_RE.findall(candidate):
        key = token.casefold().strip(".")
        if not key:
            continue
        looks_proper = token[0].isupper() or any(ch.isdigit() for ch in token) or any(ch in "+#" for ch in token)
        if looks_proper and key not in allowed:
            return False
    return True


def default_llm(messages: list[dict[str, str]], max_tokens: int) -> dict[str, Any]:
    import llm_client

    if not llm_client.llm_available():
        raise llm_client.LLMError("llm unavailable")
    return llm_client.chat_json(messages, max_tokens=max_tokens)


def rephrase(body: str, language: str, limit: int, allowed: set[str], llm: LLMFn | None) -> tuple[str, bool]:
    """Return (body, rephrased_by_llm). Falls back to the template on any doubt."""
    if llm is None:
        return body, False
    prompt = (
        "Rewrite the following outreach draft in plain B2 "
        + ("French" if language == "fr" else "English")
        + f". Keep every fact, name and technology exactly as given, add nothing new, no em dashes, no emoji, at most {limit} characters. "
        'Answer as JSON {"body": "..."}.\n\nDRAFT:\n' + body
    )
    try:
        result = llm([{"role": "user", "content": prompt}], 400)
    except Exception:  # LLM is optional; any failure keeps the template
        return body, False
    candidate = _clean(str((result or {}).get("body") or ""))
    if validate_rephrase(candidate, allowed, limit):
        return candidate, True
    return body, False


# ---------------------------------------------------------------- data access


def _load_contact(connection, contact_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
    if row is None:
        raise NotFoundError("contact not found")
    record = dict(row)
    try:
        record["source"] = json.loads(record.get("source_json") or "{}")
    except json.JSONDecodeError:
        record["source"] = {}
    return record


def _serialize_step(row) -> dict[str, Any]:
    record = dict(row)
    record["evidence_ids"] = json.loads(record.pop("evidence_ids_json") or "[]")
    record["version"] = record["updated_at"]
    record["is_draft"] = record["state"] == "draft"
    return record


def _serialize_sequence(connection, row) -> dict[str, Any]:
    record = dict(row)
    record["version"] = record["updated_at"]
    steps = connection.execute(
        "SELECT * FROM outreach_steps WHERE sequence_id=? ORDER BY n", (record["id"],)
    ).fetchall()
    record["steps"] = [_serialize_step(s) for s in steps]
    pending = [s for s in record["steps"] if s["state"] == "draft"]
    record["next_due_date"] = pending[0]["due_date"] if pending else None
    record["send_policy"] = SEND_POLICY
    return record


def _build_bodies(
    connection, contact: dict[str, Any], opportunity: dict[str, Any], channel: str, language: str,
    sources: dict[str, Any], llm: LLMFn | None, only_template: str | None = None,
) -> list[dict[str, Any]]:
    profile = kh.evidence_profile(
        sources.get("career_master_path", kh.CAREER_MASTER_PATH),
        sources.get("evidence_register_path", kh.EVIDENCE_REGISTER_PATH),
        sources.get("knowledge_path", kh.KNOWLEDGE_PATH),
        sources.get("taxonomy_path", kh.TAXONOMY_PATH),
    )
    jd_text = kh.vacancy_text(opportunity)
    facts, matched = cover_letter.select_facts(profile, jd_text, sources.get("taxonomy_path", kh.TAXONOMY_PATH))
    facts = facts[:MAX_FACTS]
    identity = profile.get("identity") or {}
    allowed = _allowed_terms(facts, jd_text, [
        str(contact.get("name") or ""), str(contact.get("company") or ""),
        str(opportunity.get("company") or ""), str(opportunity.get("title") or ""),
        str(identity.get("name") or ""), str((profile.get("targets") or {}).get("primary_identity") or ""),
        " ".join(matched), "Bonjour Hello Chez At",
    ])
    out = []
    for n, (offset, template_id) in enumerate(CADENCE):
        if only_template and template_id != only_template:
            continue
        limit = CONNECTION_MAX if (template_id == "connection_note" and channel == "linkedin") else FOLLOW_UP_MAX
        body = compose_step(n, template_id, channel, language, contact, opportunity, profile, facts, matched)
        body, by_llm = rephrase(body, language, limit, allowed, llm)
        problems = lint_body(body, limit)
        if problems:
            raise ValidationError("draft failed lint: " + ", ".join(problems))
        out.append({
            "n": n, "offset": offset, "template_id": template_id, "body": body,
            "evidence_ids": [f["citation"] for f in facts], "rephrased_by_llm": by_llm,
            "matched_skills": matched,
        })
    return out


def lint_body(body: str, limit: int) -> list[str]:
    problems = []
    if len(body) > limit:
        problems.append("too long")
    if "\u2014" in body:
        problems.append("em dash")
    if cover_letter.EMOJI_RE.search(body):
        problems.append("emoji")
    return problems


# ---------------------------------------------------------------- D1 create


def create_sequence(
    db_path: PathLike,
    contact_id: str,
    opportunity_id: str,
    channel: str,
    start_date: str | date | None = None,
    language: str | None = None,
    version: str | None = None,
    llm: LLMFn | None = None,
    **sources: Any,
) -> dict[str, Any]:
    if channel not in CHANNELS:
        raise ValidationError("channel must be linkedin or email")
    if language is not None and language not in LANGUAGES:
        raise ValidationError("language must be fr or en")
    if isinstance(start_date, str):
        try:
            start = date.fromisoformat(start_date)
        except ValueError as error:
            raise ValidationError("start_date must be YYYY-MM-DD") from error
    elif isinstance(start_date, date):
        start = start_date
    else:
        start = datetime.now(timezone.utc).date()
    with closing(connect(db_path)) as connection:
        ensure_schema(connection)
        contact = _load_contact(connection, contact_id)
        opportunity = kh.load_opportunity(connection, opportunity_id)
        if version is not None and version != contact["updated_at"]:
            raise ConflictError("contact changed; reload before retrying")
        if language is None:
            language = detect_contact_language(contact, opportunity)
        bodies = _build_bodies(connection, contact, opportunity, channel, language, sources, llm)
        now = _now()
        sequence_id = stable_id("oseq", contact_id, opportunity_id, channel, now)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO outreach_sequences(id, contact_id, opportunity_id, channel, language, status,
                   current_step, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'draft', 0, ?, ?)""",
            (sequence_id, contact_id, opportunity_id, channel, language, now, now),
        )
        for item in bodies:
            connection.execute(
                """INSERT INTO outreach_steps(id, sequence_id, n, due_date, template_id, body, state,
                       evidence_ids_json, rephrased_by_llm, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?)""",
                (
                    stable_id("ostep", sequence_id, item["n"]), sequence_id, item["n"],
                    (start + timedelta(days=item["offset"])).isoformat(), item["template_id"], item["body"],
                    json.dumps(item["evidence_ids"], ensure_ascii=False), int(item["rephrased_by_llm"]), now, now,
                ),
            )
        connection.commit()
        record = _serialize_sequence(connection, connection.execute(
            "SELECT * FROM outreach_sequences WHERE id=?", (sequence_id,)
        ).fetchone())
    record["matched_skills"] = bodies[0]["matched_skills"] if bodies else []
    return record


def create_from_payload(db_path: PathLike, payload: dict[str, Any], llm: LLMFn | None = None, **sources: Any) -> dict[str, Any]:
    unknown = set(payload) - {"contact_id", "opportunity_id", "channel", "version", "start_date", "language"}
    if unknown:
        raise ValidationError("only contact_id, opportunity_id, channel, version, start_date, language are accepted")
    for key in ("contact_id", "opportunity_id", "channel", "version"):
        if not isinstance(payload.get(key), str) or not payload.get(key):
            raise ValidationError(f"{key} is required")
    return create_sequence(
        db_path, payload["contact_id"], payload["opportunity_id"], payload["channel"],
        start_date=payload.get("start_date"), language=payload.get("language"),
        version=payload["version"], llm=llm, **sources,
    )


def list_sequences(db_path: PathLike, contact_id: str | None = None, opportunity_id: str | None = None) -> dict[str, Any]:
    clauses, params = [], []
    if contact_id:
        clauses.append("s.contact_id=?")
        params.append(contact_id)
    if opportunity_id:
        clauses.append("s.opportunity_id=?")
        params.append(opportunity_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with closing(connect(db_path)) as connection:
        ensure_schema(connection)
        rows = connection.execute(
            f"""SELECT s.*, c.name AS contact_name, c.company AS contact_company, c.role AS contact_role,
                       o.title AS opportunity_title, o.company AS opportunity_company
                FROM outreach_sequences s
                LEFT JOIN contacts c ON c.id=s.contact_id
                LEFT JOIN opportunities o ON o.id=s.opportunity_id
                {where} ORDER BY s.created_at DESC, s.id""",
            params,
        ).fetchall()
        sequences = [_serialize_sequence(connection, r) for r in rows]
    return {"sequences": sequences, "count": len(sequences), "send_policy": SEND_POLICY}


# ---------------------------------------------------------------- D2 due / mark / regenerate


def due(db_path: PathLike, on_date: str | date | None = None) -> dict[str, Any]:
    if isinstance(on_date, str):
        try:
            target = date.fromisoformat(on_date)
        except ValueError as error:
            raise ValidationError("date must be YYYY-MM-DD") from error
    elif isinstance(on_date, date):
        target = on_date
    else:
        target = datetime.now(timezone.utc).date()
    with closing(connect(db_path)) as connection:
        ensure_schema(connection)
        rows = connection.execute(
            """SELECT st.*, s.contact_id, s.opportunity_id, s.channel, s.language,
                      c.name AS contact_name, c.company AS contact_company,
                      o.title AS opportunity_title, o.company AS opportunity_company
               FROM outreach_steps st
               JOIN outreach_sequences s ON s.id=st.sequence_id
               LEFT JOIN contacts c ON c.id=s.contact_id
               LEFT JOIN opportunities o ON o.id=s.opportunity_id
               WHERE st.state='draft' AND st.due_date <= ?
               ORDER BY st.due_date, s.id, st.n""",
            (target.isoformat(),),
        ).fetchall()
    steps = []
    for row in rows:
        step = _serialize_step(row)
        step["overdue"] = step["due_date"] < target.isoformat()
        steps.append(step)
    return {"date": target.isoformat(), "steps": steps, "count": len(steps), "send_policy": SEND_POLICY}


def _refresh_sequence_status(connection, sequence_id: str, now: str) -> None:
    states = [r[0] for r in connection.execute(
        "SELECT state FROM outreach_steps WHERE sequence_id=? ORDER BY n", (sequence_id,)
    ).fetchall()]
    if "replied" in states:
        status = "replied"
    elif all(s == "skipped" for s in states) and states:
        status = "closed"
    elif "user_sent" in states:
        status = "user_sent"
    else:
        status = "draft"
    current = next((i for i, s in enumerate(states) if s == "draft"), len(states))
    connection.execute(
        "UPDATE outreach_sequences SET status=?, current_step=?, updated_at=? WHERE id=?",
        (status, current, now, sequence_id),
    )


def mark_step(db_path: PathLike, step_id: str, state: str, version: str, confirmed: bool = False) -> dict[str, Any]:
    if state not in STEP_STATES or state == "draft":
        raise ValidationError("state must be user_sent, replied or skipped")
    if not isinstance(version, str) or not version:
        raise ValidationError("version is required for every step mutation")
    if state == "user_sent" and confirmed is not True:
        raise ValidationError("user_sent requires confirmed=true (I sent this myself)")
    now = _now()
    with closing(connect(db_path)) as connection:
        ensure_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute("SELECT * FROM outreach_steps WHERE id=?", (step_id,)).fetchone()
        if current is None:
            connection.rollback()
            raise NotFoundError("step not found")
        if version != current["updated_at"]:
            connection.rollback()
            raise ConflictError("step changed; reload before retrying")
        if state not in STEP_TRANSITIONS[current["state"]]:
            connection.rollback()
            raise ValidationError(f"invalid step transition: {current['state']} -> {state}")
        connection.execute(
            "UPDATE outreach_steps SET state=?, updated_at=?, marked_at=? WHERE id=?",
            (state, now, now, step_id),
        )
        sequence = connection.execute(
            "SELECT contact_id, opportunity_id, channel FROM outreach_sequences WHERE id=?", (current["sequence_id"],)
        ).fetchone()
        connection.execute(
            """INSERT INTO outreach_events(id, opportunity_id, contact_id, draft_id, event_type, occurred_at, notes, created_by)
               VALUES (?, ?, ?, NULL, ?, ?, ?, 'user')""",
            (
                stable_id("oevt", step_id, state, now), sequence["opportunity_id"], sequence["contact_id"],
                f"outreach_step_{state}", now,
                f"step {step_id} ({current['template_id']}, {sequence['channel']}) "
                + ("confirmed by user: I sent this myself" if state == "user_sent" else f"marked {state} by user"),
            ),
        )
        _refresh_sequence_status(connection, current["sequence_id"], now)
        connection.commit()
        return _serialize_step(connection.execute("SELECT * FROM outreach_steps WHERE id=?", (step_id,)).fetchone())


def mark_from_payload(db_path: PathLike, step_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    unknown = set(payload) - {"state", "version", "confirmed"}
    if unknown:
        raise ValidationError("only state, version and confirmed are accepted")
    return mark_step(db_path, step_id, str(payload.get("state") or ""), payload.get("version"), payload.get("confirmed") is True)


def regenerate_step(db_path: PathLike, step_id: str, version: str, llm: LLMFn | None = None, **sources: Any) -> dict[str, Any]:
    if not isinstance(version, str) or not version:
        raise ValidationError("version is required")
    with closing(connect(db_path)) as connection:
        ensure_schema(connection)
        current = connection.execute("SELECT * FROM outreach_steps WHERE id=?", (step_id,)).fetchone()
        if current is None:
            raise NotFoundError("step not found")
        if version != current["updated_at"]:
            raise ConflictError("step changed; reload before retrying")
        if current["state"] != "draft":
            raise ValidationError("only draft steps can be regenerated")
        sequence = connection.execute("SELECT * FROM outreach_sequences WHERE id=?", (current["sequence_id"],)).fetchone()
        contact = _load_contact(connection, sequence["contact_id"])
        opportunity = kh.load_opportunity(connection, sequence["opportunity_id"])
        items = _build_bodies(connection, contact, opportunity, sequence["channel"], sequence["language"],
                              sources, llm, only_template=current["template_id"])
        item = items[0]
        now = _now()
        connection.execute(
            "UPDATE outreach_steps SET body=?, evidence_ids_json=?, rephrased_by_llm=?, updated_at=? WHERE id=?",
            (item["body"], json.dumps(item["evidence_ids"], ensure_ascii=False), int(item["rephrased_by_llm"]), now, step_id),
        )
        connection.execute("UPDATE outreach_sequences SET updated_at=? WHERE id=?", (now, sequence["id"]))
        connection.commit()
        return _serialize_step(connection.execute("SELECT * FROM outreach_steps WHERE id=?", (step_id,)).fetchone())


def regenerate_from_payload(db_path: PathLike, step_id: str, payload: dict[str, Any], llm: LLMFn | None = None, **sources: Any) -> dict[str, Any]:
    unknown = set(payload) - {"version"}
    if unknown:
        raise ValidationError("only version is accepted")
    return regenerate_step(db_path, step_id, payload.get("version"), llm=llm, **sources)


# ---------------------------------------------------------------- D3 applied sync + picker


def mark_applied(db_path: PathLike, opportunity_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    unknown = set(payload) - {"version", "confirmed", "applied_at", "channel"}
    if unknown:
        raise ValidationError("only version, confirmed, applied_at and channel are accepted")
    if payload.get("confirmed") is not True:
        raise ValidationError("confirmed=true is required (I applied myself)")
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        raise ValidationError("version is required")
    applied_at = payload.get("applied_at")
    if applied_at is not None:
        if not isinstance(applied_at, str):
            raise ValidationError("applied_at must be an ISO date or datetime string")
        try:
            datetime.fromisoformat(applied_at)
        except ValueError as error:
            raise ValidationError("applied_at must be an ISO date or datetime string") from error
    channel = payload.get("channel")
    if channel is not None and (not isinstance(channel, str) or len(channel) > 40):
        raise ValidationError("channel must be a short string")
    record = pipeline_v2.update_opportunity(
        db_path, opportunity_id, {"status": "user_applied", "version": version, "confirmed_by_user": True}
    )
    if applied_at or channel:
        with closing(connect(db_path)) as connection:
            application_id = stable_id("app", opportunity_id)
            notes = "Confirmed manually by user" + (f" via {_clean(channel)}" if channel else "")
            connection.execute(
                "UPDATE applications SET applied_at=COALESCE(?, applied_at), notes=?, updated_at=? WHERE id=?",
                (applied_at, notes, _now(), application_id),
            )
            connection.commit()
    with closing(connect(db_path)) as connection:
        application = connection.execute(
            "SELECT * FROM applications WHERE id=?", (stable_id("app", opportunity_id),)
        ).fetchone()
    record["version"] = record["updated_at"]
    record["application"] = dict(application) if application else None
    return record


def search_lite(db_path: PathLike, query: str, limit: int = 20) -> dict[str, Any]:
    q = _clean(query or "")
    limit = max(1, min(int(limit or 20), 20))
    with closing(connect(db_path)) as connection:
        if not q:
            rows = []
        else:
            like = f"%{q}%"
            rows = connection.execute(
                """SELECT id, company, title, status, location, updated_at FROM opportunities
                   WHERE (company LIKE ? OR title LIKE ?) AND status != 'closed'
                   ORDER BY CASE WHEN status='user_applied' THEN 1 ELSE 0 END, priority_score DESC, company, title
                   LIMIT ?""",
                (like, like, limit),
            ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["version"] = item["updated_at"]
        items.append(item)
    return {"q": q, "items": items, "count": len(items)}


def parse_query(query: str) -> dict[str, str]:
    from urllib.parse import parse_qs

    params = parse_qs(query or "")
    return {k: v[0] for k, v in params.items() if v}
