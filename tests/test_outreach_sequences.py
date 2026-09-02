"""Workstream D: outreach sequencer (draft-only) + applied-status sync API."""

import json
import re
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

import outreach_sequences as osq
import pipeline_v2
from pipeline_v2 import ConflictError, NotFoundError, ValidationError
from resume_matcher_fixtures import INVENTED, JD_FR, PortTestCase

CONTACT_ID = "con_" + "c" * 24
MODULE_SOURCE = Path(osq.__file__).read_text(encoding="utf-8")


def fake_llm_echo(messages, max_tokens):
    body = messages[-1]["content"].split("DRAFT:\n", 1)[1]
    return {"body": body.replace("I would be glad", "I would be happy")}


def fake_llm_invents(messages, max_tokens):
    return {"body": f"Hello, I am an expert in {INVENTED} and Kubernetes. Test Candidate"}


def fake_llm_raises(messages, max_tokens):
    raise RuntimeError("network down")


class OutreachTestCase(PortTestCase):
    def insert_contact(self, contact_id=CONTACT_ID, name="Jean Dupont", company="Acme Corp", language=None):
        now = datetime.now(timezone.utc).isoformat()
        source = {"profile": "https://www.example.com/in/jd", "verification_status": "unverified"}
        if language:
            source["language"] = language
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO contacts(id, name, company, role, source_json, created_at, updated_at) VALUES (?, ?, ?, 'Recruiter', ?, ?, ?)",
                (contact_id, name, company, json.dumps(source), now, now),
            )
            connection.commit()
        return contact_id, now

    _counter = 0

    def create(self, channel="linkedin", start="2026-09-01", llm=None, **extra):
        OutreachTestCase._counter += 1
        suffix = f"{OutreachTestCase._counter:024d}"
        opp_id, _ = self.insert_opportunity(opportunity_id="opp_" + suffix,
                                            **{k: v for k, v in extra.items() if k in {"description"}})
        contact_id, version = self.insert_contact(contact_id="con_" + suffix,
                                                  **{k: v for k, v in extra.items() if k in {"language"}})
        return osq.create_sequence(self.db_path, contact_id, opp_id, channel, start_date=start,
                                   version=version, llm=llm, **self.sources)


class SafetyTests(unittest.TestCase):
    def test_no_sending_or_automation_code(self):
        lowered = MODULE_SOURCE.casefold()
        for forbidden in ("smtplib", "linkedin.com", "voyager", "playwright", "selenium", "urlopen"):
            self.assertNotIn(forbidden, lowered, forbidden)
        self.assertFalse(re.search(r"\.click\(", MODULE_SOURCE))


