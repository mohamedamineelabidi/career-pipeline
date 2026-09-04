"""Outreach drafts are short, plain, fact-based and never sent."""
import json
import tempfile
import unittest
from pathlib import Path

import pipeline_v2
from reach import drafts

FACT = "j'ai construit un pipeline RAG en production pour Netix"
CONTACT = {"name": "Sara Alami", "company": "OCP", "role": "Recruiter"}
INTERN = {"role_kind": "internship", "title": "Stage PFE Data", "company": "OCP"}
JOB = {"role_kind": "exact_vacancy", "title": "Data Engineer", "company": "OCP"}
BANNED = ("I applied", "j'ai postulé", "I have applied", "\u2014", "\u2013")


class DraftForTests(unittest.TestCase):
    def _check_common(self, body):
        self.assertTrue(250 <= len(body) <= 500, len(body))
        self.assertIn("Sara", body)
        self.assertIn(FACT, body)
        for banned in BANNED:
            self.assertNotIn(banned, body)
        self.assertTrue(body.endswith("?"))
        self.assertEqual(body.count("?"), 1)

    def test_fr_internship_and_job(self):
        for opp in (INTERN, JOB):
            body = drafts.draft_for(CONTACT, opp, "fr", FACT)
            self._check_common(body)
            self.assertIn("Bonjour", body)

    def test_en_internship_and_job(self):
        for opp in (INTERN, JOB):
            body = drafts.draft_for(CONTACT, opp, "en", FACT)
            self._check_common(body)
            self.assertTrue("Hello" in body or "Hi" in body)

    def test_templates_differ_by_role_kind_and_are_deterministic(self):
        a = drafts.draft_for(CONTACT, INTERN, "en", FACT)
        b = drafts.draft_for(CONTACT, JOB, "en", FACT)
        self.assertNotEqual(a, b)
        self.assertEqual(a, drafts.draft_for(CONTACT, INTERN, "en", FACT))
        self.assertIn("Data Engineer", b)

    def test_none_opportunity_falls_back_to_job(self):
        body = drafts.draft_for(CONTACT, None, "en", FACT)
        self._check_common(body)

    def test_module_text_has_no_dashes(self):
        text = Path(drafts.__file__).read_text(encoding="utf-8")
        self.assertNotIn("\u2014", text)
        self.assertNotIn("\u2013", text)


CRINGE_WORDS = ("passionné", "passionate", "dynamique", "motivé", "n'hésitez pas", "je me permets",
                "I hope this message finds you", "synergy", "leverage", "!")


class FactSheetTests(unittest.TestCase):
    def setUp(self):
        self.sheet = drafts.about_me()

    def test_identity_is_truthful(self):
        self.assertEqual(self.sheet["name"], "the candidate")
        self.assertEqual(self.sheet["school"], "ENSAH")
        self.assertEqual(self.sheet["email"], "you@example.com")
        self.assertEqual(self.sheet["links"]["github"], "https://github.com/your-github-handle")
        self.assertEqual(self.sheet["links"]["linkedin"], "")
        self.assertIn("+000 000000000", self.sheet["signature_fr"])
        self.assertIn("Rabat", self.sheet["signature_en"])

    def test_every_proof_has_both_languages_and_tags(self):
        ids = [p["id"] for p in self.sheet["proofs"]]
        self.assertEqual(ids, ["upfund", "arya", "netix", "club"])
        for proof in self.sheet["proofs"]:
            self.assertTrue(proof["fr"] and proof["en"])
            self.assertTrue(proof["tags"])
        for kind in ("internship", "job"):
            self.assertTrue(self.sheet["seeking"][kind]["fr"] and self.sheet["seeking"][kind]["en"])

    def test_sheet_text_is_clean(self):
        text = json.dumps(self.sheet, ensure_ascii=False)
        self.assertNotIn("\u2014", text)
        self.assertNotIn("\u2013", text)
        for word in CRINGE_WORDS:
            self.assertNotIn(word.lower(), text.lower(), word)


