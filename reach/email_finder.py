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
    if search_fn is not None:
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


# --- MX + SMTP verification -------------------------------------------------

import secrets
import smtplib
import socket
from dataclasses import dataclass

SMTP_TIMEOUT_S = 10
SMTP_PORT = 25


@dataclass
class Probe:
    """What one SMTP conversation with one MX host said."""
    connected: bool
    rcpt_ok: bool
    banner_rejected: bool = False   # the server sent a 5xx banner before EHLO


@dataclass
class Result:
    mx_ok: bool
    smtp_ok: bool
    catch_all: bool
    verdict: str   # accepted | rejected | unverifiable_catch_all | unverifiable_no_smtp | unverifiable_smtp_rejected


def _mx_hosts(domain: str) -> list[str]:
    """MX hosts by preference; empty when the domain has none or DNS fails."""
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX", lifetime=SMTP_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - any DNS trouble means 'cannot verify'
        return []
    hosts = sorted((int(r.preference), str(r.exchange).rstrip(".")) for r in answers)
    return [host for _, host in hosts if host]


def _smtp_probe(host: str, addr: str, smtp_cls=smtplib.SMTP) -> Probe:
    """Ask ``host`` whether it would accept ``addr`` as a recipient, then quit.
    Nothing is sent. A 5xx banner before EHLO is recorded separately."""
    try:
        client = smtp_cls(host, SMTP_PORT, timeout=SMTP_TIMEOUT_S)
    except (OSError, smtplib.SMTPException):
        return Probe(False, False)
    try:
        code, _ = client.connect(host, SMTP_PORT)
        if code >= 500:
            return Probe(False, False, banner_rejected=True)
        if code >= 400:
            return Probe(False, False)
        client.ehlo_or_helo_if_needed()
        code, _ = client.mail("")
        if code >= 400:
            return Probe(True, False)
        code, _ = client.rcpt(addr)
        return Probe(True, 200 <= code < 300)
    except (OSError, smtplib.SMTPException, socket.timeout):
        return Probe(False, False)
    finally:
        try:
            client.quit()
        except Exception:  # noqa: BLE001
            pass


def _random_local() -> str:
    return secrets.token_hex(8)


def verify_email(addr: str, mx_fn=None, probe_fn=None) -> Result:
    """MX lookup then SMTP RCPT probe of ``addr`` and of a random local part on
    the same domain (catch-all detection). Never trusts a catch-all."""
    mx_fn = mx_fn or _mx_hosts
    probe_fn = probe_fn or _smtp_probe
    _, _, domain = (addr or "").lower().rpartition("@")
    hosts = mx_fn(domain) if domain else []
    if not hosts:
        return Result(False, False, False, "unverifiable_no_smtp")
    banner_rejected = False
    for host in hosts:
        probe = probe_fn(host, addr)
        if probe.banner_rejected:
            banner_rejected = True
            continue
        if not probe.connected:
            continue
        if not probe.rcpt_ok:
            return Result(True, True, False, "rejected")
        control = probe_fn(host, f"{_random_local()}@{domain}")
        if control.connected and control.rcpt_ok:
            return Result(True, True, True, "unverifiable_catch_all")
        return Result(True, True, False, "accepted")
    if banner_rejected:
        return Result(True, False, False, "unverifiable_smtp_rejected")
    return Result(True, False, False, "unverifiable_no_smtp")


# --- Orchestration: evidence first, pattern second, and record which -------

from datetime import datetime, timezone
from urllib.parse import urlparse

MAX_PAGES_PER_PERSON = 5
UNVERIFIABLE_PREFIX = "unverifiable_"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _search_hits(search_fn, query: str) -> list[dict]:
    """Exa 'people' category first, then the open web. Never an '@' in the query."""
    hits: list[dict] = []
    for category in ("people", None):
        try:
            hits.extend(search_fn(query, category) or [])
        except TypeError:
            hits.extend(search_fn(query) or [])
        except Exception:  # noqa: BLE001 - one channel down is not a crash
            continue
    return hits


def _page_text(read_fn, url: str) -> str:
    try:
        result = read_fn(url)
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(result, dict):
        return str(result.get("text") or "")
    if isinstance(result, tuple):
        return str(result[0] or "")
    return str(result or "")


