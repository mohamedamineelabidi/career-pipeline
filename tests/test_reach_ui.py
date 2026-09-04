"""Playwright gate for reach.html against a temporary migrated database.

Everything here is measured on the live DOM: nav count, rows, badge words,
disabled state, layout overflow, landmarks, console cleanliness, and the
absence of any control that could send or apply.
"""
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import pipeline_v2

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ["send email", "send draft", "apply now", "submit application", "connect on linkedin", "send message"]
ROUTES = ["targets", "people", "jobs", "runs"]

REACH_TABLES = """
CREATE TABLE IF NOT EXISTS target_companies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    sector TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    intent TEXT NOT NULL DEFAULT 'any',
    priority INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people_candidates (
    id TEXT PRIMARY KEY,
    target_company_id TEXT REFERENCES target_companies(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    headline TEXT NOT NULL DEFAULT '',
    company_seen TEXT NOT NULL DEFAULT '',
    role_seen TEXT NOT NULL DEFAULT '',
    profile_url TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    evidence_url TEXT NOT NULL DEFAULT '',
    evidence_quote TEXT NOT NULL DEFAULT '',
    discovered_via TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL DEFAULT 0,
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    current_role_confirmed_at TEXT,
    promoted_contact_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    email_status TEXT DEFAULT 'none',
    email_evidence_url TEXT,
    email_checked_at TEXT
);
"""

NOW = "2026-09-04T09:00:00"


