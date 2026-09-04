import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pipeline_v2  # noqa: E402
from reach import people_discovery as pd  # noqa: E402

STAGING_DDL = """
CREATE TABLE IF NOT EXISTS target_companies (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, aliases_json TEXT NOT NULL DEFAULT '[]',
    sector TEXT NOT NULL DEFAULT '', country TEXT NOT NULL DEFAULT '', intent TEXT NOT NULL DEFAULT 'any',
    priority INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people_candidates (
    id TEXT PRIMARY KEY, target_company_id TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
    headline TEXT NOT NULL DEFAULT '', company_seen TEXT NOT NULL DEFAULT '', role_seen TEXT NOT NULL DEFAULT '',
    profile_url TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', evidence_url TEXT NOT NULL DEFAULT '',
    evidence_quote TEXT NOT NULL DEFAULT '', discovered_via TEXT NOT NULL DEFAULT '', score INTEGER NOT NULL DEFAULT 0,
    verification_status TEXT NOT NULL DEFAULT 'unverified', current_role_confirmed_at TEXT,
    promoted_contact_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


def make_db(tmpdir: str) -> sqlite3.Connection:
    db = Path(tmpdir) / "p.sqlite3"
    pipeline_v2.create_schema(db)
    conn = pipeline_v2.connect(db)
    conn.executescript(STAGING_DDL)
    conn.execute(
        "INSERT INTO target_companies(id, name, created_at, updated_at) VALUES ('tc_1', 'Inwi', 'x', 'x')"
    )
    conn.commit()
    return conn


def fake_search(results_by_query):
    def search(query):
        return results_by_query.get(query, results_by_query.get("*", []))

    return search


class DiscoverPublicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = make_db(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_once(self, results, pace=2.0):
        reads, sleeps = [], []

        def read(url):
            reads.append(url)
            return "Karim Bennani is Talent Acquisition Manager at Inwi in Casablanca."

        ids = pd.discover_public(
            self.conn, "tc_1", "Inwi", fake_search({"*": results}), read,
            pace_seconds=pace, sleep_fn=sleeps.append,
        )
        return ids, reads, sleeps

    def rows(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM people_candidates")]

    def test_linkedin_urls_stored_as_profile_and_never_read(self):
        results = [{"url": "https://www.linkedin.com/in/karim-bennani-1a2b3c/?trk=x", "title": "Karim Bennani - Recruiter - Inwi | LinkedIn", "snippet": "Talent acquisition at Inwi"}]
        ids, reads, _ = self.run_once(results)
        self.assertEqual(len(ids), 1)
        self.assertEqual(reads, [])
        row = self.rows()[0]
        self.assertEqual(row["profile_url"], results[0]["url"])
        self.assertEqual(row["discovered_via"], "public_web")
        self.assertEqual(row["verification_status"], "unverified")
        self.assertEqual(row["name"], "Karim Bennani")
        self.assertEqual(row["email"], "")

    def test_glassdoor_and_indeed_never_read(self):
        results = [
            {"url": "https://www.glassdoor.fr/Avis/Inwi", "title": "Inwi avis", "snippet": ""},
            {"url": "https://ma.indeed.com/cmp/Inwi", "title": "Inwi", "snippet": ""},
        ]
        ids, reads, _ = self.run_once(results)
        self.assertEqual(reads, [])
        self.assertEqual(ids, [])

    def test_public_page_read_and_evidence_stored(self):
        results = [{"url": "https://www.medias24.com/article-1", "title": "Inwi nomme Karim Bennani", "snippet": "Karim Bennani, head of data chez Inwi, " + "x" * 300}]
        ids, reads, _ = self.run_once(results)
        self.assertEqual(reads, ["https://www.medias24.com/article-1"])
        self.assertEqual(len(ids), 1)
        row = self.rows()[0]
        self.assertEqual(row["evidence_url"], "https://www.medias24.com/article-1")
        self.assertLessEqual(len(row["evidence_quote"]), 240)
        self.assertEqual(row["profile_url"], "")
        self.assertEqual(row["name"], "Karim Bennani")
        # role comes from the page text read via read_fn
        self.assertEqual(row["role_seen"], "talent acquisition")

    def test_pacing_sleep_between_same_host_reads(self):
        results = [
            {"url": "https://news.example/a", "title": "Karim Bennani", "snippet": ""},
            {"url": "https://other.example/b", "title": "Sara Alaoui", "snippet": ""},
            {"url": "https://news.example/c", "title": "Yassine Idrissi", "snippet": ""},
        ]
        _, reads, sleeps = self.run_once(results, pace=2.5)
        self.assertEqual(len(reads), 3)
        # only the second hit on news.example needs a pause
        self.assertEqual(sleeps, [2.5])

    def test_dedupe_on_normalized_profile_url_and_name(self):
        results = [
            {"url": "https://www.linkedin.com/in/karim-bennani/", "title": "Karim Bennani - Inwi", "snippet": ""},
            {"url": "https://WWW.LinkedIn.com/in/Karim-Bennani?utm=1", "title": "Karim Bennani - Inwi", "snippet": ""},
            {"url": "https://news.example/a", "title": "Karim Bennani promu chez Inwi", "snippet": ""},
            {"url": "https://news.example/b", "title": "Sara Alaoui rejoint Inwi", "snippet": ""},
        ]
        ids, _, _ = self.run_once(results)
        self.assertEqual(len(ids), 2)
        names = sorted(r["name"] for r in self.rows())
        self.assertEqual(names, ["Karim Bennani", "Sara Alaoui"])
        # second run inserts nothing new
        ids2, _, _ = self.run_once(results)
        self.assertEqual(ids2, [])
        self.assertEqual(len(self.rows()), 2)

    def test_normalize_profile_url(self):
        self.assertEqual(
            pd.normalize_profile_url("https://WWW.LinkedIn.com/in/Karim-Bennani/?x=1"),
            "https://www.linkedin.com/in/karim-bennani",
        )


    def test_linkedin_result_uses_title_as_name_and_scores_the_row(self):
        # Exa returns the profile owner's name as Title; the snippet starts with a
        # job title that must NOT be mistaken for the person's name.
        results = [{
            "url": "https://ma.linkedin.com/in/meriem-hsaini-95951914",
            "title": "Meriem Hsaini",
            "snippet": "Human Resources Business Partner | Inwi Casablanca-Settat, Morocco Talent Acquisition Specialist - Inwi",
        }]
        ids, _reads, _sleeps = self.run_once(results)
        row = self.rows()[0]
        self.assertEqual(row["name"], "Meriem Hsaini")
        self.assertEqual(row["role_seen"], "talent acquisition")
        self.assertGreater(row["score"], 0)

    def test_linkedin_result_headline_is_stored_when_the_channel_provides_it(self):
        results = [{"url": "https://www.linkedin.com/in/kenza-akli", "title": "Kenza Akli",
                    "headline": "Deputy HR Director @ Deloitte", "snippet": "Deputy HR Director @ Deloitte Talent Management"}]
        self.run_once(results)
        self.assertEqual(self.rows()[0]["headline"], "Deputy HR Director @ Deloitte")

    def test_linkedin_result_without_title_falls_back_to_url_slug(self):
        results = [{"url": "https://www.linkedin.com/in/youness-bellasri", "title": "", "snippet": "Manager at Inwi"}]
        self.run_once(results)
        self.assertEqual(self.rows()[0]["name"], "Youness Bellasri")

    def test_profile_already_stored_for_another_target_is_skipped_not_fatal(self):
        # Same person found for two targets (moved companies): the global unique
        # profile_url index must not abort the whole run for the second target.
        url = "https://www.linkedin.com/in/hajar-ghzala-94143315b"
        self.conn.execute(
            "INSERT INTO people_candidates (id, target_company_id, name, profile_url, created_at, updated_at)"
            " VALUES ('pc_old', 'tc_other', 'Hajar Ghzala', ?, 'x', 'x')", (url,))
        self.conn.commit()
        results = [
            {"url": url, "title": "Hajar Ghzala", "snippet": "Talent Acquisition at Inwi"},
            {"url": "https://www.linkedin.com/in/new-person", "title": "New Person", "snippet": "Manager at Inwi"},
        ]
        ids, _r, _s = self.run_once(results)
        names = sorted(r["name"] for r in self.rows())
        self.assertEqual(names, ["Hajar Ghzala", "New Person"])  # old row kept, new one inserted
        self.assertEqual(len(ids), 1)


if __name__ == "__main__":
    unittest.main()
