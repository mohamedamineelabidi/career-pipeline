"""The dashboard is one HTML file, so a JS syntax error takes the whole app down.

A broken regex once left every page silently blank: no console error, no failing
assertion, just zero rows. `node --check` catches that class of damage in under a
second. Skips cleanly when node is unavailable rather than failing the suite.
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HTML = PROJECT_ROOT / "pipeline_v2.html"


class DashboardSyntaxTests(unittest.TestCase):
    def test_inline_script_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed; syntax gate skipped")

        blocks = re.findall(r"<script>(.*?)</script>", HTML.read_text(encoding="utf-8"), re.S)
        self.assertTrue(blocks, "no inline <script> block found")

        with tempfile.TemporaryDirectory() as tmp:
            for index, block in enumerate(blocks):
                path = Path(tmp) / f"block_{index}.js"
                path.write_text(block, encoding="utf-8")
                result = subprocess.run(
                    [node, "--check", str(path)],
                    capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(
                    result.returncode, 0,
                    f"script block {index} has a syntax error:\n{result.stderr}",
                )


if __name__ == "__main__":
    unittest.main()
