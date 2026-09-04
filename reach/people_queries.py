"""Search query builder for finding people at a target company.

Pure function, no network. The queries mix French and English and never
contain an email address or an email-pattern guess.
"""

from __future__ import annotations

INTENTS = ("any", "internship", "job")


def _clean(company: str) -> str:
    return " ".join(str(company or "").replace("@", " ").split())


def queries_for(company: str, intent: str = "any") -> list[str]:
    """Return at least six distinct FR/EN search strings for ``company``.

    Coverage: talent acquisition / recruiters at "<company> Maroc", AI/Data
    managers or leads in Casablanca/Rabat, PFE/stage recruitment and ENSAH
    alumni at the company.
    """
    name = _clean(company)
    if not name:
        raise ValueError("company is required")
    intent = (intent or "any").lower()
    if intent not in INTENTS:
        raise ValueError(f"unknown intent: {intent}")

    queries = [
        f"{name} Maroc talent acquisition",
        f"{name} Maroc recruteur recrutement",
        f"{name} Morocco recruiter",
        f"{name} Casablanca AI manager OR data lead",
        f"{name} Rabat responsable data OR intelligence artificielle",
        f"{name} head of data Morocco",
        f"{name} ENSAH alumni",
        f"{name} laureat ENSAH Al Hoceima",
    ]
    if intent in ("any", "internship"):
        queries.extend(
            [
                f"{name} stage PFE data",
                f"{name} recrutement stagiaires PFE Maroc",
                f"{name} internship AI data Morocco",
            ]
        )
    if intent in ("any", "job"):
        queries.extend(
            [
                f"{name} recrute ingenieur data Maroc",
                f"{name} hiring AI engineer Morocco",
            ]
        )

    seen: set[str] = set()
    out: list[str] = []
    for query in queries:
        key = query.casefold()
        if key in seen or "@" in query:
            continue
        seen.add(key)
        out.append(query)
    return out