class SequenceTests(OutreachTestCase):
    def test_default_cadence_and_bodies(self):
        seq = self.create()
        self.assertEqual(seq["status"], "draft")
        self.assertEqual(seq["language"], "en")
        self.assertEqual([s["n"] for s in seq["steps"]], [0, 1, 2])
        self.assertEqual([s["due_date"] for s in seq["steps"]], ["2026-09-01", "2026-09-06", "2026-09-13"])
        self.assertEqual([s["template_id"] for s in seq["steps"]], ["connection_note", "follow_up", "value_add"])
        self.assertLessEqual(len(seq["steps"][0]["body"]), 300)
        for step in seq["steps"]:
            self.assertEqual(step["state"], "draft")
            self.assertLessEqual(len(step["body"]), 600)
            self.assertNotIn("\u2014", step["body"])
            self.assertNotIn(INVENTED, step["body"])
            self.assertNotIn("Kubernetes", step["body"])
            self.assertNotIn("Airflow", step["body"])
            self.assertTrue(step["evidence_ids"])
        self.assertIn("Jean", seq["steps"][0]["body"])
        self.assertIn("Kafka", seq["steps"][1]["body"])
        self.assertEqual(seq["next_due_date"], "2026-09-01")
        self.assertIn("never sends", seq["send_policy"])

    def test_french_and_email_channel(self):
        seq = self.create(channel="email", description=JD_FR)
        self.assertEqual(seq["language"], "fr")
        self.assertIn("Bonjour Jean", seq["steps"][0]["body"])
        self.assertLessEqual(len(seq["steps"][0]["body"]), 600)
        explicit = self.create(language="en", description=JD_FR)
        self.assertEqual(explicit["language"], "en")

    def test_llm_rephrase_validated(self):
        accepted = self.create(llm=fake_llm_echo)
        self.assertTrue(accepted["steps"][0]["rephrased_by_llm"])
        self.assertIn("happy", accepted["steps"][0]["body"])
        rejected = self.create(llm=fake_llm_invents)
        self.assertFalse(rejected["steps"][0]["rephrased_by_llm"])
        self.assertNotIn(INVENTED, rejected["steps"][0]["body"])
        failing = self.create(llm=fake_llm_raises)
        self.assertFalse(failing["steps"][0]["rephrased_by_llm"])

    def test_errors(self):
        opp_id, _ = self.insert_opportunity()
        contact_id, version = self.insert_contact()
        with self.assertRaises(ValidationError):
            osq.create_sequence(self.db_path, contact_id, opp_id, "sms", version=version, **self.sources)
        with self.assertRaises(ConflictError):
            osq.create_sequence(self.db_path, contact_id, opp_id, "linkedin", version="stale", **self.sources)
        with self.assertRaises(NotFoundError):
            osq.create_sequence(self.db_path, "con_missing", opp_id, "linkedin", **self.sources)
        with self.assertRaises(ValidationError):
            osq.create_sequence(self.db_path, contact_id, opp_id, "linkedin", start_date="bad", **self.sources)

    def test_due_and_mark(self):
        seq = self.create()
        step0, step1 = seq["steps"][0], seq["steps"][1]
        result = osq.due(self.db_path, "2026-09-06")
        self.assertEqual([s["id"] for s in result["steps"]], [step0["id"], step1["id"]])
        self.assertTrue(result["steps"][0]["overdue"])
        with self.assertRaises(ValidationError):
            osq.mark_step(self.db_path, step0["id"], "user_sent", step0["version"], confirmed=False)
        with self.assertRaises(ValidationError):
            osq.mark_step(self.db_path, step0["id"], "user_sent", step0["version"])
        with self.assertRaises(ConflictError):
            osq.mark_step(self.db_path, step0["id"], "skipped", "stale")
        with self.assertRaises(ValidationError):
            osq.mark_step(self.db_path, step0["id"], "replied", step0["version"])  # draft -> replied invalid
        marked = osq.mark_step(self.db_path, step0["id"], "user_sent", step0["version"], confirmed=True)
        self.assertEqual(marked["state"], "user_sent")
        self.assertEqual(osq.due(self.db_path, "2026-09-06")["count"], 1)
        listed = osq.list_sequences(self.db_path, contact_id=seq["contact_id"])["sequences"][0]
        self.assertEqual(listed["status"], "user_sent")
        self.assertEqual(listed["current_step"], 1)
        replied = osq.mark_step(self.db_path, marked["id"], "replied", marked["version"])
        self.assertEqual(replied["state"], "replied")
        self.assertEqual(osq.list_sequences(self.db_path, opportunity_id=seq["opportunity_id"])["sequences"][0]["status"], "replied")
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM outreach_events WHERE event_type='outreach_step_user_sent'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_regenerate(self):
        seq = self.create()
        step = seq["steps"][1]
        with self.assertRaises(ConflictError):
            osq.regenerate_step(self.db_path, step["id"], "stale", **self.sources)
        new = osq.regenerate_step(self.db_path, step["id"], step["version"], llm=fake_llm_echo, **self.sources)
        self.assertEqual(new["state"], "draft")
        self.assertNotEqual(new["version"], step["version"])
        skipped = osq.mark_step(self.db_path, new["id"], "skipped", new["version"])
        with self.assertRaises(ValidationError):
            osq.regenerate_step(self.db_path, skipped["id"], skipped["version"], **self.sources)


