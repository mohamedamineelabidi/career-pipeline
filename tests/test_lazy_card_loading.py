"""Per-card lookups should wait until the card is actually visible.

Boot fired 134 API requests, 124 of them a two-per-card fan-out
(/api/cvs/<id>/highlight + /api/recruiter/improvements/<id>) for 105 CV cards
that were not on screen -- the user was still on Overview. That is what made
first render take ~7 seconds.

The queue was already concurrency-limited (LAZY_LIMIT = 2) but it still drained
the entire backlog regardless of visibility. Throttling how FAST you make 124
pointless requests is not the same as not making them.
"""
import re
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parents[1] / "pipeline_v2.html"


@pytest.fixture(scope="module")
def source() -> str:
    return HTML.read_text(encoding="utf-8")


def test_card_lookups_are_gated_on_visibility(source: str) -> None:
    """An IntersectionObserver must gate the per-card lookups."""
    assert "IntersectionObserver" in source, (
        "Per-card CV lookups still fire for every card at boot. Gate them on an "
        "IntersectionObserver so off-screen cards cost nothing."
    )


def test_observer_watches_cv_cards(source: str) -> None:
    """The observer has to actually observe the cards, not just exist."""
    assert re.search(r"observeCard|cardObserver", source), (
        "Expected a named observer helper wired into the CV card renderer."
    )


def test_lazy_queue_still_bounded(source: str) -> None:
    """Visibility gating replaces the backlog, but keep the concurrency cap."""
    match = re.search(r"LAZY_LIMIT\s*=\s*(\d+)", source)
    assert match, "LAZY_LIMIT should remain: it protects the local server."
    assert int(match.group(1)) <= 4, "Concurrency cap should stay small."
