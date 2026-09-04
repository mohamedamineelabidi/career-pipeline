"""Target companies are seeded once and grow from the pipeline's own evidence.

`seed_targets` takes a hand-written list; `derive_targets_from_opportunities`
promotes employers behind eligible/shortlisted Moroccan listings. Both must be
safe to re-run: the Reach loop calls them on every cycle.
"""
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import migrate_pipeline_v2
import pipeline_v2
from reach import targets


class ReachTargetTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.db = Path(self._dir.name) / "pipeline.sqlite3"
        pipeline_v2.create_schema(self.db)
        migrate_pipeline_v2.ensure_reach_schema(self.db)
        self.conn = pipeline_v2.connect(self.db)
        self.addCleanup(self.conn.close)

    def _insert_opportunity(self, **kw):
        fields = {
            "id": "opp_1", "title": "Data Engineer", "company": "Acme",
            "location": "Casablanca, Morocco",
            "role_kind": "exact_vacancy", "url": "https://example.com/job",
            "source": "test", "status": "eligible", "freshness_status": "recent",
            "source_verification_status": "description_fetched",
            "eligibility_status": "eligible", "verification_confidence": 85,
            "description": "x" * 300, "fit_score": 70, "priority_score": 70,
            "match_score": 70, "score_schema_version": 2,
            "score_breakdown_json": "{}", "source_json": "{}",
            "created_at": "2026-09-01T00:00:00+00:00",
            "updated_at": "2026-09-01T00:00:00+00:00",
        }
        fields.update(kw)
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        self.conn.execute(f"INSERT INTO opportunities ({cols}) VALUES ({marks})",
                          tuple(fields.values()))
        self.conn.commit()

    def _targets(self):
        return {row["name"]: dict(row) for row in self.conn.execute(
            "SELECT * FROM target_companies")}

    def test_seed_targets_inserts_and_returns_count(self):
        inserted = targets.seed_targets(self.conn, ["OCP", "Attijariwafa bank"],
                                        intent="internship")
        self.assertEqual(inserted, 2)
        rows = self._targets()
        self.assertEqual(set(rows), {"OCP", "Attijariwafa bank"})
        for row in rows.values():
            self.assertTrue(row["id"].startswith("tgt_"))
            self.assertEqual(len(row["id"]), len("tgt_") + 32)
            self.assertEqual(row["intent"], "internship")
            self.assertEqual(row["aliases_json"], "[]")
            self.assertEqual(row["priority"], 50)
            self.assertTrue(row["created_at"])
            self.assertEqual(row["created_at"], row["updated_at"])

    def test_seed_targets_is_idempotent_and_skips_blanks(self):
        targets.seed_targets(self.conn, ["OCP"])
        inserted = targets.seed_targets(self.conn, ["OCP", "  ", "", "Inwi"])
        self.assertEqual(inserted, 1)
        self.assertEqual(set(self._targets()), {"OCP", "Inwi"})

    def test_derive_targets_picks_eligible_or_shortlisted_moroccan_employers(self):
        self._insert_opportunity(id="o1", company="OCP", location="Casablanca")
        self._insert_opportunity(id="o2", company="Maroc Telecom", location="Rabat, Maroc",
                                 status="shortlisted")
        self._insert_opportunity(id="o3", company="Inwi", location="Morocco (Remote)",
                                 status="eligible")
        # wrong status
        self._insert_opportunity(id="o4", company="Ignored Co", location="Casablanca",
                                 status="discovered")
        # wrong location
        self._insert_opportunity(id="o5", company="Paris Co", location="Paris, France")
        added = targets.derive_targets_from_opportunities(self.conn)
        self.assertEqual(added, 3)
        self.assertEqual(set(self._targets()), {"OCP", "Maroc Telecom", "Inwi"})

    def test_derive_targets_is_idempotent_and_respects_seeded_rows(self):
        targets.seed_targets(self.conn, ["OCP"], intent="internship")
        self._insert_opportunity(id="o1", company="OCP", location="Casablanca")
        self._insert_opportunity(id="o2", company="OCP", location="Rabat")
        self._insert_opportunity(id="o3", company="Inwi", location="Morocco")
        self.assertEqual(targets.derive_targets_from_opportunities(self.conn), 1)
        self.assertEqual(targets.derive_targets_from_opportunities(self.conn), 0)
        rows = self._targets()
        self.assertEqual(set(rows), {"OCP", "Inwi"})
        self.assertEqual(rows["OCP"]["intent"], "internship")  # not overwritten
        self.assertEqual(rows["Inwi"]["intent"], "any")


if __name__ == "__main__":
    unittest.main()