def make_db(directory):
    db_path = Path(directory) / "pipeline.sqlite3"
    connection = sqlite3.connect(str(db_path))
    connection.executescript(pipeline_v2.SCHEMA)
    connection.executescript(REACH_TABLES)
    connection.executemany(
        "INSERT INTO target_companies(id, name, sector, country, intent, priority, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("tgt_a", "Acme Robotics", "AI", "Morocco", "internship", 5, NOW, NOW),
            ("tgt_b", "Globex Cloud", "Cloud", "Morocco", "job", 3, NOW, NOW),
        ],
    )
    connection.executemany(
        """INSERT INTO people_candidates(id, target_company_id, name, headline, company_seen, role_seen, profile_url,
           evidence_url, evidence_quote, discovered_via, score, verification_status, current_role_confirmed_at,
           created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            ("p_hi", "tgt_a", "Amina Tazi", "Head of Data at Acme Robotics", "Acme Robotics", "Head of Data",
             "https://www.linkedin.com/in/amina-tazi", "https://example.test/team", "Amina leads the data team.",
             "public_web", 80, "profile_only", NOW, NOW, NOW),
            ("p_mid", "tgt_a", "Youssef Benali", "ML Engineer", "Acme Robotics", "ML Engineer",
             "https://www.linkedin.com/in/youssef-benali", "https://example.test/blog", "Youssef wrote the post.",
             "public_web", 55, "unverified", None, NOW, NOW),
            ("p_lo", "tgt_b", "Sara Idrissi", "Cloud Architect", "Globex Cloud", "Cloud Architect",
             "", "https://example.test/talk", "Sara spoke at the cloud meetup.", "public_web", 40,
             "email_verified", NOW, NOW, NOW),
        ],
    )
    connection.executemany(
        "UPDATE people_candidates SET email = ?, email_status = ?, email_evidence_url = ? WHERE id = ?",
        [
            ("amina.tazi@acme.example", "found_official", "https://acme.example/team", "p_hi"),
            ("youssef.benali@acme.example", "inferred", "", "p_mid"),
            ("", "none", "", "p_lo"),
        ],
    )
    connection.commit()
    connection.close()
    return db_path


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class ReachUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp(prefix="reach_ui_")
        cls.db_path = make_db(cls.directory)
        cls.server = pipeline_v2.make_server(cls.db_path, PROJECT_ROOT, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/reach.html"
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch()
        except Exception as error:  # pragma: no cover
            cls.playwright.stop()
            cls.server.shutdown()
            raise unittest.SkipTest(f"chromium unavailable: {error}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        shutil.rmtree(cls.directory, ignore_errors=True)

    def open(self, route, width=1440):
        errors = []
        page = self.browser.new_page(viewport={"width": width, "height": 900})
        page.on("pageerror", lambda e: errors.append(f"PAGEERROR {e}"))
        page.on("console", lambda m: errors.append(f"CONSOLE {m.text}") if m.type == "error" and "Failed to load resource" not in m.text else None)
        page.goto(f"{self.base}#/{route}", wait_until="domcontentloaded")
        page.wait_for_timeout(600)
        self.addCleanup(page.close)
        return page, errors

    def api_available(self, page, endpoint):
        status = page.evaluate(f"fetch('/api/reach/{endpoint}').then(r => r.status)")
        return status == 200

    def test_shell_landmarks_and_nav(self):
        page, errors = self.open("targets")
        self.assertEqual(page.locator("nav .nav-item").count(), 4)
        self.assertEqual(page.locator("h1").count(), 1)
        self.assertEqual(page.locator("main").count(), 1)
        self.assertEqual(page.locator("a.skip-link").count(), 1)
        self.assertEqual(page.locator("nav .nav-item[aria-current='page']").count(), 1)
        self.assertEqual(page.locator("nav .nav-item[aria-current='page']").inner_text().strip(), "Targets")
        page.evaluate("location.hash='#/runs'")
        page.wait_for_timeout(300)
        self.assertEqual(page.locator("nav .nav-item[aria-current='page']").inner_text().strip(), "Runs")
        self.assertEqual(page.locator("h1").inner_text().strip(), "Runs")
        self.assertEqual(page.locator(".page.active").count(), 1)
        self.assertEqual(errors, [])

    def test_targets_lists_two_rows(self):
        page, errors = self.open("targets")
        if not self.api_available(page, "targets"):
            self.skipTest("/api/reach/targets is not served yet")
        page.wait_for_selector("#targets-list .card")
        self.assertEqual(page.locator("#targets-list .card").count(), 2)
        self.assertIn("Acme Robotics", page.locator("#targets-list").inner_text())
        self.assertEqual(page.locator("#page-targets .btn-primary").count(), 1)
        self.assertEqual(errors, [])

    def test_people_cards_badges_and_promote_state(self):
        page, errors = self.open("people")
        if not self.api_available(page, "people"):
            self.skipTest("/api/reach/people is not served yet")
        page.wait_for_selector("#people-list .card")
        self.assertEqual(page.locator("#people-list .card").count(), 3)
        badges = [b.strip() for b in page.locator("#people-list .badge").all_inner_texts()]
        for word in ("profile only", "unverified", "email verified"):
            self.assertIn(word, badges)
        for badge in badges:
            self.assertNotIn("_", badge, f"badge shows an abbreviation: {badge}")
        card = page.locator("#people-list .card", has_text="Youssef Benali")
        promote = card.get_by_role("button", name="Promote to contact", disabled=True)
        self.assertEqual(promote.count(), 1)
        self.assertEqual(promote.get_attribute("title"), "Confirm current role first")
        confirmed = page.locator("#people-list .card", has_text="Amina Tazi")
        self.assertTrue(confirmed.get_by_role("button", name="Promote to contact").is_enabled())
        self.assertEqual(card.get_by_role("button", name="Confirm current role").count(), 1)
        self.assertEqual(card.get_by_role("button", name="Copy LinkedIn note").count(), 1)
        self.assertEqual(card.get_by_role("button", name="Copy LinkedIn message").count(), 1)
        self.assertTrue(card.get_by_role("button", name="Copy email").is_enabled())
        self.assertEqual(card.get_by_role("button", name="Preview draft").count(), 1)
        no_email = page.locator("#people-list .card", has_text="Sara Idrissi")
        self.assertEqual(no_email.get_by_role("button", name="Copy email", disabled=True).count(), 1)
        self.assertEqual(card.get_by_role("button", name="Copy LinkedIn draft").count(), 0)
        self.assertIn("Amina leads the data team.", confirmed.inner_text())
        links = page.locator("#people-list a[target='_blank']")
        for index in range(links.count()):
            self.assertEqual(links.nth(index).get_attribute("rel"), "noopener")
        self.assertIn("linkedin_people_scan.py", page.locator("#page-people code").inner_text())
        self.assertEqual(errors, [])

    def test_people_cards_show_email_and_how_it_was_found(self):
        page, errors = self.open("people")
        if not self.api_available(page, "people"):
            self.skipTest("/api/reach/people is not served yet")
        page.wait_for_selector("#people-list .card")
        badges = [b.strip() for b in page.locator("#people-list .badge").all_inner_texts()]
        for words in ("email found on official page", "email inferred, confirm before use", "no email"):
            self.assertIn(words, badges)
        official = page.locator("#people-list .card", has_text="Amina Tazi")
        self.assertEqual(official.locator("a[href^='mailto:']").count(), 1)
        self.assertEqual(official.locator("a[href^='mailto:']").get_attribute("href"), "mailto:amina.tazi@acme.example")
        self.assertIn("amina.tazi@acme.example", official.inner_text())
        self.assertEqual(official.locator(".email-line svg").count(), 1)
        inferred = page.locator("#people-list .card", has_text="Youssef Benali")
        self.assertEqual(inferred.locator("a[href^='mailto:']").count(), 1)
        none = page.locator("#people-list .card", has_text="Sara Idrissi")
        self.assertEqual(none.locator("a[href^='mailto:']").count(), 0)
        self.assertEqual(errors, [])

    def test_people_card_draft_preview_and_copy(self):
        page, errors = self.open("people")
        if not self.api_available(page, "people"):
            self.skipTest("/api/reach/people is not served yet")
        page.wait_for_selector("#people-list .card")
        card = page.locator("#people-list .card", has_text="Sara Idrissi")
        self.assertEqual(page.locator("#people-list .card").count(), 3)
        self.assertEqual(page.locator("#people-list .card pre").count(), 0)
        preview = card.get_by_role("button", name="Preview draft")
        preview.click()
        card.locator("pre").wait_for()
        text = card.locator("pre").inner_text()
        self.assertIn("Hi Sara,", text)
        self.assertIn("Thank you, the candidate", text)
        self.assertEqual(preview.get_attribute("aria-expanded"), "true")
        self.assertEqual(card.get_by_role("button", name="Copy email", disabled=True).count(), 1)
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        card.get_by_role("button", name="Copy LinkedIn message").click()
        page.locator("#toast.show").wait_for()
        self.assertEqual(page.locator("#toast").inner_text().strip(), "Draft copied. Nothing was sent.")
        copied = page.evaluate("navigator.clipboard.readText()")
        self.assertIn("Hi Sara,", copied)
        self.assertIn("One concrete point:", copied)
        preview.click()
        self.assertEqual(card.locator("pre").count(), 0)
        self.assertEqual(errors, [])

    def test_jobs_empty_state(self):
        page, errors = self.open("jobs")
        if not self.api_available(page, "jobs?kind=internship"):
            self.skipTest("/api/reach/jobs is not served yet")
        page.wait_for_selector("#jobs-table .card-muted")
        self.assertIn("No radar jobs match yet", page.locator("#jobs-table").inner_text())
        self.assertEqual(page.locator("#page-jobs [data-kind][aria-pressed='true']").count(), 1)
        self.assertEqual(errors, [])

    def test_runs_controls(self):
        page, errors = self.open("runs")
        self.assertEqual(page.get_by_role("button", name="Run Morocco job radar").count(), 1)
        self.assertEqual(page.get_by_role("button", name="Find people on public web").count(), 1)
        self.assertEqual(page.get_by_role("button", name="Find emails for a target").count(), 1)
        self.assertEqual(page.locator("#page-runs .btn-primary").count(), 1)
        self.assertEqual(errors, [])

    def test_narrow_viewport_has_no_horizontal_overflow(self):
        for route in ROUTES:
            page, errors = self.open(route, width=820)
            page.wait_for_timeout(400)
            overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            self.assertLessEqual(overflow, 0, f"{route} overflows by {overflow}px at 820px")
            columns = page.eval_on_selector(".shell", "e => getComputedStyle(e).gridTemplateColumns.split(' ').length")
            self.assertEqual(columns, 1)
            self.assertEqual(errors, [])

    def test_no_forbidden_words_and_tap_targets(self):
        for route in ROUTES:
            page, errors = self.open(route)
            page.wait_for_timeout(400)
            texts = page.evaluate("[...document.querySelectorAll('button, a')].map(e => (e.innerText || e.getAttribute('aria-label') || '').trim().toLowerCase())")
            for text in texts:
                for word in FORBIDDEN:
                    self.assertNotIn(word, text)
            small = page.evaluate("""[...document.querySelectorAll('button')].filter(b => {
                const r = b.getBoundingClientRect(); return r.height > 0 && r.height < 44; }).map(b => b.innerText)""")
            self.assertEqual(small, [], f"{route}: buttons under 44px: {small}")
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
