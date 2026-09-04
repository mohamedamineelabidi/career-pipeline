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


# --- compose: persona x channel x lang -------------------------------------

KINDS = ("internship", "job")
_SECTOR_TAGS = {
    "telecom": ("telecom", "orange", "inwi", "maroc telecom", "network", "réseau", "reseau"),
    "cloud": ("cloud", "gcp", "azure", "aws", "devops"),
    "genai": ("data", "ai", "ia", "intelligence", "machine learning", "ml", "genai", "llm", "analytics"),
    "recruiting": ("recrut", "recruit", "talent", "rh", "hr", "people"),
}
_MANAGER_PROOF = {"telecom": "netix", "cloud": "upfund", "genai": "arya", "recruiting": "arya"}
_RECRUITER_PROOF = {"telecom": "netix", "cloud": "upfund", "genai": "arya", "recruiting": "arya"}


def _proof_by_id(sheet: dict, proof_id: str) -> dict:
    return next(p for p in sheet["proofs"] if p["id"] == proof_id)


def _sector_of(text: str) -> str | None:
    padded = f" {text.lower()} "
    for sector, words in _SECTOR_TAGS.items():
        if any((f" {w} " in padded) or (len(w) > 4 and f" {w}" in padded) for w in words):
            return sector
    return None


def _choose_proof(persona_name: str, person: dict, company: str) -> str:
    headline = " ".join(str(person.get("headline") or "").split())
    # Judge the function, not the employer: "Head of Data @ Orange" is a data role.
    headline = " ".join(_split_role_company(seg)[0] for seg in re.split(r"\s*[|,]\s*", headline))
    if persona_name == "recruiter":
        sector = _sector_of(f"{company} {person.get('target_sector') or ''}")
        return _RECRUITER_PROOF.get(sector or "", "arya")
    if persona_name == "manager":
        sector = _sector_of(f"{headline} {person.get('role_seen') or ''}") or _sector_of(company)
        return _MANAGER_PROOF.get(sector or "", "arya")
    if persona_name == "alumni":
        return "netix" if _sector_of(company) == "telecom" else "club"
    if persona_name == "senior":
        return "upfund"
    sector = _sector_of(f"{headline} {person.get('role_seen') or ''}") or _sector_of(company)
    return _MANAGER_PROOF.get(sector or "", "arya")


def _topic(person: dict, lang: str) -> str:
    text = f"{person.get('headline') or ''} {person.get('role_seen') or ''}"
    sector = _sector_of(text)
    if sector == "telecom":
        return "le réseau" if lang == "fr" else "network"
    if sector == "cloud":
        return "le cloud" if lang == "fr" else "cloud"
    return "la data et l'IA" if lang == "fr" else "data and AI"


def _ask(persona_name: str, lang: str, company: str, person: dict, short: bool) -> str:
    fr = lang == "fr"
    if persona_name == "recruiter":
        if short:
            return ("Y a-t-il un recrutement de stagiaires PFE cette année chez vous ?" if fr
                    else "Is there a PFE intern intake this year on your side?")
        return (f"Y a-t-il un recrutement de stagiaires PFE cette année chez {company}, et qui pilote ce sujet ?" if fr
                else f"Is there a PFE intern intake this year at {company}, and who owns it?")
    if persona_name == "manager":
        return ("Un échange de 15 minutes aurait-il du sens pour vous ?" if fr
                else "Would a 15-minute call make sense for you?")
    if persona_name == "alumni":
        return (f"Comment avez-vous abordé le PFE chez {company} ?" if fr
                else f"How did you approach the PFE at {company}?")
    if persona_name == "senior":
        return ("Qui dans votre équipe serait la bonne personne pour ce sujet ?" if fr
                else "Who on your team would be the right person for this?")
    topic = _topic(person, lang)
    return (f"Comment l'équipe travaille-t-elle sur {topic} chez {company} ?" if fr
            else f"How is the team at {company} working on {topic}?")


