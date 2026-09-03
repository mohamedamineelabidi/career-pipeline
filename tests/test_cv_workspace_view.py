"""The CV workspace should open on the CVs.

The Document filter defaulted to "All", so the page rendered 292 cards when only
83 opportunities actually had a tailored document. The other 209 were cards with
nothing to show. Opening on "Has CV" makes the default view the useful one, and
the filter still reaches everything else.
"""
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HTML = PROJECT_ROOT / "pipeline_v2.html"


class CvWorkspaceViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text(encoding="utf-8")

    def test_document_filter_defaults_to_has_cv(self):
        match = re.search(r'<select id="cv-has-filter">(.*?)</select>', self.html, re.S)
        self.assertIsNotNone(match, "cv-has-filter select not found")
        options = match.group(1)
        self.assertRegex(
            options,
            r'<option value="yes"[^>]*\bselected\b',
            "the 'Has CV' option should be selected by default",
        )

    def test_all_option_still_reachable(self):
        """Defaulting must not remove the escape hatch to the full list."""
        match = re.search(r'<select id="cv-has-filter">(.*?)</select>', self.html, re.S)
        self.assertIn('value=""', match.group(1))
        self.assertIn('value="no"', match.group(1))

    def test_count_is_reported(self):
        self.assertIn("cv-count", self.html)


if __name__ == "__main__":
    unittest.main()
