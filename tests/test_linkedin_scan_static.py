import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "reach" / "linkedin_people_scan.py"


class LinkedinScanStaticTest(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_never_touches_messaging_or_composers(self):
        for forbidden in ("/messaging", "compose", "invite", ".click("):
            self.assertNotIn(forbidden, self.text, forbidden)

    def test_stop_words_present(self):
        self.assertIn("checkpoint", self.text)
        self.assertIn("captcha", self.text)
        self.assertIn("authwall", self.text)
        self.assertIn("uas/login", self.text)

    def test_only_people_search_url_is_used(self):
        self.assertIn("linkedin.com/search/results/people/?keywords=", self.text)
        self.assertNotIn("/mynetwork", self.text)

    def test_prints_usage_without_harness(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--target", "Inwi"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("usage", proc.stdout.lower() + proc.stderr.lower())
        self.assertIn("browser helpers", proc.stdout.lower())


if __name__ == "__main__":
    unittest.main()
