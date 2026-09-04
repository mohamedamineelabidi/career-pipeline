"""The Exa search channel (via mcporter, ported from Agent Reach) parses results
into the shape reach.people_discovery expects, and never spends a live call in tests."""
import json
import unittest
from unittest import mock

from reach import search_channel


SAMPLE = """Title: Hajar Ghzala
URL: https://www.linkedin.com/in/hajar-ghzala-94143315b
Published: 2026-08-22T15:36:15.000Z
Author: N/A
Highlights:
Talent Acquisition Specialist - Deloitte
...
Casablanca, Casablanca-Settat, Morocco (MA)
...
### Talent Acquisition Specialist - [Deloitte](https://www.linkedin.com/company/deloitte) (Current)

Title: Deloitte Maroc | Carrières
URL: https://www2.deloitte.com/ma/fr/careers.html
Published: N/A
Author: N/A
Highlights:
Rejoignez Deloitte au Maroc. Stages PFE et postes juniors.
"""


class ParseTests(unittest.TestCase):
    def test_parses_blocks_into_url_title_snippet(self):
        results = search_channel.parse_exa_text(SAMPLE)
        self.assertEqual(len(results), 2)
        first = results[0]
        self.assertEqual(first["url"], "https://www.linkedin.com/in/hajar-ghzala-94143315b")
        self.assertEqual(first["title"], "Hajar Ghzala")
        self.assertIn("Talent Acquisition Specialist - Deloitte", first["snippet"])
        self.assertIn("Casablanca", first["snippet"])
        self.assertEqual(first["published"], "2026-08-22T15:36:15.000Z")
        self.assertNotIn("...", first["snippet"])
        self.assertEqual(results[1]["url"], "https://www2.deloitte.com/ma/fr/careers.html")

    def test_empty_text_gives_no_results(self):
        self.assertEqual(search_channel.parse_exa_text(""), [])
        self.assertEqual(search_channel.parse_exa_text("Error: something"), [])


class SearchTests(unittest.TestCase):
    def test_search_calls_mcporter_with_json_args_and_people_category(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout=SAMPLE, stderr="")

        with mock.patch.object(search_channel.subprocess, "run", fake_run):
            results = search_channel.exa_search("Deloitte Maroc recruiter", category="people", num_results=5)
        self.assertEqual(len(results), 2)
        cmd = calls[0]
        self.assertIn("mcporter", cmd[0])
        self.assertIn("web_search_exa", cmd)
        args = json.loads(cmd[cmd.index("--args") + 1])
        self.assertTrue(args["query"].startswith("category:people "))
        self.assertEqual(args["numResults"], 5)

    def test_search_failure_returns_empty_and_records_reason(self):
        def fake_run(cmd, **kwargs):
            return mock.Mock(returncode=1, stdout="", stderr="connection refused")

        with mock.patch.object(search_channel.subprocess, "run", fake_run):
            results = search_channel.exa_search("x")
        self.assertEqual(results, [])
        self.assertIn("connection refused", search_channel.last_error())

    def test_search_never_sends_email_pattern_guesses(self):
        with self.assertRaises(ValueError):
            search_channel.exa_search("Deloitte @deloitte.com firstname.lastname")

    def test_available_reports_missing_mcporter(self):
        with mock.patch.object(search_channel.shutil, "which", return_value=None):
            self.assertFalse(search_channel.available())


class CleanSnippetTests(unittest.TestCase):
    def test_snippet_drops_markdown_markup_and_repeats(self):
        raw = ("Senior HR Business Partner chez Deloitte Senior HR Business Partner chez Deloitte "
               "### Senior HR Business Partner - [Deloitte](https://www.linkedin.com/company/deloitte) (Current) "
               "Sep 2022 - Present (3 years and 11 months) in Casablanca, Casablanca-Settat, Maroc")
        cleaned = search_channel.clean_snippet(raw)
        self.assertNotIn("###", cleaned)
        self.assertNotIn("](http", cleaned)
        self.assertNotIn("[", cleaned)
        self.assertEqual(cleaned.count("Senior HR Business Partner chez Deloitte"), 1)
        self.assertIn("Casablanca", cleaned)

    def test_parse_exposes_a_clean_headline(self):
        results = search_channel.parse_exa_text(SAMPLE)
        self.assertEqual(results[0]["headline"], "Talent Acquisition Specialist - Deloitte")
        self.assertNotIn("###", results[0]["snippet"])


if __name__ == "__main__":
    unittest.main()