class AppliedSyncTests(OutreachTestCase):
    def test_mark_applied_requires_confirmation_and_transitions(self):
        opp_id, version = self.insert_opportunity()
        with self.assertRaises(ValidationError):
            osq.mark_applied(self.db_path, opp_id, {"version": version})
        with self.assertRaises(ValidationError):
            osq.mark_applied(self.db_path, opp_id, {"version": version, "confirmed": "yes"})
        with self.assertRaises(ConflictError):
            osq.mark_applied(self.db_path, opp_id, {"version": "stale", "confirmed": True})
        record = osq.mark_applied(self.db_path, opp_id, {"version": version, "confirmed": True,
                                                         "applied_at": "2026-09-01", "channel": "company site"})
        self.assertEqual(record["status"], "user_applied")
        self.assertEqual(record["application"]["applied_at"], "2026-09-01")
        self.assertIn("company site", record["application"]["notes"])
        # transition rules: user_applied is reachable from any open status (the user may apply on
        # the real site at any time) but still requires confirmation; closed stays terminal.
        other, v2 = self.insert_opportunity(opportunity_id="opp_" + "d" * 24, status="discovered")
        with self.assertRaises(ValidationError):
            osq.mark_applied(self.db_path, other, {"version": v2})
        self.assertEqual(osq.mark_applied(self.db_path, other, {"version": v2, "confirmed": True})["status"], "user_applied")
        closed, v3 = self.insert_opportunity(opportunity_id="opp_" + "e" * 24, status="closed")
        with self.assertRaises(ValidationError):
            osq.mark_applied(self.db_path, closed, {"version": v3, "confirmed": True})

    def test_search_lite(self):
        self.insert_opportunity()
        self.insert_opportunity(opportunity_id="opp_" + "e" * 24, status="closed")
        result = osq.search_lite(self.db_path, "acme")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["company"], "Acme Corp")
        self.assertIn("version", result["items"][0])
        self.assertEqual(osq.search_lite(self.db_path, "")["count"], 0)
        self.assertEqual(osq.search_lite(self.db_path, "data engineer")["count"], 1)


class HttpTests(OutreachTestCase):
    def test_endpoints(self):
        opp_id, opp_version = self.insert_opportunity()
        contact_id, version = self.insert_contact()
        self.start_server()
        payload = {"contact_id": contact_id, "opportunity_id": opp_id, "channel": "linkedin", "version": version}
        status, _ = self.request("/api/outreach/sequences", "POST", payload, origin="https://evil.example")
        self.assertEqual(status, 403)
        status, _ = self.request("/api/outreach/sequences", "POST", {**payload, "version": "stale"})
        self.assertEqual(status, 409)
        status, seq = self.request("/api/outreach/sequences", "POST", payload)
        self.assertEqual(status, 201)
        self.assertEqual(len(seq["steps"]), 3)
        status, body = self.request(f"/api/outreach/sequences?contact_id={contact_id}")
        self.assertEqual((status, body["count"]), (200, 1))
        status, body = self.request(f"/api/outreach/sequences?opportunity_id={opp_id}")
        self.assertEqual((status, body["count"]), (200, 1))
        status, body = self.request("/api/outreach/due?date=2099-01-01")
        self.assertEqual((status, body["count"]), (200, 3))
        status, body = self.request("/api/outreach/due?date=nope")
        self.assertEqual(status, 400)
        step = seq["steps"][0]
        status, _ = self.request(f"/api/outreach/steps/{step['id']}/mark", "POST",
                                 {"state": "user_sent", "version": step["version"]})
        self.assertEqual(status, 400)
        status, _ = self.request(f"/api/outreach/steps/{step['id']}/mark", "POST",
                                 {"state": "user_sent", "version": step["version"], "confirmed": False})
        self.assertEqual(status, 400)
        status, body = self.request(f"/api/outreach/steps/{step['id']}/regenerate", "POST", {"version": step["version"]})
        self.assertEqual((status, body["state"]), (200, "draft"))
        status, body = self.request(f"/api/outreach/steps/{step['id']}/mark", "POST",
                                    {"state": "user_sent", "version": body["version"], "confirmed": True})
        self.assertEqual((status, body["state"]), (200, "user_sent"))
        status, _ = self.request("/api/outreach/steps/missing/mark", "POST",
                                 {"state": "skipped", "version": "x"})
        self.assertEqual(status, 404)
        # applied sync
        status, body = self.request("/api/opportunities/search-lite?q=acme")
        self.assertEqual((status, body["count"]), (200, 1))
        status, _ = self.request(f"/api/opportunities/{opp_id}/applied", "POST", {"version": opp_version})
        self.assertEqual(status, 400)
        status, body = self.request(f"/api/opportunities/{opp_id}/applied", "POST",
                                    {"version": opp_version, "confirmed": True})
        self.assertEqual((status, body["status"]), (200, "user_applied"))


if __name__ == "__main__":
    unittest.main()
