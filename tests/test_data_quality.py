"""Data quality: source names and duplicate listings.

Measured on the real database: 'linkedin' and 'LinkedIn' were counted as two
different sources (splitting 103 rows), and 10 groups of jobs were the same
vacancy re-posted under different URLs, which content_hash dedupe cannot catch.
"""
import unittest

import pipeline_v2


class SourceNormalizationTests(unittest.TestCase):
    def test_case_variants_collapse(self):
        self.assertEqual(pipeline_v2.normalize_source("LinkedIn"),
                         pipeline_v2.normalize_source("linkedin"))

    def test_whitespace_and_punctuation_collapse(self):
        for variant in (" LinkedIn ", "Linked In", "linked-in"):
            self.assertEqual(pipeline_v2.normalize_source(variant), "linkedin", variant)

    def test_known_sources_keep_a_readable_name(self):
        self.assertEqual(pipeline_v2.normalize_source("RemoteOK"), "remoteok")
        self.assertEqual(pipeline_v2.normalize_source("We Work Remotely"), "weworkremotely")

    def test_unknown_source_is_preserved_lowercased(self):
        self.assertEqual(pipeline_v2.normalize_source("Acme Board"), "acme board")

    def test_empty_is_unknown(self):
        for value in ("", None, "   "):
            self.assertEqual(pipeline_v2.normalize_source(value), "unknown")


class DuplicateKeyTests(unittest.TestCase):
    def test_same_role_same_company_matches_across_urls(self):
        a = pipeline_v2.duplicate_key("Data Engineer", "Acme", "Rabat")
        b = pipeline_v2.duplicate_key("data engineer", "ACME", "rabat")
        self.assertEqual(a, b)

    def test_seniority_noise_is_ignored(self):
        a = pipeline_v2.duplicate_key("Senior Data Engineer (H/F)", "Acme", "Rabat")
        b = pipeline_v2.duplicate_key("Senior Data Engineer", "Acme", "Rabat")
        self.assertEqual(a, b)

    def test_different_companies_stay_distinct(self):
        a = pipeline_v2.duplicate_key("Data Engineer", "Acme", "Rabat")
        b = pipeline_v2.duplicate_key("Data Engineer", "Globex", "Rabat")
        self.assertNotEqual(a, b)

    def test_different_roles_stay_distinct(self):
        a = pipeline_v2.duplicate_key("Data Engineer", "Acme", "Rabat")
        b = pipeline_v2.duplicate_key("ML Engineer", "Acme", "Rabat")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