def _subject(lang: str, kind: str, company: str) -> str:
    if lang == "fr":
        subject = f"Stage PFE 2027 IA / data chez {company}" if kind == "internship" else f"Premier poste IA / data chez {company}"
    else:
        subject = f"PFE internship 2027, AI and data, {company}" if kind == "internship" else f"First AI / data role, {company}"
    if len(subject) > SUBJECT_MAX:
        subject = subject[:SUBJECT_MAX].rsplit(" ", 1)[0]
    return subject


def compose(person: dict, channel: str, lang: str, kind: str = "internship", company: str | None = None) -> dict:
    """Build one outreach draft around the person: greeting, hook, who I am, one proof, one ask, close.

    Returns {subject, body, persona, proof_id, lint}. subject is None off email.
    """
    if channel not in LIMITS:
        raise ValueError(f"unknown channel {channel!r}; expected one of {CHANNELS}")
    lang = str(lang).lower()
    if lang not in ("fr", "en"):
        raise ValueError("lang must be fr|en")
    if kind not in KINDS:
        raise ValueError("kind must be internship|job")
    sheet = about_me()
    fr = lang == "fr"
    first = _first_name(person.get("name", ""))
    company = (company or person.get("company_seen") or person.get("target_name") or "").strip()
    if not company:
        _, head_company = _split_role_company(str(person.get("headline") or "").split("|")[0].split(",")[0])
        company = head_company or ("votre entreprise" if fr else "your company")
    who = persona(person)
    proof_id = _choose_proof(who, person, company)
    proof = _proof_by_id(sheet, proof_id)[lang]
    status = sheet["status_" + lang]
    seeking = sheet["seeking"][kind][lang]
    greeting = f"Bonjour {first}," if fr else f"Hi {first},"

    if channel == "linkedin_note":
        me = (f"Je suis {status} et je cherche un {seeking}." if fr
              else f"I am a {status}, currently looking for a {seeking}.")
        lines = [greeting, me, _ask(who, lang, company, person, short=True),
                 "Merci, the candidate" if fr else "Thank you, the candidate"]
        body = "\n".join(lines)
        return {"subject": None, "body": body, "persona": who, "proof_id": proof_id,
                "lint": lint(body, channel)}

    hook_text = hook(person, lang, company=company)
    paragraphs = [greeting]
    if hook_text:
        paragraphs.append(hook_text[0].upper() + hook_text[1:] + ".")
    paragraphs.append(f"Je suis {status} et je cherche un {seeking}." if fr
                      else f"I am a {status}, currently looking for a {seeking}.")
    paragraphs.append(("Un point concret : " if fr else "One concrete point: ") + proof + ".")
    paragraphs.append(_ask(who, lang, company, person, short=False))
    subject = None
    if channel == "email":
        subject = _subject(lang, kind, company)
        close = ["Merci pour votre temps," if fr else "Thank you for your time,", sheet["signature_" + lang]]
        if sheet["links"].get("linkedin"):
            close.append("LinkedIn: " + sheet["links"]["linkedin"])
        if sheet["links"].get("github"):
            close.append("GitHub: " + sheet["links"]["github"])
        paragraphs.append("\n".join(close))
    else:
        paragraphs.append("Merci, the candidate" if fr else "Thank you, the candidate")
    body = "\n\n".join(paragraphs)
    return {"subject": subject, "body": body, "persona": who, "proof_id": proof_id,
            "lint": lint(body, channel, subject)}


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
               fact: str | None = None, extra: dict | None = None) -> str:
    """Insert the draft with status draft_not_opened and return its id."""
    draft_id = "dr_" + uuid.uuid4().hex
    now = _now()
    source = {"generator": "reach", "fact": fact, "lang": lang}
    source.update(extra or {})
    conn.execute(
        "INSERT INTO drafts(id, opportunity_id, contact_id, contact_route_id, channel, subject, "
        "body, status, source_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (draft_id, opportunity_id, contact_id, contact_route_id, channel, subject or "",
         body, DRAFT_STATUS, json.dumps(source, ensure_ascii=False, sort_keys=True), now, now),
    )
    conn.commit()
    return draft_id
