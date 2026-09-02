from html.parser import HTMLParser
from pathlib import Path
import pytest
import re
import shutil
import subprocess


HTML_PATH = Path(__file__).resolve().parents[1] / "pipeline_v2.html"


class DashboardParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.start_tags = []
        self.sections = set()
        self.controls = []
        self._control = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.start_tags.append((tag, attrs))
        if tag == "section" and attrs.get("id"):
            self.sections.add(attrs["id"])
        if tag in {"button", "a"}:
            self._control = {"tag": tag, "attrs": attrs, "text": []}

    def handle_data(self, data):
        if self._control is not None:
            self._control["text"].append(data)

    def handle_endtag(self, tag):
        if self._control is not None and tag == self._control["tag"]:
            self._control["text"] = " ".join("".join(self._control["text"]).split())
            self.controls.append(self._control)
            self._control = None


def source():
    return HTML_PATH.read_text(encoding="utf-8")


def parsed():
    parser = DashboardParser()
    parser.feed(source())
    return parser


def test_dashboard_is_a_complete_offline_document():
    text = source()
    assert "<!doctype html>" in text.lower()
    assert "<style>" in text and "<script>" in text
    assert not re.search(r'<script\b[^>]*\bsrc\s*=', text, re.I)
    assert not re.search(r'<link\b[^>]*\brel\s*=\s*["\']stylesheet', text, re.I)
    assert not re.search(r'url\(\s*["\']?https?://', text, re.I)
    assert not re.search(r'<(?:img|source)\b[^>]*\bsrc\s*=\s*["\']https?://', text, re.I)


def test_has_all_unified_dashboard_sections():
    document = parsed()
    assert {
        "overview",
        "opportunities",
        "cv-readiness",
        "contacts",
        "drafts",
        "funnel",
        "run-health",
    } <= document.sections
    text = source()
    for heading in (
        "Overview",
        "Opportunity inventory",
        "CV readiness",
        "Contacts",
        "Drafts",
        "Funnel",
        "Run health",
    ):
        assert heading in text


def test_uses_expected_read_and_patch_api_contract():
    text = source()
    for endpoint in (
        "/api/summary",
        "/api/opportunities",
        "/api/contacts",
        "/api/drafts",
        "/api/funnel",
    ):
        assert endpoint in text
    assert re.search(r"fetch\([^\n]+/api/opportunities/", text)
    assert re.search(r"fetch\([^\n]+/api/drafts/", text)
    assert text.count("method: 'PATCH'") >= 2
    assert "response.status === 409" in text


def test_never_builds_dom_from_untrusted_html():
    text = source()
    forbidden = (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "document.writeln",
        "eval(",
    )
    for token in forbidden:
        assert token not in text
    assert ".textContent" in text
    assert "document.createElement" in text


def test_has_no_inline_event_handlers():
    for _tag, attrs in parsed().start_tags:
        assert not any(name.lower().startswith("on") for name in attrs)
    assert not re.search(r'<[^>]+\son[a-z]+\s*=', source(), re.I)


def test_controls_do_not_offer_external_final_actions():
    forbidden = re.compile(r"\b(send|apply|connect|submit)\b", re.I)
    for control in parsed().controls:
        assert not forbidden.search(control["text"]), control
        assert not forbidden.search(control["attrs"].get("aria-label", "")), control
    text = source()
    assert "Draft-only" in text
    assert "final action remains manual" in text


def test_links_are_allowlisted_and_new_tabs_are_isolated():
    text = source()
    assert "function safeHref" in text
    assert "url.protocol === 'https:'" in text
    assert "function safePdfPath" in text
    assert "noopener noreferrer" in text
    assert "javascript:" not in text.lower()


def test_accessibility_filters_scores_and_explicit_metrics_are_present():
    text = source()
    assert 'href="#main-content"' in text
    assert ":focus-visible" in text
    assert "aria-live" in text
    assert "function normalizeScore" in text
    assert 'id="opportunity-search"' in text
    assert 'id="opportunity-status-filter"' in text
    assert 'id="opportunity-score-filter"' in text
    assert "summary.actionable" in text
    assert "summary.drafts_ready" in text
    assert "summary.contacts_verified" in text
    assert "messages.length" not in text


def test_drafts_require_explicit_tracking_change_and_show_warning():
    text = source()
    assert "Verify every recipient, claim, link, and attachment before manual use." in text
    assert "Draft status" in text
    assert "Select a new status to persist this tracking change." in text
    assert "saveDraftStatus" in text


def test_dashboard_uses_the_normalized_backend_state_contract():
    text = source()
    assert "['priority_score', 'score']" in text
    assert "discovered: ['verified_active', 'closed']" in text
    assert "shortlisted: ['eligible', 'user_applied', 'closed']" in text
    # user_applied is reachable from every open status (the user may apply on the real site at any time)
    for status in ("discovered", "verified_active", "eligible"):
        assert f"{status}: [" in text and "'user_applied'" in text.split(f"{status}: [", 1)[1].split("]", 1)[0]
    assert "approved_by_user: ['reviewed', 'sent_by_user', 'closed']" in text
    assert "payload.confirmed_by_user = true" in text
    for obsolete in ("'tracked'", "'reviewing'", "'used_manually'"):
        assert obsolete not in text


