"""Insights should show shapes, not lists of numbers.

The funnel read as text lines like "168/289 (58.1%)", and eight weeks of "0 / 0 /
0 / 0" is not parseable at a glance. These are hand-rolled inline SVG: the app is
offline-first, so a charting library from a CDN is not an option.
"""
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HTML = PROJECT_ROOT / "pipeline_v2.html"


class InsightsChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text(encoding="utf-8")

    def test_chart_builders_exist(self):
        for name in ("funnelChart", "weeklyHeatmap", "correlationPlot"):
            with self.subTest(name=name):
                self.assertIn(f"function {name}(", self.html)

    def test_charts_are_hand_rolled_svg(self):
        """No CDN charting library: this app must work from file:// with no network."""
        self.assertIn("createElementNS", self.html)
        for banned in ("recharts", "chart.js", "chartjs", "d3.min.js", "cdn.jsdelivr", "unpkg.com"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, self.html.lower())

    def test_no_external_script_or_style(self):
        self.assertFalse(re.search(r"<script[^>]+\bsrc=", self.html, re.I))
        self.assertFalse(re.search(r'<link[^>]+href="https?://', self.html, re.I))

    def test_charts_carry_accessible_text(self):
        """A chart with no text alternative is unreadable to a screen reader."""
        frame = re.search(r"function chartFrame\(.*?\n    \}", self.html, re.S)
        self.assertIsNotNone(frame, "chartFrame() not found")
        self.assertIn("role", frame.group(0))
        self.assertIn("aria-label", frame.group(0))
        # Every chart must route through the accessible frame.
        for name in ("funnelChart", "weeklyHeatmap", "correlationPlot"):
            match = re.search(rf"function {name}\(.*?\n    \}}", self.html, re.S)
            with self.subTest(name=name):
                self.assertIn("chartFrame(", match.group(0))

    def test_charts_avoid_innerhtml(self):
        for name in ("funnelChart", "weeklyHeatmap", "correlationPlot"):
            match = re.search(rf"function {name}\(.*?\n    \}}", self.html, re.S)
            self.assertIsNotNone(match, f"{name} not found")
            with self.subTest(name=name):
                self.assertNotIn("innerHTML", match.group(0))


if __name__ == "__main__":
    unittest.main()
