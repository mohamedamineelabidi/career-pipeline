"""Tests for application_prep (pre-fill, never submit)."""
import json
import re
import sys
import unittest
from contextlib import closing
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import application_prep  # noqa: E402
import pipeline_v2  # noqa: E402
from resume_matcher_fixtures import PortTestCase  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fake_greenhouse.html"
MODULE_PATH = Path(application_prep.__file__)
MASTER = {
    "identity": {
        "name": "Test Candidate",
        "email": "test@example.com",
        "phone": "+212 600000000",
        "linkedin_url": "https://www.linkedin.com/in/testcandidate",
        "location": "Rabat, Morocco",
    }
}


class DetectAtsTests(unittest.TestCase):
    def test_detects_known_hosts(self):
        cases = {
            "https://boards.greenhouse.io/x/jobs/1": "greenhouse",
            "https://job-boards.greenhouse.io/x/jobs/1": "greenhouse",
            "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/1": "workday",
            "https://jobs.lever.co/acme/abc": "lever",
            "https://jobs.ashbyhq.com/acme/uuid": "ashby",
            "https://jobs.smartrecruiters.com/Acme/123": "smartrecruiters",
            "https://apply.workable.com/acme/j/ABC/": "workable",
            "https://www.linkedin.com/jobs/view/123": "linkedin",
            "https://careers.bcg.com/job/1": "unknown",
            "": "unknown",
            "not a url": "unknown",
        }
        for url, expected in cases.items():
            self.assertEqual(application_prep.detect_ats(url), expected, url)

    def test_greenhouse_in_path_does_not_fool_detector(self):
        self.assertEqual(application_prep.detect_ats("https://evil.example/greenhouse.io/x"), "unknown")


class BuildPrefillTests(unittest.TestCase):
    def test_maps_identity_and_splits_name(self):
        prefill = application_prep.build_prefill(MASTER, "greenhouse", resume_path="/x/cv.pdf")
        self.assertEqual(prefill["first_name"], "Test")
        self.assertEqual(prefill["last_name"], "Candidate")
        self.assertEqual(prefill["full_name"], "Test Candidate")
        self.assertEqual(prefill["email"], "test@example.com")
        self.assertEqual(prefill["phone"], "+212 600000000")
        self.assertEqual(prefill["linkedin"], "https://www.linkedin.com/in/testcandidate")
        self.assertEqual(prefill["location"], "Rabat, Morocco")
        self.assertEqual(prefill["resume_path"], "/x/cv.pdf")
        self.assertIsNone(prefill["github"])
        self.assertIsNone(prefill["portfolio"])

    def test_missing_fields_are_none_never_invented(self):
        prefill = application_prep.build_prefill({"identity": {"name": "Solo"}}, "lever")
        self.assertEqual(prefill["first_name"], "Solo")
        self.assertIsNone(prefill["last_name"])
        for key in ("email", "phone", "linkedin", "location", "resume_path"):
            self.assertIsNone(prefill[key])
        empty = application_prep.build_prefill({}, "ashby")
        self.assertTrue(all(v is None for v in empty.values()))

    def test_real_career_master_identity(self):
        master = yaml.safe_load(application_prep.CAREER_MASTER_PATH.read_text(encoding="utf-8"))
        prefill = application_prep.build_prefill(master, "ashby")
        self.assertEqual(prefill["email"], master["identity"]["email"])
        self.assertEqual(prefill["linkedin"], master["identity"]["linkedin_url"])


