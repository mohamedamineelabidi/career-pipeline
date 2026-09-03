"""A job's source should be recovered from its URL, not left as "unknown".

Measured on the real database: 20 jobs carried source='unknown' while their URL
was plainly a linkedin.com job link. That understates where the pipeline actually
finds work and scatters one board across two labels in every per-source view.
"""
import unittest

import pipeline_v2


class SourceFromUrlTests(unittest.TestCase):
    def test_linkedin_url_recovers_the_board(self):
        self.assertEqual(
            pipeline_v2.source_from_url("https://www.linkedin.com/jobs/view/4457374550/"),
            "linkedin",
        )

    def test_known_boards_are_recognised(self):
        cases = {
            "https://weworkremotely.com/remote-jobs/x": "weworkremotely",
            "https://remoteok.com/remote-jobs/123": "remoteok",
            "https://www.welcometothejungle.com/fr/companies/x/jobs/y": "welcometothejungle",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(pipeline_v2.source_from_url(url), expected)

    def test_company_page_is_not_guessed(self):
        """An unrecognised host must stay unknown rather than invent a board."""
        self.assertIsNone(pipeline_v2.source_from_url("https://careers.acme.com/jobs/7"))

    def test_missing_or_junk_url_is_safe(self):
        for url in (None, "", "not a url", "ftp://x"):
            with self.subTest(url=url):
                self.assertIsNone(pipeline_v2.source_from_url(url))

    def test_recovered_source_matches_normalisation(self):
        """What we recover must equal what normalize_source would produce."""
        recovered = pipeline_v2.source_from_url("https://LinkedIn.com/jobs/view/1")
        self.assertEqual(recovered, pipeline_v2.normalize_source("LinkedIn"))


if __name__ == "__main__":
    unittest.main()
