"""Deterministic outreach drafts for promoted contacts.

No LLM call. Two fixed templates per language (internship vs job); the caller
passes one verified fact about the sender. Drafts are saved with status
'draft_not_opened' and are never sent or opened by this module.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

DRAFT_STATUS = "draft_not_opened"
ABOUT_ME_PATH = Path(__file__).with_name("about_me.json")


@lru_cache(maxsize=1)
def _about_me_cached() -> dict:
    return json.loads(ABOUT_ME_PATH.read_text(encoding="utf-8"))


def about_me() -> dict:
    """The fact sheet every draft is built from (a fresh copy each call)."""
    return json.loads(json.dumps(_about_me_cached()))

_TEMPLATES = {
    ("fr", "internship"): (
        "Bonjour {first}, je suis the candidate, étudiant ingénieur à l'ENSAH, et "
        "je cherche un stage PFE en IA ou data chez {company} pour 2027. Un point "
        "concret sur mon parcours : {fact}. Je ne veux pas prendre trop de votre "
        "temps. Serait-il possible de savoir si {company} accueille des stagiaires PFE "
        "sur ces sujets cette année ?"
    ),
    ("fr", "job"): (
        "Bonjour {first}, je suis the candidate, ingénieur en IA et data formé à "
        "l'ENSAH. Je m'intéresse au poste {role} chez {company}. Un point concret sur "
        "mon parcours : {fact}. Je reste bref pour respecter votre temps. Pourriez-vous "
        "me dire à qui il vaut mieux s'adresser pour ce type de profil chez {company} ?"
    ),
    ("en", "internship"): (
        "Hello {first}, I am the candidate, an engineering student at ENSAH looking "
        "for a final year (PFE) internship in AI or data at {company} for 2027. One "
        "concrete point about my background: {fact}. I will keep this short. Could "
        "you tell me whether {company} takes PFE interns on these topics this year?"
    ),
    ("en", "job"): (
        "Hello {first}, I am the candidate, an AI and data engineer trained at ENSAH. "
        "I am interested in the {role} position at {company}. One concrete point about "
        "my background: {fact}. I will keep this short out of respect for your time. "
        "Could you point me to the right person for this kind of profile at {company}?"
    ),
}

_BANNED = ("\u2014", "\u2013", "I applied", "j'ai postulé", "I have applied")

# Words that make an outreach message sound generic or needy. Merged with
# the older _BANNED list; lint() checks all of them, case-insensitive.
CRINGE = (
    "passionné", "passionate", "dynamique", "motivé", "synergy", "leverage",
    "I hope this message finds you", "j'espère que ce message vous trouve",
    "n'hésitez pas", "do not hesitate", "don't hesitate", "opportunité incroyable",
    "amazing opportunity", "rockstar", "ninja", "je me permets", "I would be honored",
    "humbly", "!!!",
) + _BANNED

CHANNELS = ("linkedin_note", "linkedin_message", "email")
LIMITS = {"linkedin_note": 300, "linkedin_message": 700, "email": 1200}
SUBJECT_MIN, SUBJECT_MAX = 6, 60
_SENTENCE_SPLIT = re.compile(r"(?<=[.?!])\s+|\n+")


def lint(body: str, channel: str, subject: str | None = None) -> list[str]:
    """Return a list of problems with an outreach draft; [] means clean.

    Codes: banned:<text>, too_long:<n>><limit>, missing_subject,
    subject_length:<n>, too_many_exclamations:<n>, too_many_i_sentences:<n>.
    """
    if channel not in LIMITS:
        raise ValueError(f"unknown channel {channel!r}; expected one of {CHANNELS}")
    problems: list[str] = []
    text = body or ""
    lowered = text.lower()
    for word in CRINGE:
        if word.lower() in lowered:
            problems.append(f"banned:{word}")
    if len(text) > LIMITS[channel]:
        problems.append(f"too_long:{len(text)}>{LIMITS[channel]}")
    if channel == "email":
        subject_text = (subject or "").strip()
        if not subject_text:
            problems.append("missing_subject")
        elif not SUBJECT_MIN <= len(subject_text) <= SUBJECT_MAX:
            problems.append(f"subject_length:{len(subject_text)}")
    exclamations = text.count("!")
    if exclamations > 1:
        problems.append(f"too_many_exclamations:{exclamations}")
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    i_sentences = sum(1 for s in sentences if s.startswith(("I ", "Je ", "J'")))
    if i_sentences > 2:
        problems.append(f"too_many_i_sentences:{i_sentences}")
    return problems

PERSONAS = ("recruiter", "alumni", "senior", "manager", "peer")
_RECRUITER_WORDS = ("talent", "recrut", "recruit", "acquisition", "hr ", " hr", "rh ", " rh", "ressources humaines",
                    "human resources", "people partner", "staffing")
_SENIOR_WORDS = ("partner", "director", "directeur", "directrice", "head", "vp", "vice president", "cto", "cio",
                 "ceo", "chief")
_MANAGER_WORDS = ("manager", "lead", "responsable")


def _words_in(text: str, words: tuple[str, ...]) -> bool:
    padded = f" {text} "
    return any(word in padded for word in words)


def persona(person: dict) -> str:
    """Classify who we write to: recruiter, alumni, senior, manager or peer.

    Only uses evidence already stored on the candidate row (role_seen,
    headline, evidence_quote). Never guesses beyond these words.
    """
    role = str(person.get("role_seen") or "").lower()
    headline = str(person.get("headline") or "").lower()
    quote = str(person.get("evidence_quote") or "").lower()
    everything = " ".join((role, headline, quote))
    if _words_in(everything, _RECRUITER_WORDS):
        return "recruiter"
    if "ensah" in headline or "ensah" in quote or "al hoceima" in everything:
        return "alumni"
    # The role we saw on the evidence page beats the self-written headline.
    for text in (role, headline):
        if _words_in(text, _SENIOR_WORDS):
            return "senior"
        if _words_in(text, _MANAGER_WORDS):
            return "manager"
    return "peer"


# --- Hook: one sentence built from the person's own evidence ----------------

_HOOK_MAX_WORDS = 8
_SCHOOL_WORDS = ("ensah", "alumni", "alumnus", "alumna", "ecole", "école", "university", "université",
                 "school", "phd", "msc", "ingénieur d'état", "graduate", "diplôm")
_COMPANY_SPLIT = re.compile(r"\s+(?:@|at|chez)\s+", re.IGNORECASE)


def _split_role_company(text: str) -> tuple[str, str]:
    parts = _COMPANY_SPLIT.split(text, maxsplit=1)
    role = parts[0].strip(" .")
    company = parts[1].strip(" .") if len(parts) > 1 else ""
    return role, company


def _usable(segment: str) -> bool:
    words = segment.split()
    if not words or len(words) > _HOOK_MAX_WORDS:
        return False
    lowered = segment.lower()
    return not any(word in lowered for word in _SCHOOL_WORDS)


def hook(person: dict, lang: str, company: str | None = None) -> str:
    """One short clause about the person, or '' when the evidence gives nothing safe.

    Prefers a headline segment after '|' or ',' that names a function
    ("Talent Management"); otherwise falls back to role_seen (or the first
    headline segment) plus the company. Never repeats more than eight words
    of the evidence and never quotes evidence_quote.
    """
    lang = "fr" if str(lang).lower().startswith("fr") else "en"
    headline = " ".join(str(person.get("headline") or "").split())
    segments = [s.strip(" .") for s in re.split(r"\s*[|,]\s*", headline) if s.strip(" .")]
    head_role, head_company = _split_role_company(segments[0]) if segments else ("", "")
    company = (company or person.get("company_seen") or head_company or person.get("target_name") or "").strip()
    function = next((s for s in segments[1:] if _usable(s) and not _COMPANY_SPLIT.search(s)), "")
    if function and company:
        if lang == "fr":
            return f"j'ai vu que vous pilotez le {function} chez {company}"
        return f"I saw that you lead {function} at {company}"
    role = str(person.get("role_seen") or "").strip() or head_role
    role, role_company = _split_role_company(role)
    company = company or role_company
    if not _usable(role):
        return ""
    if lang == "fr":
        return f"j'ai vu que vous êtes {role} chez {company}" if company else f"j'ai vu que vous êtes {role}"
    return f"I saw that you are {role} at {company}" if company else f"I saw that you are {role}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_name(name: str) -> str:
    tokens = (name or "").strip().split()
    return tokens[0] if tokens else "there"


def _kind(opportunity: dict | None) -> str:
    kind = str((opportunity or {}).get("role_kind") or "").lower()
    if any(word in kind for word in ("intern", "stage", "pfe")):
        return "internship"
    return "job"


def draft_for(contact: dict, opportunity: dict | None, lang: str, fact: str,
              channel: str = "linkedin") -> str:
    """Build a short, plain outreach message in 'fr' or 'en'."""
    lang = "fr" if str(lang).lower().startswith("fr") else "en"
    kind = _kind(opportunity)
    fallback = "votre entreprise" if lang == "fr" else "your company"
    company = contact.get("company") or (opportunity or {}).get("company") or fallback
    role = (opportunity or {}).get("title") or ("proposé" if lang == "fr" else "open")
    body = _TEMPLATES[(lang, kind)].format(
        first=_first_name(contact.get("name", "")), company=company,
        role=role, fact=(fact or "").strip().rstrip("."),
    )
    body = " ".join(body.split())
    for banned in _BANNED:
        if banned in body:
            raise ValueError(f"draft contains banned text: {banned!r}")
    return body


def save_draft(conn: sqlite3.Connection, contact_id: str, opportunity_id, contact_route_id,
               channel: str, lang: str, body: str, subject: str | None = None,
               fact: str | None = None) -> str:
    """Insert the draft with status draft_not_opened and return its id."""
    draft_id = "dr_" + uuid.uuid4().hex
    now = _now()
    source = {"generator": "reach", "fact": fact, "lang": lang}
    conn.execute(
        "INSERT INTO drafts(id, opportunity_id, contact_id, contact_route_id, channel, subject, "
        "body, status, source_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (draft_id, opportunity_id, contact_id, contact_route_id, channel, subject or "",
         body, DRAFT_STATUS, json.dumps(source, ensure_ascii=False, sort_keys=True), now, now),
    )
    conn.commit()
    return draft_id
