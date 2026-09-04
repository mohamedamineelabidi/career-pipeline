import json
import unittest
from pathlib import Path

QUERIES_PATH = Path(__file__).resolve().parents[1] / "reach" / "queries_morocco.json"
LOCATIONS = {"Casablanca", "Rabat", "Morocco", "Maroc"}
ROLE_KINDS = {"internship", "job"}
ROLE_FAMILIES = {"ai_engineer", "data_engineer", "ml_engineer", "cloud_engineer", "mlops", "data_scientist"}


class RadarQueriesConfigTests(unittest.TestCase):
    def load(self):
        return json.loads(QUERIES_PATH.read_text(encoding="utf-8"))

    def test_config_is_a_nonempty_list_of_valid_entries(self):
        queries = self.load()
        self.assertIsInstance(queries, list)
        self.assertGreaterEqual(len(queries), 16)
        for entry in queries:
            self.assertEqual(set(entry), {"keywords", "location", "role_kind", "role_family"}, entry)
            self.assertIsInstance(entry["keywords"], str)
            self.assertTrue(entry["keywords"].strip(), entry)
            self.assertIn(entry["location"], LOCATIONS, entry)
            self.assertIn(entry["role_kind"], ROLE_KINDS, entry)
            self.assertIn(entry["role_family"], ROLE_FAMILIES, entry)

    def test_covers_internships_and_jobs_and_families(self):
        queries = self.load()
        internships = [q for q in queries if q["role_kind"] == "internship"]
        jobs = [q for q in queries if q["role_kind"] == "job"]
        self.assertGreaterEqual(len(internships), 8)
        self.assertGreaterEqual(len(jobs), 8)
        self.assertEqual({q["role_family"] for q in queries}, ROLE_FAMILIES)
        self.assertTrue({"Casablanca", "Rabat", "Morocco"} <= {q["location"] for q in queries})
        keywords = " | ".join(q["keywords"].casefold() for q in internships)
        for french in ("stage ingénieur ia", "stagiaire data engineer", "stage pfe data", "stage machine learning"):
            self.assertIn(french, keywords)

    def test_no_duplicate_entries(self):
        queries = self.load()
        keys = [(q["keywords"].casefold(), q["location"], q["role_kind"]) for q in queries]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
