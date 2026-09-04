"""Regression: LinkedIn (and other login-walled boards) must never be sent to Jina."""
import unittest

import agent_reach_channel as channel


def failing_direct(url):
    raise ConnectionError("simulated network failure")


def jina_must_not_be_called(url):
    raise AssertionError(f"Jina fetcher was called for denylisted URL {url}")


class NoJinaForLinkedInTests(unittest.TestCase):
    def read(self, url):
        return channel.read_url(
            url, direct_fetcher=failing_direct, jina_fetcher=jina_must_not_be_called,
            sleep=lambda _s: None, clock=lambda: 0.0,
        )

    def test_linkedin_job_view_is_blocked_not_jina(self):
        result = self.read("https://www.linkedin.com/jobs/view/123")
        self.assertNotEqual(result["backend"], "jina")
        self.assertEqual(result["backend"], "blocked")
        self.assertEqual(result["text"], "")
        backends = [a["backend"] for a in result["attempts"] if a["status"] not in ("skipped_denylist",)]
        self.assertNotIn("jina", backends)
        self.assertIn({"backend": "jina", "status": "skipped_denylist"}, result["attempts"])

    def test_other_login_walled_boards_are_denied_too(self):
        for url in ("https://ma.indeed.com/viewjob?jk=abc", "https://www.glassdoor.com/job-listing/x"):
            self.assertTrue(channel.is_jina_denied(url), url)
            self.assertNotEqual(self.read(url)["backend"], "jina", url)

    def test_read_via_jina_refuses_linkedin_directly(self):
        status, text = channel.read_via_jina(
            "https://www.linkedin.com/jobs/view/123", fetcher=jina_must_not_be_called,
            sleep=lambda _s: None, clock=lambda: 0.0,
        )
        self.assertEqual((status, text), ("login_wall", ""))


if __name__ == "__main__":
    unittest.main()