class ClickAllowListTests(unittest.TestCase):
    def test_every_click_target_is_allow_listed_and_not_submit_like(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        allowed = set(application_prep.ALLOWED_CLICK_SELECTORS)
        for match in re.finditer(r"\.click\(([^)]*)\)", source):
            arg = match.group(1).strip()
            literal = arg.strip("'\"")
            self.assertTrue(
                literal in allowed or arg == "selector" and "for selector in ALLOWED_CLICK_SELECTORS" in source,
                f"click target not allow-listed: {match.group(0)}")
        forbidden = application_prep.FORBIDDEN_CLICK
        for selector in application_prep.ALLOWED_CLICK_SELECTORS:
            self.assertIsNone(forbidden.search(selector), selector)
        self.assertNotIn("form.submit", source)
        self.assertNotIn("press(\"Enter\"", source)
        self.assertNotIn("keyboard.press", source)
        self.assertNotRegex(source, r"captcha", "module must not touch CAPTCHA")

    def test_profiles_json_has_no_submit_like_selectors(self):
        profiles = application_prep.load_profiles()
        for ats in list(profiles["profiles"]) + ["generic"]:
            for field, selectors in application_prep.selectors_for(ats, profiles).items():
                for selector in selectors:
                    application_prep.assert_safe_selector(selector)

    def test_guard_rejects_submit_selector(self):
        with self.assertRaises(pipeline_v2.ValidationError):
            application_prep.assert_safe_selector("input[type=submit]")
        with self.assertRaises(pipeline_v2.ValidationError):
            application_prep.assert_safe_selector("#apply_btn")


def _headless_factory(headless):
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    return pw, pw.chromium.launch(headless=True)


class PrepareApplicationTests(PortTestCase):
    def _prepared(self, url, factory=_headless_factory, **kwargs):
        opp_id, _ = self.insert_opportunity()
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            connection.execute("UPDATE opportunities SET url=? WHERE id=?", (url, opp_id))
            connection.commit()
        self.insert_artifact(opp_id, "cv text")
        return opp_id, application_prep.prepare_application(
            self.db_path, opp_id, headless=True, browser_factory=factory,
            project_root=self.root, career_master_path=self.master_path,
            screenshot_dir=self.root / "shots", **kwargs)

    def test_linkedin_is_manual_only_without_browser(self):
        opp_id, _ = self.insert_opportunity()
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            connection.execute("UPDATE opportunities SET url=? WHERE id=?", ("https://www.linkedin.com/jobs/view/1", opp_id))
            connection.commit()

        def boom(headless):
            raise AssertionError("browser must not be launched for linkedin")

        result = application_prep.prepare_application(self.db_path, opp_id, browser_factory=boom, project_root=self.root)
        self.assertEqual(result["prep"]["status"], "manual_only")
        self.assertEqual(result["prep"]["ats"], "linkedin")
        self.assertEqual(application_prep.latest_prep(self.db_path, opp_id)["status"], "manual_only")

    def test_missing_pdf_is_not_found(self):
        opp_id, _ = self.insert_opportunity()
        with self.assertRaises(pipeline_v2.NotFoundError):
            application_prep.prepare_application(self.db_path, opp_id, headless=True, project_root=self.root)

    def test_fills_fixture_form_attaches_pdf_and_never_submits(self):
        self.master_path.write_text(yaml.safe_dump(MASTER), encoding="utf-8")
        # Verify not-clicked + values via a second Playwright pass on the same file after fill.
        clicked = {}

        def factory(headless):
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=True)
            real_new_page = browser.new_page

            def new_page(*a, **k):
                page = real_new_page(*a, **k)
                real_shot = page.screenshot

                def shot(**kw):
                    clicked["submit"] = page.get_attribute("#submit_app", "data-clicked")
                    clicked["apply"] = page.get_attribute("#apply_btn", "data-clicked")
                    clicked["first"] = page.input_value("#first_name")
                    clicked["last"] = page.input_value("#last_name")
                    clicked["email"] = page.input_value("#email")
                    clicked["phone"] = page.input_value("#phone")
                    clicked["linkedin"] = page.input_value("#question_linkedin")
                    clicked["files"] = page.evaluate("document.getElementById('resume').files.length")
                    return real_shot(**kw)
                page.screenshot = shot
                return page
            browser.new_page = new_page
            return pw, browser

        opp_id, result = self._prepared(FIXTURE.as_uri(), factory=factory)
        # url_override keeps file:// while ats profile is forced via generic fallback -> ats 'unknown'
        prep = result["prep"]
        self.assertEqual(prep["status"], "prepared_awaiting_user", prep)
        self.assertEqual(clicked.get("submit", "false"), "false")
        self.assertEqual(clicked.get("apply", "false"), "false")
        filled = prep["filled_fields"]
        self.assertEqual(filled["first_name"]["value"], "Test")
        self.assertEqual(filled["last_name"]["value"], "Candidate")
        self.assertEqual(filled["email"]["value"], "test@example.com")
        self.assertEqual(filled["phone"]["value"], "+212 600000000")
        self.assertIn("linkedin", filled)
        self.assertTrue(filled["resume_path"]["value"].endswith(".pdf"))
        self.assertTrue((self.root / "shots" / f"{opp_id}.png").is_file())
        self.assertIn(opp_id, prep["screenshot_path"])
        self.assertIsNone(result["browser"])  # headless -> closed
        row = application_prep.latest_prep(self.db_path, opp_id)
        self.assertEqual(row["id"], prep["id"])
        self.assertEqual(len(application_prep.list_preps(self.db_path)), 1)

    def test_fixture_dom_state_after_fill(self):
        """Re-open fixture through Playwright, run fill_page directly, inspect DOM."""
        from playwright.sync_api import sync_playwright
        pdf = self.root / "cv.pdf"
        pdf.write_bytes(b"%PDF-1.4 test")
        prefill = application_prep.build_prefill(MASTER, "greenhouse", resume_path=str(pdf))
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(FIXTURE.as_uri())
            filled = application_prep.fill_page(page, prefill, "greenhouse")
            self.assertEqual(page.input_value("#first_name"), "Test")
            self.assertEqual(page.input_value("#last_name"), "Candidate")
            self.assertEqual(page.input_value("#email"), "test@example.com")
            self.assertEqual(page.input_value("#phone"), "+212 600000000")
            self.assertEqual(page.input_value("#question_linkedin"), MASTER["identity"]["linkedin_url"])
            self.assertEqual(page.evaluate("document.getElementById('resume').files.length"), 1)
            self.assertEqual(page.get_attribute("#submit_app", "data-clicked"), "false")
            self.assertEqual(page.get_attribute("#apply_btn", "data-clicked"), "false")
            self.assertEqual(set(filled), {"first_name", "last_name", "email", "phone", "linkedin", "resume_path"})
            browser.close()