class PersonaTests(unittest.TestCase):
    def test_persona_from_role_and_headline(self):
        self.assertEqual(drafts.persona({"role_seen": "talent acquisition"}), "recruiter")
        self.assertEqual(drafts.persona({"role_seen": "Chargée RH"}), "recruiter")
        self.assertEqual(drafts.persona({"role_seen": "data manager", "headline": "Head of Data @ Deloitte"}), "manager")
        self.assertEqual(drafts.persona({"headline": "Consultant, ENSAH alumni"}), "alumni")
        self.assertEqual(drafts.persona({"headline": "Partner"}), "senior")
        self.assertEqual(drafts.persona({"headline": "Directeur Data"}), "senior")
        self.assertEqual(drafts.persona({"headline": "Tech Lead"}), "manager")
        self.assertEqual(drafts.persona({"headline": "Data Engineer"}), "peer")
        self.assertEqual(drafts.persona({}), "peer")

    def test_alumni_wins_over_other_signals_when_ensah_in_quote(self):
        self.assertEqual(drafts.persona({"headline": "Senior Manager", "evidence_quote": "diplômé de l'ENSAH"}),
                         "alumni")


class LintTests(unittest.TestCase):
    def test_lint_flags_cringe_and_length(self):
        self.assertEqual(drafts.lint("Je suis passionné par la data", channel="linkedin_note"), ["banned:passionné"])
        self.assertEqual(drafts.lint("x" * 301, channel="linkedin_note"), ["too_long:301>300"])
        self.assertEqual(drafts.lint("x" * 701, channel="linkedin_message"), ["too_long:701>700"])
        self.assertEqual(drafts.lint("Bonjour Hajar, merci.", channel="email", subject=""), ["missing_subject"])
        self.assertEqual(drafts.lint("Bonjour Hajar, merci.", channel="email", subject="Hi"), ["subject_length:2"])
        self.assertEqual(drafts.lint("Bonjour Hajar, merci.", channel="email", subject="Stage PFE 2027"), [])

    def test_lint_flags_dashes_exclamations_and_i_heavy_text(self):
        self.assertEqual(drafts.lint("Bonjour \u2014 merci", channel="linkedin_note"), ["banned:\u2014"])
        self.assertEqual(drafts.lint("Merci! Super!", channel="linkedin_note"), ["too_many_exclamations:2"])
        text = "I build things. I ship code. I lead a club. Thanks."
        self.assertEqual(drafts.lint(text, channel="linkedin_message"), ["too_many_i_sentences:3"])
        self.assertEqual(drafts.lint("I hope this message finds you well.", channel="linkedin_note"),
                         ["banned:I hope this message finds you"])

    def test_lint_is_case_insensitive_and_rejects_unknown_channel(self):
        self.assertEqual(drafts.lint("PASSIONATE about data", channel="email", subject="Hello there"),
                         ["banned:passionate"])
        with self.assertRaises(ValueError):
            drafts.lint("x", channel="fax")


class HookTests(unittest.TestCase):
    def test_hook_uses_the_persons_evidence_not_a_template(self):
        p = {"name": "Kenza Akli", "headline": "Deputy HR Director @ Deloitte | Talent Management",
             "evidence_quote": "Kenza has led the talent management team since September 2025 across offices."}
        self.assertEqual(drafts.hook(p, "fr"), "j'ai vu que vous pilotez le Talent Management chez Deloitte")
        self.assertEqual(drafts.hook(p, "en"), "I saw that you lead Talent Management at Deloitte")

    def test_hook_falls_back_to_role_seen_and_company(self):
        p = {"name": "Amina Tazi", "headline": "", "role_seen": "Head of Data", "company_seen": "Acme Robotics"}
        self.assertEqual(drafts.hook(p, "fr"), "j'ai vu que vous êtes Head of Data chez Acme Robotics")
        self.assertEqual(drafts.hook(p, "en"), "I saw that you are Head of Data at Acme Robotics")
        self.assertEqual(drafts.hook(p, "en", company="Acme"), "I saw that you are Head of Data at Acme")

    def test_hook_skips_school_segments_and_long_text(self):
        self.assertEqual(drafts.hook({"headline": "Consultant chez KPMG, ENSAH alumni"}, "fr"),
                         "j'ai vu que vous êtes Consultant chez KPMG")
        self.assertEqual(drafts.hook({"headline": " ".join(["word"] * 9)}, "en"), "")
        self.assertEqual(drafts.hook({"headline": "Data | " + " ".join(["word"] * 9)}, "en"), "I saw that you are Data")

    def test_hook_returns_empty_when_nothing_usable(self):
        self.assertEqual(drafts.hook({}, "fr"), "")
        self.assertEqual(drafts.hook({"evidence_quote": "a very long quote about nothing in particular here"}, "en"), "")
        for text in (drafts.hook({"headline": "Partner"}, "fr"),):
            self.assertNotIn("—", text)