def _observed_at_company(conn, company: str) -> tuple[list[str], list[tuple[str, str]]]:
    like = f"%{company.strip()}%"
    rows = conn.execute(
        "SELECT c.name, r.value FROM contact_routes r JOIN contacts c ON c.id = r.contact_id"
        " WHERE r.route_type = 'email' AND c.company LIKE ? COLLATE NOCASE", (like,)
    ).fetchall()
    observed, people = [], []
    for name, value in rows:
        observed.append(str(value).lower())
        people.append(split_name(str(name)))
    return observed, people


def _store(conn, candidate_id: str, *, email: str, email_status: str, evidence_url: str,
           verification_status: str | None) -> None:
    now = _now()
    conn.execute(
        "UPDATE people_candidates SET email = ?, email_status = ?, email_evidence_url = ?,"
        " email_checked_at = ?, updated_at = ? WHERE id = ?",
        (email, email_status, evidence_url, now, now, candidate_id),
    )
    if verification_status:
        conn.execute("UPDATE people_candidates SET verification_status = ? WHERE id = ?",
                     (verification_status, candidate_id))


def find_email(conn, candidate: dict, search_fn, read_fn, verify_fn, company_search_fn=None) -> dict:
    """Find one candidate's address. Returns {email, email_status, evidence_url, verdict}.

    ``search_fn(query, category)`` is the people/web search; ``company_search_fn``
    (optional, Exa 'company' category) adds the official-site domain to the
    lead-observed domains.

    1. Read on a page (found_official on a company domain, found_public elsewhere).
    2. Learned pattern from >= 2 observed lead emails at the company, then an SMTP
       probe: rejected -> 'rejected'; accepted or unverifiable -> 'inferred'.
    3. Nothing observed -> 'none'.
    """
    candidate_id = str(candidate["id"])
    name = str(candidate.get("name") or "").strip()
    company = str(candidate.get("company_seen") or "").strip()
    if not company and candidate.get("target_company_id"):
        row = conn.execute("SELECT name FROM target_companies WHERE id = ?",
                           (candidate["target_company_id"],)).fetchone()
        company = str(row[0]) if row else ""
    domains = company_domains(conn, company, search_fn=company_search_fn)

    # 1. evidence on a page
    if name and domains:
        read = 0
        for hit in _search_hits(search_fn, f'"{name}" {company}'.strip()):
            url = str(hit.get("url") or "")
            host = registered_domain(urlparse(url).hostname or "")
            if not url or _is_excluded(host):
                continue
            emails = extract_emails(_page_text(read_fn, url), domains)
            read += 1
            if emails:
                status = "found_official" if host in domains else "found_public"
                level = "official_company_public" if status == "found_official" else "professional_public"
                _store(conn, candidate_id, email=emails[0], email_status=status,
                       evidence_url=url, verification_status=level)
                return {"email": emails[0], "email_status": status, "evidence_url": url, "verdict": "read"}
            if read >= MAX_PAGES_PER_PERSON:
                break

    # 2. learned pattern + probe
    observed, people = _observed_at_company(conn, company)
    first, last = split_name(name)
    pattern = learn_pattern(observed, people) if first and last else None
    if pattern:
        domain = max((addr.rpartition("@")[2] for addr in observed),
                     key=lambda d: sum(1 for a in observed if a.endswith("@" + d)))
        guess = apply_pattern(pattern, first, last, domain)
        result = verify_fn(guess)
        verdict = getattr(result, "verdict", "unverifiable_no_smtp") if result else "unverifiable_no_smtp"
        if verdict == "rejected":
            _store(conn, candidate_id, email="", email_status="rejected", evidence_url="", verification_status=None)
            return {"email": "", "email_status": "rejected", "evidence_url": "", "verdict": verdict}
        _store(conn, candidate_id, email=guess, email_status="inferred", evidence_url="", verification_status=None)
        return {"email": guess, "email_status": "inferred", "evidence_url": "", "verdict": verdict}

    # 3. nothing observed
    _store(conn, candidate_id, email=str(candidate.get("email") or ""), email_status="none",
           evidence_url="", verification_status=None)
    return {"email": "", "email_status": "none", "evidence_url": "", "verdict": "no_pattern"}
