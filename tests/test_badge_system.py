"""The dashboard's shared visual vocabulary, enforced.

Metrics were competing for attention: a row showed `AI 60`, `Priority 95`,
`69 / Gaps: 3` and `Eligible` as four separate text nodes. One badge helper with
a fixed tone vocabulary keeps meaning in the colour rather than in decoration.

The CDN test is the important one. This app is offline-first: deliverables open
over file:// and the whole UI is one dependency-free file. A charting library or
a webfont pulled from a CDN would break that quietly, so it fails the build.
"""
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HTML = PROJECT_ROOT / "pipeline_v2.html"


class BadgeSystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text(encoding="utf-8")

    def test_badge_helper_exists(self):
        self.assertIn("function badge(", self.html)

    def test_badge_tones_are_defined(self):
        for tone in ("badge-good", "badge-warn", "badge-neutral"):
            with self.subTest(tone=tone):
                self.assertIn(tone, self.html)

    def test_typography_and_elevation_tokens_exist(self):
        for token in ("--font:", "--muted-strong:", "--elev-1:", "--elev-2:"):
            with self.subTest(token=token):
                self.assertIn(token, self.html)

    def test_no_cdn_script_or_stylesheet(self):
        """Offline-first is a product guarantee, not a preference."""
        self.assertIsNone(re.search(r'<script[^>]+src="https?://', self.html))
        self.assertIsNone(re.search(r'<link[^>]+href="https?://', self.html))

    def test_no_webfont_import(self):
        self.assertNotIn("@import url(", self.html)
        self.assertNotIn("fonts.googleapis", self.html)

    def test_badges_use_textcontent_not_innerhtml(self):
        """Badge labels come from job data; innerHTML would be an injection path."""
        match = re.search(r"function badge\([^)]*\)\s*\{(.*?)\n    \}", self.html, re.S)
        self.assertIsNotNone(match, "badge() not found in expected shape")
        self.assertNotIn("innerHTML", match.group(1))


if __name__ == "__main__":
    unittest.main()
