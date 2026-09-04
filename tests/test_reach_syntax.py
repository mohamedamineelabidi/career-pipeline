"""Static gate for reach.html: JS parses, and the hard frontend rules hold.

The rules mirror reach/DESIGN.md: textContent only (no innerHTML), no inline
event handlers, no external requests (the page works offline), and no
typographic dashes that hint at generated text.
"""
import re
import os
import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HTML = PROJECT_ROOT / "reach.html"


def scratch_dir():
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "Temp" if base else Path(os.environ.get("TMPDIR", "/tmp"))
    root.mkdir(parents=True, exist_ok=True)
    return root


class ReachSyntaxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = HTML.read_text(encoding="utf-8")

    def test_inline_scripts_parse(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed; syntax gate skipped")
        blocks = re.findall(r"<script>(.*?)</script>", self.text, re.S)
        self.assertTrue(blocks, "no inline <script> block found")
        for index, block in enumerate(blocks):
            path = scratch_dir() / f"reach_block_{index}_{os.getpid()}.js"
            path.write_text(block, encoding="utf-8")
            try:
                result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=60)
            finally:
                path.unlink(missing_ok=True)
            self.assertEqual(result.returncode, 0, f"script block {index} has a syntax error:\n{result.stderr}")

    def test_no_innerhtml(self):
        self.assertNotIn("innerHTML", self.text)
        self.assertNotIn("outerHTML", self.text)
        self.assertNotIn("insertAdjacentHTML", self.text)

    def test_no_inline_event_handlers(self):
        self.assertEqual(re.findall(r"\son[a-z]+=", self.text, re.I), [])

    def test_no_external_requests(self):
        for match in re.finditer(r"https?://[^\s\"'<>)]+", self.text):
            url = match.group(0)
            allowed = "linkedin.com/in" in url or url.startswith("http://127.0.0.1")
            self.assertTrue(allowed, f"external URL in reach.html: {url}")
        self.assertNotIn("<link", self.text.lower().replace("<link rel=\"icon\"", ""), "no external stylesheets or fonts")
        self.assertNotIn("@import", self.text)
        self.assertNotIn("<script src", self.text)

    def test_no_em_dash_or_emoji(self):
        self.assertNotIn("\u2014", self.text)
        self.assertEqual(re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", self.text), [])

    def test_no_forbidden_control_words(self):
        for word in ("send email", "send draft", "apply now", "submit application", "connect on linkedin", "send message"):
            self.assertNotIn(word, self.text.lower())


if __name__ == "__main__":
    unittest.main()