PEOPLE = {
    "recruiter": {"name": "Kenza Akli", "headline": "Deputy HR Director @ Deloitte | Talent Management",
                  "role_seen": "Talent Acquisition", "company_seen": "Deloitte", "email": "k@deloitte.test"},
    "manager": {"name": "Omar Idrissi", "headline": "Head of Data & AI @ Orange Maroc", "role_seen": "Data Manager",
                "company_seen": "Orange Maroc"},
    "senior": {"name": "Sara Bennani", "headline": "Partner @ EY", "role_seen": "Partner", "company_seen": "EY"},
    "alumni": {"name": "Yassine Kadiri", "headline": "Consultant chez KPMG, ENSAH alumni", "company_seen": "KPMG"},
    "peer": {"name": "Hajar Lamrani", "headline": "Data Engineer @ OCP", "role_seen": "Data Engineer",
             "company_seen": "OCP"},
}


class ComposeTests(unittest.TestCase):
    def test_every_persona_channel_lang_passes_lint(self):
        sheet = drafts.about_me()
        proof_texts = {lang: [p[lang] for p in sheet["proofs"]] for lang in ("fr", "en")}
        for want, person in PEOPLE.items():
            for channel in drafts.CHANNELS:
                for lang in ("fr", "en"):
                    with self.subTest(persona=want, channel=channel, lang=lang):
                        out = drafts.compose(person, channel, lang)
                        self.assertEqual(out["persona"], want)
                        self.assertEqual(out["lint"], [])
                        self.assertEqual(drafts.lint(out["body"], channel, out["subject"]), [])
                        body = out["body"]
                        self.assertTrue(body.startswith(("Bonjour " if lang == "fr" else "Hi ") + person["name"].split()[0] + ","))
                        self.assertNotIn("!", body)
                        self.assertLessEqual(len(body), drafts.LIMITS[channel])
                        self.assertIn(out["proof_id"], [p["id"] for p in sheet["proofs"]])
                        if channel == "linkedin_note":
                            self.assertIsNone(out["subject"])
                            self.assertTrue(body.endswith("Merci, the candidate" if lang == "fr" else "Thank you, the candidate"))
                            self.assertEqual(sum(1 for t in proof_texts[lang] if t in body), 0)
                        else:
                            self.assertEqual(sum(1 for t in proof_texts[lang] if t in body), 1)
                            self.assertIn(sheet["status_" + lang], body)
                            self.assertIn(sheet["seeking"]["internship"][lang], body)
                        if channel == "email":
                            self.assertTrue(6 <= len(out["subject"]) <= 60, out["subject"])
                            self.assertTrue(body.endswith(sheet["signature_" + lang] + "\nGitHub: " + sheet["links"]["github"]))
                            self.assertNotIn("LinkedIn:", body)
                        else:
                            self.assertNotIn("github.com", body)
                        self.assertEqual(body.count("the candidate"), 1)

    def test_proof_and_ask_follow_persona(self):
        self.assertEqual(drafts.compose(PEOPLE["recruiter"], "email", "fr")["proof_id"], "arya")
        self.assertEqual(drafts.compose(PEOPLE["recruiter"], "email", "fr", company="Orange")["proof_id"], "netix")
        self.assertEqual(drafts.compose(PEOPLE["manager"], "email", "fr")["proof_id"], "arya")
        cloud = dict(PEOPLE["manager"], headline="Head of Cloud @ Orange Maroc")
        self.assertEqual(drafts.compose(cloud, "email", "en")["proof_id"], "upfund")
        telecom = dict(PEOPLE["manager"], headline="Network Manager @ Orange Maroc")
        self.assertEqual(drafts.compose(telecom, "email", "en")["proof_id"], "netix")
        self.assertEqual(drafts.compose(PEOPLE["senior"], "email", "en")["proof_id"], "upfund")
        self.assertIn(drafts.compose(PEOPLE["alumni"], "email", "en")["proof_id"], ("club", "netix"))
        self.assertIn("PFE", drafts.compose(PEOPLE["recruiter"], "linkedin_message", "en")["body"])
        self.assertIn("15", drafts.compose(PEOPLE["manager"], "linkedin_message", "fr")["body"])
        self.assertIn("KPMG", drafts.compose(PEOPLE["alumni"], "linkedin_message", "en")["body"])
        self.assertIn("right person", drafts.compose(PEOPLE["senior"], "linkedin_message", "en")["body"])
        self.assertIn("OCP", drafts.compose(PEOPLE["peer"], "linkedin_message", "en")["body"])

    def test_job_kind_and_subject_and_hook_drop(self):
        out = drafts.compose(PEOPLE["recruiter"], "email", "fr", kind="job")
        self.assertIn("premier poste", out["body"])
        self.assertIn("Deloitte", out["subject"])
        en = drafts.compose(PEOPLE["senior"], "email", "en")
        self.assertEqual(en["subject"], "PFE internship 2027, AI and data, EY")
        bare = drafts.compose({"name": "Nadia"}, "linkedin_message", "en")
        self.assertNotIn("I saw", bare["body"])
        self.assertEqual(bare["lint"], [])
        with self.assertRaises(ValueError):
            drafts.compose(PEOPLE["peer"], "fax", "en")
        with self.assertRaises(ValueError):
            drafts.compose(PEOPLE["peer"], "email", "de")


class SaveDraftTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        db = Path(self._dir.name) / "pipeline.sqlite3"
        pipeline_v2.create_schema(db)
        self.conn = pipeline_v2.connect(db)
        self.addCleanup(self.conn.close)
        now = "2026-09-04T00:00:00+00:00"
        self.conn.execute(
            "INSERT INTO contacts(id, name, company, role, source_json, created_at, updated_at) "
            "VALUES ('ct_1', 'Sara Alami', 'OCP', 'Recruiter', '{}', ?, ?)", (now, now))
        self.conn.execute(
            "INSERT INTO contact_routes(id, contact_id, route_type, value, is_verified) "
            "VALUES ('cr_1', 'ct_1', 'linkedin', 'https://www.linkedin.com/in/sara', 0)")
        self.conn.commit()

    def test_save_draft_inserts_not_opened(self):
        body = drafts.draft_for(CONTACT, JOB, "fr", FACT)
        draft_id = drafts.save_draft(self.conn, "ct_1", None, "cr_1", "linkedin", "fr", body,
                                     fact=FACT)
        self.assertTrue(draft_id.startswith("dr_"))
        row = self.conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        self.assertEqual(row["status"], "draft_not_opened")
        self.assertEqual(row["body"], body)
        self.assertEqual(row["channel"], "linkedin")
        self.assertEqual(row["contact_route_id"], "cr_1")
        self.assertEqual(row["subject"], "")
        source = json.loads(row["source_json"])
        self.assertEqual(source["generator"], "reach")
        self.assertEqual(source["fact"], FACT)


if __name__ == "__main__":
    unittest.main()