def test_dashboard_exposes_score_components_and_sanitizes_imported_emoji():
    text = source()
    for field in ("fit_score", "priority_score", "eligibility_status", "freshness_status", "verification_confidence"):
        assert field in text
    assert "Extended_Pictographic" in text
    assert "Minimum priority" in text
    assert "Confirm you applied manually" in text
    assert "confirmed_by_user" in text
    assert "clearTimeout(searchTimer)" in text
    assert "setTimeout(renderOpportunities" in text
    assert "number <= 10" not in text


def test_paste_intake_and_per_row_paste_action_exist():
    text = source()
    document = parsed()
    assert "Add job by paste" in text
    for field_id in ("paste-url", "paste-title", "paste-company", "paste-location", "paste-text"):
        assert f'id="{field_id}"' in text
    assert "/api/opportunities/paste" in text
    assert "version: 'new'" in text
    assert "Paste description" in text
    assert "DESCRIPTION_NEEDS_PASTE" in text
    for status in ("'blocked'", "'login_wall'", "'empty'"):
        assert status in text
    assert re.search(r"fetch\(`/api/opportunities/\$\{encodeURIComponent\(id\)\}/description`, \{method: 'POST'", text)
    button_texts = {control["text"] for control in document.controls if control["tag"] == "button"}
    assert "Save locally" in button_texts


def test_semantic_score_gaps_and_search_are_defensive():
    text = source()
    assert "<th>Semantic</th>" in text
    assert "function buildSemanticCell" in text
    assert "semantic_score" in text
    assert "semantic-fill" in text
    assert "Gaps: ${" in text
    assert "skills_have" in text and "skills_missing" in text
    assert re.search(r"fetch\(`/api/match/\$\{encodeURIComponent\(id\)\}`", text)
    assert "/api/search?q=" in text
    assert "state.features.search = false" in text
    assert "filtering by title, company, and location instead" in text
    for field in ("job_type", "is_remote", "salary"):
        assert field in text
    # every optional endpoint has an explicit 404 fallback
    assert text.count("response.status === 404") >= 6


def test_recruiter_improve_loop_is_confirmed_and_renders_rounds():
    text = source()
    assert "Improve with recruiter loop" in text
    assert "/api/recruiter/improve" in text
    assert re.search(r"async function runImproveLoop[\s\S]*?window\.confirm\(", text)
    assert "function renderImprovement" in text
    for label in ("'Round'", "'ATS before'", "'ATS after'", "'Edits'"):
        assert label in text
    assert "best_round" in text
    assert "addSafeLink(target, artifact, 'View PDF', true)" in text
    assert "jd_language" in text
    assert "language_mismatch" in text
    assert "Evidence citations" in text


def test_skill_gaps_page_and_overview_cards_exist():
    text = source()
    document = parsed()
    assert "skills" in document.sections
    assert 'href="#/skills"' in text and 'data-route="skills"' in text
    assert "skills: 'Skill gaps'" in text
    assert "/api/match/gaps" in text
    assert "/api/match/recompute" in text
    assert "JSON.stringify({all: true})" in text
    assert 'id="skills-recompute"' in text
    for card in ("Avg semantic fit", "Jobs with full description", "Blocked descriptions (need paste)", "Data sources"):
        assert card in text
    assert "function renderSources" in text
    assert "['source', 'source_name', 'origin']" in text


def test_new_controls_add_no_consequential_actions():
    forbidden = re.compile(r"\b(send|apply|connect|submit)\b", re.I)
    text = source()
    # every button label created in JS
    for label in re.findall(r"text\('button', '([^']+)'", text):
        assert not forbidden.search(label), label
    button_texts = {control["text"] for control in parsed().controls if control["tag"] == "button"}
    assert {"Save locally", "Recompute all"} <= button_texts
    for label in button_texts:
        assert not forbidden.search(label), label
    # paste form never submits natively
    assert 'novalidate' in text
    assert "event.preventDefault(); savePastedJob();" in text
    assert re.search(r'<form\b[^>]*\baction\s*=', text) is None


