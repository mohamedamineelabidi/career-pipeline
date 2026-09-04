"""Reach system tables must exist after the migration step runs on any DB.

`migrate_pipeline_v2.ensure_reach_schema` is what `serve`/`migrate` call, so
existing databases pick the tables up on next start without a manual step.
"""
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import migrate_pipeline_v2
import pipeline_v2


class ReachSchemaTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.db = Path(self._dir.name) / "pipeline.sqlite3"
        pipeline_v2.create_schema(self.db)
        migrate_pipeline_v2.ensure_reach_schema(self.db)

    def _columns(self, table):
        with closing(pipeline_v2.connect(self.db)) as connection:
            return {row[1]: row for row in connection.execute(f"PRAGMA table_info({table})")}

    def test_target_companies_table_has_expected_columns(self):
        columns = self._columns("target_companies")
        self.assertEqual(
            set(columns),
            {"id", "name", "aliases_json", "sector", "country", "intent",
             "priority", "notes", "created_at", "updated_at"},
        )
        self.assertEqual(columns["id"][5], 1)  # primary key
        self.assertEqual(columns["aliases_json"][4], "'[]'")
        self.assertEqual(columns["intent"][4], "'any'")
        self.assertEqual(columns["priority"][4], "50")

    def test_target_companies_name_is_unique_and_intent_is_checked(self):
        with closing(pipeline_v2.connect(self.db)) as connection:
            connection.execute(
                "INSERT INTO target_companies (id, name) VALUES ('tgt_1', 'OCP')")
            with self.assertRaises(Exception):
                connection.execute(
                    "INSERT INTO target_companies (id, name) VALUES ('tgt_2', 'OCP')")
            with self.assertRaises(Exception):
                connection.execute(
                    "INSERT INTO target_companies (id, name, intent) "
                    "VALUES ('tgt_3', 'X', 'bogus')")

    def test_ensure_reach_schema_is_idempotent(self):
        migrate_pipeline_v2.ensure_reach_schema(self.db)  # second run must not raise
        self.assertTrue(self._columns("target_companies"))

    def test_people_candidates_table_has_expected_columns(self):
        columns = self._columns("people_candidates")
        self.assertEqual(
            set(columns),
            {"id", "target_company_id", "name", "headline", "company_seen", "role_seen",
             "profile_url", "email", "evidence_url", "evidence_quote", "discovered_via",
             "score", "verification_status", "current_role_confirmed_at",
             "promoted_contact_id", "created_at", "updated_at",
             "email_status", "email_evidence_url", "email_checked_at"},
        )
        self.assertEqual(columns["id"][5], 1)
        self.assertEqual(columns["name"][3], 1)  # NOT NULL
        self.assertEqual(columns["score"][4], "0")
        self.assertEqual(columns["verification_status"][4], "'unverified'")

    def test_people_candidates_references_target_companies(self):
        with closing(pipeline_v2.connect(self.db)) as connection:
            fks = [row[2] for row in connection.execute(
                "PRAGMA foreign_key_list(people_candidates)")]
            self.assertEqual(fks, ["target_companies"])
            with self.assertRaises(Exception):  # FK enforced via PRAGMA foreign_keys=ON
                connection.execute(
                    "INSERT INTO people_candidates (id, target_company_id, name) "
                    "VALUES ('pc_1', 'tgt_missing', 'Jane')")

    def test_people_candidates_profile_url_unique_except_blank(self):
        with closing(pipeline_v2.connect(self.db)) as connection:
            connection.execute(
                "INSERT INTO people_candidates (id, name, profile_url) "
                "VALUES ('pc_1', 'A', 'https://linkedin.com/in/a')")
            with self.assertRaises(Exception):
                connection.execute(
                    "INSERT INTO people_candidates (id, name, profile_url) "
                    "VALUES ('pc_2', 'B', 'https://linkedin.com/in/a')")
            # blank and NULL URLs may repeat freely
            connection.execute(
                "INSERT INTO people_candidates (id, name, profile_url) VALUES ('pc_3', 'C', '')")
            connection.execute(
                "INSERT INTO people_candidates (id, name, profile_url) VALUES ('pc_4', 'D', '')")
            connection.execute(
                "INSERT INTO people_candidates (id, name) VALUES ('pc_5', 'E')")
            connection.execute(
                "INSERT INTO people_candidates (id, name) VALUES ('pc_6', 'F')")

    def test_people_candidates_has_email_evidence_columns(self):
        columns = self._columns("people_candidates")
        self.assertTrue({"email_status", "email_evidence_url", "email_checked_at"} <= set(columns))
        self.assertEqual(columns["email_status"][4], "'none'")
        with closing(pipeline_v2.connect(self.db)) as connection:
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='people_candidates'").fetchone()[0]
            self.assertIn("email_status", sql)
            with self.assertRaises(Exception):  # CHECK constraint on the status word
                connection.execute(
                    "INSERT INTO people_candidates (id, name, email_status) VALUES ('pc_9', 'Z', 'bogus')")
        migrate_pipeline_v2.ensure_reach_schema(self.db)  # idempotent with the new columns


if __name__ == "__main__":
    unittest.main()
