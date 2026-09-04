"""Email finder for Reach people candidates.

Three tiers, in this order, and the tier is always recorded next to the address:

1. ``found_official`` / ``found_public``: the address was read on a page.
2. ``inferred``: a pattern observed at least twice on the same company domain,
   applied to this person. Never presented as a fact.
3. ``none``: nothing observed, so nothing is stored.

Nothing here sends mail. The SMTP probe only asks the server whether it would
accept a recipient and then quits.
"""
from __future__ import annotations

import re
import unicodedata

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

GENERIC_LOCALPARTS = frozenset({
    "careers", "contact", "info", "rh", "hr", "recrutement", "recruitment", "jobs",
    "press", "presse", "noreply", "no-reply", "support", "admin", "webmaster",
    "communication", "marketing", "sales", "privacy", "legal",
})

_OBFUSCATION = (
    (re.compile(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", re.IGNORECASE), "@"),
    (re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.IGNORECASE), "."),
)


def deobfuscate(text: str) -> str:
    for pattern, replacement in _OBFUSCATION:
        text = pattern.sub(replacement, text)
    return text


def _domain_allowed(domain: str, domains: set[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


def extract_emails(text: str, domains) -> list[str]:
    """Return the personal-looking addresses on allowed domains, lower-cased,
    de-duplicated, in order of first appearance."""
    allowed = {d.lower() for d in (domains or ())}
    if not allowed:
        return []
    seen: list[str] = []
    for match in EMAIL_RE.findall(deobfuscate(text or "")):
        address = match.lower().strip(".")
        local, _, domain = address.rpartition("@")
        if not local or local in GENERIC_LOCALPARTS or not _domain_allowed(domain, allowed):
            continue
        if address not in seen:
            seen.append(address)
    return seen


# --- Company domains: from evidence, never from guesses -------------------

# Hosts that are never a company's own mail domain.
EXCLUDED_HOSTS = frozenset({
    "linkedin.com", "facebook.com", "glassdoor.com", "indeed.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "wikipedia.org", "crunchbase.com", "bloomberg.com",
    "rekrute.com", "emploi.ma", "welcometothejungle.com", "google.com", "gmail.com",
})

# Second-level labels under which the registered name sits one level deeper.
_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "gov", "ac", "edu", "press"})


def registered_domain(host: str) -> str:
    """'www2.deloitte.com' -> 'deloitte.com'; 'x.co.uk' -> 'x.co.uk'."""
    host = (host or "").lower().strip(".").split(":")[0]
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    if labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _is_excluded(domain: str) -> bool:
    return any(domain == h or domain.endswith("." + h) for h in EXCLUDED_HOSTS)


def _lead_domains(conn, company: str) -> set[str]:
    """Registered domains of email routes already stored for contacts at this company."""
    like = f"%{company.strip()}%"
    rows = conn.execute(
        "SELECT r.value FROM contact_routes r JOIN contacts c ON c.id = r.contact_id"
        " WHERE r.route_type = 'email' AND c.company LIKE ? COLLATE NOCASE", (like,)
    ).fetchall()
    domains = set()
    for row in rows:
        _, _, host = str(row[0]).lower().rpartition("@")
        domain = registered_domain(host)
        if domain and not _is_excluded(domain):
            domains.add(domain)
    return domains


def _official_site_domains(company: str, search_fn) -> set[str]:
    """Registered domains of the official-site hits of ONE company search."""
    from urllib.parse import urlparse
    try:
        hits = search_fn(f"{company} site officiel") or []
    except Exception:  # noqa: BLE001 - search outage means no extra domains, not a crash
        return set()
    domains = set()
    for hit in hits:
        host = urlparse(str(hit.get("url") or "")).hostname or ""
        domain = registered_domain(host)
        if domain and not _is_excluded(domain):
            domains.add(domain)
    return domains


def company_domains(conn, company: str, search_fn) -> set[str]:
    """Domains we are allowed to look for addresses on: existing leads' email
    domains plus the company's official site. Empty when nothing was observed."""
    if not (company or "").strip():
        return set()
    domains = _lead_domains(conn, company)
    domains |= _official_site_domains(company, search_fn)
    return domains


# --- Pattern learning from observed addresses only ------------------------

PATTERNS = ("{first}.{last}", "{f}{last}", "{first}{last}", "{first}_{last}",
            "{last}.{first}", "{f}.{last}")
MIN_OBSERVATIONS = 2
MIN_AGREEMENT = 0.8


def _slug(value: str) -> str:
    """Lower-case ASCII letters and digits only: 'Mélanie' -> 'melanie', 'the candidate' -> 'elabidi'."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", ascii_only.lower())


def split_name(full_name: str) -> tuple[str, str]:
    """Last whitespace token is the last name, with a particle (el, ben, al, de...) kept with it."""
    tokens = (full_name or "").split()
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return tokens[0], ""
    particles = {"el", "al", "ben", "bin", "de", "da", "di", "du", "der", "van", "von", "la", "le", "ait", "ou"}
    cut = len(tokens) - 1
    if cut >= 2 and tokens[cut - 1].lower() in particles:
        cut -= 1
    return " ".join(tokens[:cut]), " ".join(tokens[cut:])


def apply_pattern(pattern: str, first: str, last: str, domain: str) -> str:
    first_slug, last_slug = _slug(first), _slug(last)
    local = pattern.format(first=first_slug, last=last_slug, f=first_slug[:1], l=last_slug[:1])
    return f"{local}@{domain.lower()}"


def learn_pattern(observed: list[str], people: list[tuple[str, str]]) -> str | None:
    """Return the pattern that explains >= 2 observed addresses and >= 80% of
    them, else None. ``people`` gives (first, last) for each observed address."""
    pairs = [(addr.lower(), first, last) for addr, (first, last) in zip(observed, people)
             if addr and "@" in addr and _slug(first) and _slug(last)]
    if len(pairs) < MIN_OBSERVATIONS:
        return None
    best, best_hits = None, 0
    for pattern in PATTERNS:
        hits = sum(1 for addr, first, last in pairs
                   if addr.rpartition("@")[0] == apply_pattern(pattern, first, last, "x").rpartition("@")[0])
        if hits > best_hits:
            best, best_hits = pattern, hits
    if best_hits >= MIN_OBSERVATIONS and best_hits / len(pairs) >= MIN_AGREEMENT:
        return best
    return None