def test_inline_javascript_parses():
    node = shutil.which("node")
    assert node, "Node.js is required for the JavaScript syntax check"
    match = re.search(r"<script>([\s\S]*)</script>", source())
    assert match
    result = subprocess.run(
        [node, "--check", "-"],
        input=match.group(1),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_tracker_route_and_kanban_board_exist():
    text = source()
    document = parsed()
    assert "tracker" in document.sections
    assert "tracker: 'Tracker'" in text
    assert 'id="page-tracker"' in text and 'id="tracker-board"' in text and 'id="tracker-search"' in text
    assert "fetch('/api/tracker')" in text
    assert "fetch('/api/tracker/move'" in text
    assert "to_status: toStatus, version: card.version" in text
    assert "Refreshed, try again" in text
    assert "column_order" in text and "allowed_moves" in text
    assert "user_applied: ['closed']" in text and "closed: []" in text
    assert "'Update status'" in text


def test_detail_drawer_tabs_and_draft_only_labels():
    text = source()
    assert "window.openDrawer = function openDrawer" in text
    assert '<aside id="drawer" hidden' in text
    assert "event.key === 'Escape'" in text
    for label in ("'Description'", "'Match'", "'CV keywords'", "'Interview prep'", "'Cover letter'", "'Activity'"):
        assert label in text
    assert "Draft only, not sent" in text
    assert "Missing but evidenced (add these)" in text
    assert "cv_coverage_pct" in text
    assert "navigator.clipboard.writeText" in text
    assert "document.createElementNS" in text
    for endpoint in ("/api/interview/generate", "/api/cover-letters/generate", "/api/tracker/timeline/", "/highlight"):
        assert endpoint in text
    for label in ("'Generate prep'", "'Generate draft'", "'Copy draft'", "'Details'"):
        assert label in text
    forbidden = re.compile(r"\b(send|apply|connect|submit)\b", re.I)
    for label in re.findall(r"text\('button', '([^']+)'", text):
        assert not forbidden.search(label), label
    for control in parsed().controls:
        assert not forbidden.search(control["text"]), control


def test_visual_refresh_has_no_emoji_and_uses_svg_icons():
    text = source()
    assert not re.search(r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]", text)
    assert "\u2014" not in text and "\u2026" not in text
    assert text.count('stroke="currentColor"') >= 8
    assert 'href="#/tracker"' in text and 'data-route="tracker"' in text
    assert 'id="sidebar-toggle"' in text
    assert "--accent" in text and "--surface" in text and "--danger" in text


def test_pagination_toast_and_empty_state_utilities_exist():
    text = source()
    assert "function showToast" in text
    assert "function renderPager" in text
    assert 'id="opportunity-pager"' in text
    for size in ("[25, 50, 100]", "'Previous'", "'Next'", "of ${total}"):
        assert size in text
    assert "function emptyState" in text
    assert "function skeletonRows" in text
    assert "window.openDrawer" in text and "showToast('Details unavailable'" in text
    assert "/api/cvs/${encodeURIComponent(id)}/highlight" in text
    assert "cv_coverage_pct" in text and "No coverage yet" in text
    assert "/api/recruiter/improvements/" in text
    for kpi in ("Open jobs", "Applied by you", "With tailored CV", "Avg semantic fit", "Blocked descriptions"):
        assert kpi in text
    assert "Needs action" in text


def test_v3_tabs_widgets_and_confirmation_labels_exist():
    text = source()
    for label in ("AI score", "Apply prep", "Outreach", "submit yourself", "I sent this myself",
                  "Mark as applied by me", "I applied myself", "Mark as sent by me", "Copy draft",
                  "Due today", "Follow up", "Ready to submit", "Preview PDF", "Unavailable yet"):
        assert label in text, label
    assert "/api/opportunities/${encodeURIComponent(id)}/applied" in text or "/applied`" in text
    assert "/api/outreach/due" in text and "/api/applications/prepare" in text and "/api/llm-score/" in text
    # PDF preview is a server-rendered PNG of page 1 (no iframe/plugin, which CSP blocks and which hangs headless browsers)
    assert "/preview.png" in text and "/pdf`" in text and "createElement('iframe')" not in text


def test_cv_pdf_endpoint_rejects_path_escape(tmp_path):
    import sqlite3
    import sys
    sys.path.insert(0, str(HTML_PATH.parent))
    import pipeline_v2

    sys.path.insert(0, str(HTML_PATH.parent))
    from resume_matcher_fixtures import PortTestCase
    case = PortTestCase("run") if hasattr(PortTestCase, "run") else None
    db = tmp_path / "t.sqlite3"
    pipeline_v2.create_schema(db)
    case.db_path = db
    case.insert_opportunity(opportunity_id="opp_x")
    for bad in ("../secret.pdf", "C:/Windows/x.pdf", "cv_output/a.txt"):
        with sqlite3.connect(db) as conn:
            conn.execute("DELETE FROM cv_artifacts")
            conn.execute(
                "INSERT INTO cv_artifacts (id, opportunity_id, path, artifact_type) VALUES ('art_x', 'opp_x', ?, 'tailored')",
                (bad,),
            )
        with pytest.raises((pipeline_v2.ValidationError, pipeline_v2.NotFoundError)):
            pipeline_v2.cv_pdf_bytes(db, "opp_x", tmp_path)
    with pytest.raises(pipeline_v2.NotFoundError):
        pipeline_v2.cv_pdf_bytes(db, "opp_missing", tmp_path)


def test_applied_panel_payload_matches_endpoint_contract():
    """The /applied endpoint accepts only version, confirmed, applied_at, channel; an extra key
    made it answer 400 and the applied date was silently lost."""
    text = source()
    match = re.search(r"/applied`, \{method: 'POST'.*?body: JSON\.stringify\((\{.*?\})\)", text)
    assert match, "applied panel fetch not found"
    keys = set(re.findall(r"(\w+):", match.group(1)))
    assert keys <= {"version", "confirmed", "applied_at", "channel"}, keys
    assert "confirmed: true" in match.group(1)
