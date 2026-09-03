"""The hot queries must use indexes, not full table scans.

Measured on the real database: `WHERE status=... ORDER BY priority_score DESC`
(run on every triage keypress) and `WHERE source=...` both produced
`SCAN opportunities`. Invisible at 295 rows, painful at 5,000.
"""
import tempfile
from contextlib import closing
import unittest
from pathlib import Path

import pipeline_v2


class OpportunityIndexTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.db = Path(self._dir.name) / "pipeline.sqlite3"
        pipeline_v2.create_schema(self.db)

    def _plan(self, sql):
        with closing(pipeline_v2.connect(self.db)) as connection:
            return " ".join(row[3] for row in connection.execute(
                "EXPLAIN QUERY PLAN " + sql))

    def test_triage_query_uses_an_index(self):
        plan = self._plan(
            "SELECT * FROM opportunities WHERE status='verified_active' "
            "ORDER BY priority_score DESC"
        )
        self.assertNotIn("SCAN opportunities", plan, plan)

    def test_source_filter_uses_an_index(self):
        plan = self._plan("SELECT * FROM opportunities WHERE source='linkedin'")
        self.assertNotIn("SCAN opportunities", plan, plan)

    def test_indexes_exist_by_name(self):
        with closing(pipeline_v2.connect(self.db)) as connection:
            names = {row[1] for row in connection.execute(
                "PRAGMA index_list(opportunities)")}
        for expected in ("opportunities_status_priority", "opportunities_source"):
            self.assertIn(expected, names)

    def test_schema_is_reapplied_safely(self):
        """create_schema runs on every start; adding indexes must stay idempotent."""
        pipeline_v2.create_schema(self.db)
        pipeline_v2.create_schema(self.db)


if __name__ == "__main__":
    unittest.main()
