import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import pipeline_v2
import rss_sources

from test_opportunity_filters_and_descriptions import _seed

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Jobs</title>
<item><title>Acme: Data Engineer</title><link>https://boards.test/rss/1</link>
<description>&lt;p&gt;Build &lt;b&gt;pipelines&lt;/b&gt;&lt;/p&gt;&lt;p&gt;Remote&lt;/p&gt;</description>
<pubDate>Mon, 31 Aug 2026 10:00:00 GMT</pubDate><category>Remote</category></item>
<item><title>ML Scientist at Beta</title><link>https://boards.test/rss/2</link><description>Train models</description></item>
<item><title>Existing job</title><link>https://boards.test/existing</link><description>old</description></item>
<item><title>No link</title><description>x</description></item>
</channel></rss>"""

ASHBY = json.dumps({"jobs": [
    {"title": "AI Engineer", "jobUrl": "https://jobs.ashbyhq.com/mistral/abc", "descriptionHtml": "<p>Do AI</p>",
     "publishedAt": "2026-08-30T12:00:00.000Z", "location": "Paris", "department": "Research", "isListed": True},
    {"title": "Hidden", "jobUrl": "https://jobs.ashbyhq.com/mistral/hidden", "isListed": False},
]}).encode()

FEEDS = [
    {"name": "Test RSS", "url": "https://feeds.test/rss", "type": "rss"},
    {"name": "Ashby - Mistral AI", "url": "https://api.ashbyhq.com/posting-api/job-board/mistral", "type": "ashby_json"},
]


def http(mapping):
    calls = []

    def get(url):
        calls.append(url)
        v = mapping[url]
        if isinstance(v, Exception):
            raise v
        return v
    get.calls = calls
    return get


class ParseTests(unittest.TestCase):
    def test_parse_rss_extracts_fields(self):
        items = rss_sources.parse_rss(RSS, FEEDS[0])
        self.assertEqual(len(items), 3)
        first = items[0]
        self.assertEqual((first["title"], first["company"]), ("Data Engineer", "Acme"))
        self.assertEqual(first["publication_date"], "2026-08-31")
        self.assertIn("Build pipelines", first["description"])
        self.assertNotIn("<", first["description"])
        self.assertEqual(first["location"], "Remote")
        self.assertEqual((items[1]["title"], items[1]["company"]), ("ML Scientist", "Beta"))

    def test_parse_ashby_skips_unlisted(self):
        items = rss_sources.parse_ashby_json(ASHBY, FEEDS[1])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["company"], "Mistral AI")
        self.assertEqual(items[0]["publication_date"], "2026-08-30")
        self.assertEqual(items[0]["description"], "Do AI")


class UpsertTests(unittest.TestCase):
    def seeded(self, directory):
        db = Path(directory) / "p.sqlite3"
        _seed(db, [{"id": "opp-existing", "url": "https://boards.test/existing", "status": "shortlisted",
                    "description": "keep me", "source_json": json.dumps({"jd_fetch_status": "ok"})}])
        return db

    def test_inserts_new_only_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            db = self.seeded(d)
            get = http({FEEDS[0]["url"]: RSS, FEEDS[1]["url"]: ASHBY})
            slept = []
            summary = rss_sources.run(db, feeds=FEEDS, http_get=get, sleep=slept.append)
            self.assertEqual(summary["new"], 3)
            self.assertEqual(summary["skipped_existing"], 1)
            self.assertEqual(summary["new_by_source"], {"rss:boards.test": 2, "rss:jobs.ashbyhq.com": 1})
            self.assertEqual(slept, [rss_sources.FEED_SLEEP_SECONDS])
            with closing(pipeline_v2.connect(db)) as c:
                rows = {r["url"]: dict(r) for r in c.execute("SELECT * FROM opportunities")}
            self.assertEqual(len(rows), 4)
            new = rows["https://boards.test/rss/1"]
            self.assertEqual(new["source"], "rss:boards.test")
            self.assertEqual(new["status"], "discovered")
            self.assertEqual(new["publication_date"], "2026-08-31")
            self.assertIn("Build pipelines", new["description"])
            src = json.loads(new["source_json"])
            self.assertEqual(src["rss_feed"], "Test RSS")
            self.assertEqual(rows["https://jobs.ashbyhq.com/mistral/abc"]["company"], "Mistral AI")
            existing = rows["https://boards.test/existing"]
            self.assertEqual(existing["status"], "shortlisted")
            self.assertEqual(existing["description"], "keep me")
            self.assertEqual(existing["updated_at"], "2026-01-01T00:00:00+00:00")
            # second run: nothing new, nothing touched
            summary2 = rss_sources.run(db, feeds=FEEDS, http_get=get, sleep=lambda s: None)
            self.assertEqual(summary2["new"], 0)
            self.assertEqual(summary2["skipped_existing"], 4)
            with closing(pipeline_v2.connect(db)) as c:
                rows2 = {r["url"]: dict(r) for r in c.execute("SELECT * FROM opportunities")}
            self.assertEqual(rows, rows2)

    def test_dry_run_limit_and_feed_error(self):
        with tempfile.TemporaryDirectory() as d:
            db = self.seeded(d)
            get = http({FEEDS[0]["url"]: RSS, FEEDS[1]["url"]: OSError("down")})
            summary = rss_sources.run(db, feeds=FEEDS, http_get=get, sleep=lambda s: None, dry_run=True)
            self.assertEqual(summary["new"], 2)
            self.assertIn(FEEDS[1]["url"], summary["feed_errors"])
            with closing(pipeline_v2.connect(db)) as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 1)
            summary = rss_sources.run(db, feeds=FEEDS, http_get=http({FEEDS[0]["url"]: RSS, FEEDS[1]["url"]: ASHBY}),
                                      sleep=lambda s: None, limit=1)
            self.assertEqual(summary["new"], 1)
            with closing(pipeline_v2.connect(db)) as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 2)

    def test_keyword_filter(self):
        feed = dict(FEEDS[0], keyword_filter=["data"])
        with tempfile.TemporaryDirectory() as d:
            db = self.seeded(d)
            summary = rss_sources.run(db, feeds=[feed], http_get=http({feed["url"]: RSS}), sleep=lambda s: None)
            self.assertEqual(summary["new"], 1)
            self.assertEqual(summary["filtered"], 2)

    def test_default_feeds_file_loads(self):
        feeds = rss_sources.load_feeds(rss_sources.DEFAULT_FEEDS)
        self.assertGreaterEqual(len(feeds), 5)
        self.assertTrue(all(f["url"].startswith("https://") for f in feeds))
        self.assertTrue(all(f.get("type", "rss") in rss_sources.PARSERS for f in feeds))
