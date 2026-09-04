"""Discover people candidates from public web search results.

No network code lives here: the caller passes ``search_fn`` and ``read_fn``.
LinkedIn, Glassdoor and Indeed pages are never read; a LinkedIn URL is only
stored as a profile URL. Reads to the same host are paced via ``sleep_fn``.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

from reach.people_queries import queries_for

EVIDENCE_QUOTE_MAX = 240
NEVER_READ_HOSTS = ("linkedin.com", "glassdoor.", "indeed.")

_ROLE_WORDS = (
    "recruiter",
    "recruteur",
    "recruteuse",
    "talent acquisition",
    "chargee de recrutement",
    "charge de recrutement",
    "responsable recrutement",
    "hr business partner",
    "head of data",
    "head of ai",
    "data manager",
    "data lead",
    "ai lead",
    "ai manager",
    "ml lead",
    "chief data officer",
    "cto",
    "data scientist",
    "data engineer",
    "ml engineer",
    "ai engineer",
    "responsable data",
    "directeur data",
    "manager",
)

# "Firstname Lastname" with optional middle token, capitalised, accents allowed.
_NAME_RE = re.compile(
    r"\b([A-ZÀ-Ý][a-zà-ÿ'\-]+(?:\s+[A-ZÀ-Ý][a-zà-ÿ'\-]+){1,2})\b"
)
_NAME_STOP = {
    "linkedin",
    "casablanca",
    "rabat",
    "maroc",
    "morocco",
    "data",
    "talent",
    "acquisition",
    "manager",
    "head",
    "lead",
    "group",
    "the",
    "for",
    "and",
    "our",
    "team",
    "jobs",
    "job",
    "careers",
    "stage",
    "pfe",
    "offre",
    "offres",
    "recrutement",
    "recruteur",
    "engineer",
    "ingenieur",
    "senior",
    "junior",
    "profil",
    "profile",
    "google",
    "bing",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_linkedin(url: str) -> bool:
    host = host_of(url)
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def never_read(url: str) -> bool:
    host = host_of(url)
    if not host:
        return True
    for marker in NEVER_READ_HOSTS:
        if marker.endswith("."):
            if host.startswith(marker) or ("." + marker) in host + ".":
                return True
        elif host == marker or host.endswith("." + marker):
            return True
    return False


def normalize_profile_url(url: str) -> str:
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), (parts.netloc or "").lower(), path, "", "")).lower()


def clip_quote(text: str, limit: int = EVIDENCE_QUOTE_MAX) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


def extract_role(*texts: str) -> str:
    blob = " ".join(t for t in texts if t).lower()
    for word in _ROLE_WORDS:
        if word in blob:
            return word
    return ""


def extract_name(*texts: str, company: str = "") -> str:
    company_tokens = {t.lower() for t in company.split()}
    for text in texts:
        if not text:
            continue
        for match in _NAME_RE.finditer(text):
            candidate = match.group(1)
            tokens = [t.lower().strip("'-") for t in candidate.split()]
            if any(t in _NAME_STOP or t in company_tokens for t in tokens):
                continue
            return candidate
    return ""


_TITLE_NOISE_RE = re.compile(r"\s*[|\-–:,]\s*.*$")


def _clean_profile_title(title: str, company: str = "") -> str:
    """'Hajar Ghzala - Talent Acquisition | LinkedIn' -> 'Hajar Ghzala'. Empty when
    the leading segment does not look like a 2-4 word personal name."""
    head = _TITLE_NOISE_RE.sub("", (title or "").strip())
    tokens = head.split()
    if not 2 <= len(tokens) <= 4:
        return ""
    lowered = {t.lower().strip("'-") for t in tokens}
    company_tokens = {t.lower() for t in company.split()}
    if lowered & (_NAME_STOP | company_tokens):
        return ""
    if not all(t[:1].isalpha() for t in tokens):
        return ""
    return " ".join(t if t.isupper() and len(t) > 3 else t[:1].upper() + t[1:] for t in tokens)


def _profile_exists(conn, url: str) -> bool:
    key = normalize_profile_url(url)
    for (stored,) in conn.execute(
        "SELECT profile_url FROM people_candidates WHERE profile_url != '' AND lower(profile_url) LIKE ?",
        ("%" + key.rsplit("/in/", 1)[-1].lower() + "%",),
    ):
        if normalize_profile_url(stored) == key:
            return True
    return False


def _linkedin_name_from_url(url: str) -> str:
    path = urlsplit(url).path
    match = re.search(r"/in/([^/]+)", path)
    if not match:
        return ""
    slug = re.sub(r"-[0-9a-f]{4,}$", "", match.group(1))
    return " ".join(p.capitalize() for p in slug.split("-") if p and not p.isdigit())


class _Pacer:
    def __init__(self, pace_seconds: float, sleep_fn: Callable[[float], None]):
        self.pace = float(pace_seconds)
        self.sleep_fn = sleep_fn
        self.seen: set[str] = set()

    def before_read(self, host: str) -> None:
        if host in self.seen and self.pace > 0:
            self.sleep_fn(self.pace)
        self.seen.add(host)


def _score_row(row: dict) -> int:
    try:
        from reach.scoring import score_candidate
    except ImportError:
        return 0
    return int(score_candidate(row, str(row.get("company_seen") or "")))


def _insert_candidate(conn, row: dict) -> str:
    candidate_id = "pc_" + uuid.uuid4().hex
    now = _now()
    conn.execute(
        """
        INSERT INTO people_candidates(
            id, target_company_id, name, headline, company_seen, role_seen,
            profile_url, email, evidence_url, evidence_quote, discovered_via,
            score, verification_status, current_role_confirmed_at,
            promoted_contact_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
        """,
        (
            candidate_id,
            row["target_company_id"],
            row.get("name") or "",
            row.get("headline") or "",
            row.get("company_seen") or "",
            row.get("role_seen") or "",
            row.get("profile_url") or "",
            "",
            row.get("evidence_url") or "",
            row.get("evidence_quote") or "",
            "public_web",
            _score_row({**row, "verification_status": "unverified", "created_at": now}),
            "unverified",
            now,
            now,
        ),
    )
    return candidate_id


def _existing_keys(conn, target_id: str) -> tuple[set[str], set[str]]:
    urls: set[str] = set()
    names: set[str] = set()
    for row in conn.execute(
        "SELECT name, company_seen, profile_url FROM people_candidates WHERE target_company_id = ?",
        (target_id,),
    ):
        if row[2]:
            urls.add(normalize_profile_url(row[2]))
        if row[0]:
            names.add(f"{row[0]}|{row[1]}".lower())
    return urls, names


def discover_public(
    conn,
    target_id: str,
    company: str,
    search_fn: Callable[[str], Iterable[dict]],
    read_fn: Callable[[str], str],
    pace_seconds: float = 2.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    intent: str = "any",
) -> list[str]:
    """Run the FR/EN queries for ``company`` and store people candidates.

    Returns the ids of the rows inserted during this call.
    """
    pacer = _Pacer(pace_seconds, sleep_fn)
    seen_urls, seen_names = _existing_keys(conn, target_id)
    read_urls: set[str] = set()
    inserted: list[str] = []

    for query in queries_for(company, intent):
        for result in search_fn(query) or []:
            url = str(result.get("url") or "").strip()
            if not url:
                continue
            title = str(result.get("title") or "")
            snippet = str(result.get("snippet") or "")

            if is_linkedin(url):
                key = normalize_profile_url(url)
                if key in seen_urls or _profile_exists(conn, url):
                    # already known (this run, or found earlier for another
                    # target): keep the existing row, never abort the run.
                    seen_urls.add(key)
                    continue
                # A LinkedIn result's title IS the profile owner's name (search
                # engines index "Firstname Lastname"); the snippet leads with a job
                # title, which the generic heuristic mistakes for a person.
                name = (_clean_profile_title(title, company)
                        or _linkedin_name_from_url(url)
                        or extract_name(snippet, company=company))
                name_key = f"{name}|{company}".lower()
                if name and name_key in seen_names:
                    continue
                seen_urls.add(key)
                if name:
                    seen_names.add(name_key)
                inserted.append(
                    _insert_candidate(
                        conn,
                        {
                            "target_company_id": target_id,
                            "name": name,
                            # the search channel's headline is the person's current
                            # title; fall back to the page title only when absent
                            "headline": clip_quote(str(result.get("headline") or "") or title),
                            "company_seen": company,
                            "role_seen": extract_role(title, snippet),
                            "profile_url": url,
                            "evidence_url": url,
                            "evidence_quote": clip_quote(snippet or title),
                        },
                    )
                )
                continue

            if never_read(url):
                continue

            url_key = normalize_profile_url(url)
            if url_key in read_urls:
                continue
            read_urls.add(url_key)
            host = host_of(url)
            pacer.before_read(host)
            try:
                text = str(read_fn(url) or "")
            except Exception:  # noqa: BLE001 - one bad page must not stop the run
                text = ""

            name = extract_name(title, snippet, text[:2000], company=company)
            if not name:
                continue
            name_key = f"{name}|{company}".lower()
            if name_key in seen_names:
                continue
            seen_names.add(name_key)
            role = extract_role(title, snippet, text[:2000])
            inserted.append(
                _insert_candidate(
                    conn,
                    {
                        "target_company_id": target_id,
                        "name": name,
                        "headline": clip_quote(title),
                        "company_seen": company,
                        "role_seen": role,
                        "profile_url": "",
                        "evidence_url": url,
                        "evidence_quote": clip_quote(snippet or text or title),
                    },
                )
            )
    conn.commit()
    return inserted
