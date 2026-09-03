"""Drafts are read one at a time, so the page should be built that way.

A three-column card grid forces 29 cold emails and cover letters into narrow
columns, which is the wrong shape for reading prose. This is the email-client
layout: a list rail on the left, the full draft on the right.

The safety assertions matter more than the layout ones. Copying a draft is not
evidence it was sent, so nothing here may auto-advance a draft to "sent", and no
control may submit anything on the user's behalf.
"""
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HTML = PROJECT_ROOT / "pipeline_v2.html"


class DraftsLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text(encoding="utf-8")

    def test_master_detail_containers_exist(self):
        for node_id in ('draft-list', 'draft-detail'):
            with self.subTest(node_id=node_id):
                self.assertIn(f'id="{node_id}"', self.html)

    def test_list_and_detail_renderers_exist(self):
        self.assertIn("function renderDrafts(", self.html)
        self.assertIn("function renderDraftDetail(", self.html)

    def test_placeholders_are_highlighted(self):
        """Unfilled {Company} must be visible before the text is copied."""
        self.assertIn("highlightPlaceholders", self.html)

    def test_copy_does_not_auto_mark_sent(self):
        """Copying is not sending. Auto-advancing would write a false 'sent' state."""
        match = re.search(r"function copyDraftBody\(.*?\n    \}", self.html, re.S)
        self.assertIsNotNone(match, "copyDraftBody() not found")
        body = match.group(0)
        self.assertNotIn("sent_by_user", body)

    def test_no_send_controls_on_drafts_page(self):
        page = self.html[self.html.index('id="page-drafts"'):]
        page = page[: page.index('<div class="page"', 1)] if '<div class="page"' in page[1:] else page
        for word in ("Send draft", "Send email", "Submit application"):
            with self.subTest(word=word):
                self.assertNotIn(word, page)

    def test_placeholder_highlighting_uses_textcontent(self):
        match = re.search(r"function highlightPlaceholders\(.*?\n    \}", self.html, re.S)
        self.assertIsNotNone(match)
        self.assertNotIn("innerHTML", match.group(0))


if __name__ == "__main__":
    unittest.main()
