import unittest

import application_tracker
import pipeline_v2
from pipeline_v2 import ConflictError, NotFoundError, ValidationError
from resume_matcher_fixtures import PortTestCase


class ApplicationTrackerTests(PortTestCase):
    def test_board_columns_and_cards(self):
        opp_id, version = self.insert_opportunity(status="eligible")
        other, _ = self.insert_opportunity(opportunity_id="opp_" + "b" * 24, status="discovered")
        self.insert_artifact(opp_id, "cv text")
        board = application_tracker.board(self.db_path)
        self.assertEqual(board["column_order"][0], "discovered")
        self.assertEqual(board["counts"]["eligible"], 1)
        self.assertEqual(board["counts"]["discovered"], 1)
        card = board["columns"]["eligible"][0]
        for key in ("id", "title", "company", "semantic_score", "priority", "has_cv", "last_update", "next_action"):
            self.assertIn(key, card)
        self.assertTrue(card["has_cv"])
        self.assertEqual(card["version"], version)
        self.assertIn("shortlisted", card["allowed_moves"])
        self.assertFalse(board["columns"]["discovered"][0]["has_cv"])
        self.assertIn("Verify", board["columns"]["discovered"][0]["next_action"])
        self.assertIn("Generate", card["next_action"])

    def test_move_reuses_transition_rules(self):
        opp_id, version = self.insert_opportunity(status="eligible")
        with self.assertRaises(ConflictError):
            application_tracker.move(self.db_path, {"opportunity_id": opp_id, "to_status": "shortlisted", "version": "stale"})
        with self.assertRaises(ValidationError):
            application_tracker.move(self.db_path, {"opportunity_id": opp_id, "to_status": "user_applied", "version": version})
        with self.assertRaises(ValidationError):
            application_tracker.move(self.db_path, {"opportunity_id": opp_id, "to_status": "bogus", "version": version})
        result = application_tracker.move(self.db_path, {"opportunity_id": opp_id, "to_status": "shortlisted", "version": version})
        self.assertEqual(result["moved_to"], "shortlisted")
        self.assertNotEqual(result["version"], version)
        # user_applied needs explicit confirmation.
        with self.assertRaises(ValidationError):
            application_tracker.move(self.db_path, {"opportunity_id": opp_id, "to_status": "user_applied", "version": result["version"]})
        result = application_tracker.move(self.db_path, {
            "opportunity_id": opp_id, "to_status": "user_applied", "version": result["version"], "confirmed_by_user": True})
        self.assertEqual(result["moved_to"], "user_applied")
        timeline = application_tracker.timeline(self.db_path, opp_id)
        kinds = [e["kind"] for e in timeline["events"]]
        self.assertEqual(kinds[0], "created")
        self.assertEqual(kinds.count("status_change"), 2)
        self.assertIn("application", kinds)
        with self.assertRaises(NotFoundError):
            application_tracker.timeline(self.db_path, "opp_" + "x" * 24)

    def test_http_endpoints(self):
        opp_id, version = self.insert_opportunity(status="eligible")
        self.start_server()
        status, body = self.request("/api/tracker")
        self.assertEqual(status, 200)
        self.assertEqual(body["counts"]["eligible"], 1)
        status, _ = self.request("/api/tracker/move", "POST",
                                 {"opportunity_id": opp_id, "to_status": "shortlisted", "version": version},
                                 origin="https://evil.example")
        self.assertEqual(status, 403)
        status, _ = self.request("/api/tracker/move", "POST",
                                 {"opportunity_id": opp_id, "to_status": "shortlisted", "version": "stale"})
        self.assertEqual(status, 409)
        status, _ = self.request("/api/tracker/move", "POST",
                                 {"opportunity_id": opp_id, "to_status": "user_applied", "version": version})
        self.assertEqual(status, 400)
        status, body = self.request("/api/tracker/move", "POST",
                                    {"opportunity_id": opp_id, "to_status": "shortlisted", "version": version})
        self.assertEqual(status, 200)
        self.assertEqual(body["moved_to"], "shortlisted")
        status, body = self.request(f"/api/tracker/timeline/{opp_id}")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "shortlisted")
        status, _ = self.request("/api/tracker/timeline/opp_" + "z" * 24)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
