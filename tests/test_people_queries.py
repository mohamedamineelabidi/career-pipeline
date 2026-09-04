import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reach.people_queries import queries_for  # noqa: E402

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


class PeopleQueriesTest(unittest.TestCase):
    def test_returns_at_least_six_distinct_queries(self):
        qs = queries_for("OCP Group")
        self.assertGreaterEqual(len(qs), 6)
        self.assertEqual(len(qs), len({q.casefold() for q in qs}))
        for q in qs:
            self.assertIn("OCP Group", q)

    def test_covers_required_topics_in_fr_and_en(self):
        blob = " ".join(queries_for("Inwi")).lower()
        self.assertIn("inwi maroc", blob)
        self.assertIn("talent acquisition", blob)
        self.assertIn("recrut", blob)
        self.assertIn("casablanca", blob)
        self.assertIn("rabat", blob)
        self.assertIn("pfe", blob)
        self.assertIn("stage", blob)
        self.assertIn("ensah", blob)
        self.assertIn("recruiter", blob)  # EN
        self.assertIn("recruteur", blob)  # FR

    def test_never_contains_at_sign_or_email_pattern(self):
        for company in ("Inwi", "OCP", "user@evil.com", "x@y"):
            for q in queries_for(company):
                self.assertNotIn("@", q)
                self.assertIsNone(EMAIL_RE.search(q))

    def test_intent_filters_but_keeps_minimum(self):
        internship = queries_for("Inwi", "internship")
        job = queries_for("Inwi", "job")
        self.assertGreaterEqual(len(internship), 6)
        self.assertGreaterEqual(len(job), 6)
        self.assertTrue(any("pfe" in q.lower() for q in internship))
        self.assertFalse(any("hiring ai engineer" in q.lower() for q in internship))

    def test_rejects_blank_company_and_unknown_intent(self):
        with self.assertRaises(ValueError):
            queries_for("")
        with self.assertRaises(ValueError):
            queries_for("Inwi", "spam")


if __name__ == "__main__":
    unittest.main()