class HttpTests(PortTestCase):
    def test_endpoints(self):
        self.start_server()
        status, body = self.request("/api/applications/preps")
        self.assertEqual((status, body), (200, []))
        opp_id, version = self.insert_opportunity()
        status, _ = self.request(f"/api/applications/prep/{opp_id}")
        self.assertEqual(status, 404)
        status, _ = self.request("/api/applications/prepare", "POST",
                                 {"opportunity_id": opp_id, "version": version}, origin="http://evil.test")
        self.assertEqual(status, 403)
        status, _ = self.request("/api/applications/prepare", "POST", {"opportunity_id": opp_id, "version": "stale"})
        self.assertEqual(status, 409)
        status, body = self.request("/api/applications/prepare", "POST", {"opportunity_id": opp_id, "version": version})
        self.assertEqual(status, 404, body)  # no tailored PDF
        status, _ = self.request("/api/applications/prepare", "POST", {"opportunity_id": "opp_missing", "version": version})
        self.assertEqual(status, 404)
        # linkedin -> 200 manual_only, no browser needed
        with closing(pipeline_v2.connect(self.db_path)) as connection:
            connection.execute("UPDATE opportunities SET url='https://www.linkedin.com/jobs/view/9' WHERE id=?", (opp_id,))
            connection.commit()
        status, body = self.request("/api/applications/prepare", "POST",
                                    {"opportunity_id": opp_id, "version": version, "headless": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["prep"]["status"], "manual_only")
        self.assertTrue(body["nothing_submitted"])
        status, body = self.request(f"/api/applications/prep/{opp_id}")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "manual_only")
        status, body = self.request("/api/applications/preps")
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["company"], "Acme Corp")


if __name__ == "__main__":
    unittest.main()
