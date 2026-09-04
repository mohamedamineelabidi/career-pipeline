"""Email finder: evidence first, observed patterns second, never a bare guess."""
import sqlite3
import unittest

import pipeline_v2
from reach import email_finder as ef


class ExtractEmailsTests(unittest.TestCase):
    def test_extract_emails_keeps_only_company_domain_and_ignores_generic(self):
        text = "Contact: hajar.ghzala@deloitte.com, careers@deloitte.com, me@gmail.com, image@2x.png"
        got = ef.extract_emails(text, domains={"deloitte.com"})
        self.assertEqual(got, ["hajar.ghzala@deloitte.com"])  # generic mailbox and other domains dropped

    def test_extract_handles_obfuscation(self):
        self.assertEqual(ef.extract_emails("h.ghzala [at] deloitte [dot] com", {"deloitte.com"}),
                         ["h.ghzala@deloitte.com"])
        self.assertEqual(ef.extract_emails("K.Akli (at) Deloitte (dot) COM", {"deloitte.com"}),
                         ["k.akli@deloitte.com"])

    def test_extract_dedupes_and_accepts_subdomains_of_allowed_domain(self):
        text = "a.b@ma.deloitte.com A.B@ma.deloitte.com c.d@deloitte.com"
        self.assertEqual(ef.extract_emails(text, {"deloitte.com"}),
                         ["a.b@ma.deloitte.com", "c.d@deloitte.com"])
        self.assertEqual(ef.extract_emails("x.y@deloitte.com", set()), [])


def make_conn():
    """In-memory DB with the real pipeline schema plus the Reach tables."""
    import migrate_pipeline_v2
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(pipeline_v2.SCHEMA)
    connection.executescript(migrate_pipeline_v2.REACH_SCHEMA)
    for column, ddl in migrate_pipeline_v2.PEOPLE_CANDIDATES_EXTRA_COLUMNS:
        connection.execute(f"ALTER TABLE people_candidates ADD COLUMN {column} {ddl}")
    return connection


class CompanyDomainsTests(unittest.TestCase):
    def test_company_domains_come_from_official_pages_and_existing_leads(self):
        conn = make_conn()
        # An existing lead at the same company already has an email route: its domain counts.
        conn.execute("INSERT INTO contacts (id, name, company, role, source_json, created_at, updated_at)"
                     " VALUES ('c1', 'X', 'Deloitte', '', '{}', 'x', 'x')")
        conn.execute("INSERT INTO contact_routes (id, contact_id, route_type, value)"
                     " VALUES ('r1', 'c1', 'email', 'x.y@deloitte.com')")
        conn.execute("INSERT INTO contact_routes (id, contact_id, route_type, value)"
                     " VALUES ('r2', 'c1', 'linkedin', 'https://www.linkedin.com/in/xy')")
        calls = []

        def search(query):
            calls.append(query)
            return [
                {"url": "https://www.linkedin.com/company/deloitte", "title": "Deloitte", "snippet": ""},
                {"url": "https://www2.deloitte.com/ma/fr/contact.html", "title": "Contact Deloitte Maroc", "snippet": ""},
                {"url": "https://www.glassdoor.com/Overview/deloitte", "title": "Deloitte reviews", "snippet": ""},
            ]

        domains = ef.company_domains(conn, "Deloitte", search_fn=search)
        self.assertEqual(domains, {"deloitte.com"})  # www2 stripped, deduped with the lead's domain
        self.assertEqual(len(calls), 1)
        self.assertNotIn("@", calls[0])

    def test_company_domains_ignores_other_companies_and_survives_search_failure(self):
        conn = make_conn()
        conn.execute("INSERT INTO contacts (id, name, company, role, source_json, created_at, updated_at)"
                     " VALUES ('c1', 'X', 'Orange Maroc', '', '{}', 'x', 'x')")
        conn.execute("INSERT INTO contact_routes (id, contact_id, route_type, value)"
                     " VALUES ('r1', 'c1', 'email', 'a.b@orange.ma')")
        conn.execute("INSERT INTO contacts (id, name, company, role, source_json, created_at, updated_at)"
                     " VALUES ('c2', 'Y', 'EY', '', '{}', 'x', 'x')")
        conn.execute("INSERT INTO contact_routes (id, contact_id, route_type, value)"
                     " VALUES ('r2', 'c2', 'email', 'y.z@ey.com')")

        def broken(query):
            raise RuntimeError("search down")

        self.assertEqual(ef.company_domains(conn, "orange", search_fn=broken), {"orange.ma"})
        self.assertEqual(ef.company_domains(conn, "Nobody Inc", search_fn=lambda q: []), set())


class PatternTests(unittest.TestCase):
    def test_pattern_needs_two_observations_at_same_domain(self):
        obs = ["hajar.ghzala@deloitte.com", "kenza.akli@deloitte.com"]
        people = [("Hajar", "Ghzala"), ("Kenza", "Akli")]
        self.assertEqual(ef.learn_pattern(obs, people), "{first}.{last}")
        self.assertIsNone(ef.learn_pattern(obs[:1], people[:1]))  # one sample is a guess, not a pattern

    def test_pattern_needs_eighty_percent_agreement(self):
        obs = ["a.b@x.com", "c.d@x.com", "efoo@x.com", "gbar@x.com", "ibaz@x.com"]
        people = [("A", "B"), ("C", "D"), ("E", "Foo"), ("G", "Bar"), ("I", "Baz")]
        self.assertIsNone(ef.learn_pattern(obs, people))  # 3/5 = 60% for {f}{last}
        self.assertEqual(ef.learn_pattern(obs[2:], people[2:]), "{f}{last}")

    def test_apply_pattern_normalises_accents_and_spaces(self):
        self.assertEqual(ef.apply_pattern("{first}.{last}", "Kaoutar", "MIHRAT", "orange.ma"),
                         "kaoutar.mihrat@orange.ma")
        self.assertEqual(ef.apply_pattern("{f}{last}", "Mélanie", "Benali", "deloitte.com"),
                         "mbenali@deloitte.com")
        self.assertEqual(ef.apply_pattern("{first}_{last}", "the candidate", "the candidate", "x.ma"),
                         "mohamedamine_elabidi@x.ma")

    def test_split_name_takes_last_token_as_last_name(self):
        self.assertEqual(ef.split_name("Hajar Ghzala"), ("Hajar", "Ghzala"))
        self.assertEqual(ef.split_name("the candidate"), ("the candidate", "the candidate"))
        self.assertEqual(ef.split_name("Cher"), ("Cher", ""))


class VerifyEmailTests(unittest.TestCase):
    def test_verify_reports_catch_all_and_does_not_trust_it(self):
        r = ef.verify_email("kenza.akli@deloitte.com",
                            mx_fn=lambda d: ["mx.deloitte.com"],
                            probe_fn=lambda host, addr: ef.Probe(True, True))
        self.assertTrue(r.mx_ok)
        self.assertTrue(r.smtp_ok)
        self.assertTrue(r.catch_all)
        self.assertEqual(r.verdict, "unverifiable_catch_all")

    def test_verify_accepted_and_rejected_by_rcpt(self):
        probe = lambda host, addr: ef.Probe(True, addr.startswith("kenza"))  # noqa: E731
        self.assertEqual(ef.verify_email("kenza.akli@deloitte.com", mx_fn=lambda d: ["mx"], probe_fn=probe).verdict, "accepted")
        r = ef.verify_email("nobody.zzz@deloitte.com", mx_fn=lambda d: ["mx"], probe_fn=probe)
        self.assertEqual(r.verdict, "rejected")
        self.assertFalse(r.catch_all)

    def test_verify_no_mx_or_no_smtp_is_unverifiable(self):
        r = ef.verify_email("a.b@nowhere.example", mx_fn=lambda d: [], probe_fn=lambda h, a: ef.Probe(True, True))
        self.assertFalse(r.mx_ok)
        self.assertEqual(r.verdict, "unverifiable_no_smtp")
        r = ef.verify_email("a.b@deloitte.com", mx_fn=lambda d: ["mx"], probe_fn=lambda h, a: ef.Probe(False, False))
        self.assertTrue(r.mx_ok)
        self.assertFalse(r.smtp_ok)
        self.assertEqual(r.verdict, "unverifiable_no_smtp")

    def test_verify_banner_rejection_is_unverifiable_not_rejected(self):
        # Measured: deloitte.com and orange.ma MX answer '554 ... rejected' before EHLO.
        def probe(host, addr):
            return ef.Probe(False, False, banner_rejected=True)
        r = ef.verify_email("a.b@deloitte.com", mx_fn=lambda d: ["mx"], probe_fn=probe)
        self.assertEqual(r.verdict, "unverifiable_smtp_rejected")

    def test_smtp_probe_parses_5xx_banner_without_network(self):
        class FakeSMTP:
            def __init__(self, host, port, timeout):
                self.host = host
            def connect(self, host, port):
                return 554, b"Your access to this mail system has been rejected"
            def quit(self):
                pass
        p = ef._smtp_probe("mx", "a@b.com", smtp_cls=FakeSMTP)
        self.assertTrue(p.banner_rejected)
        self.assertFalse(p.connected)

    def test_smtp_probe_accepts_rcpt_without_network(self):
        class FakeSMTP:
            def __init__(self, host, port, timeout):
                pass
            def connect(self, host, port):
                return 220, b"ok"
            def ehlo_or_helo_if_needed(self):
                pass
            def mail(self, sender):
                return 250, b"ok"
            def rcpt(self, addr):
                return (250, b"ok") if addr.startswith("kenza") else (550, b"no such user")
            def quit(self):
                pass
        self.assertTrue(ef._smtp_probe("mx", "kenza@b.com", smtp_cls=FakeSMTP).rcpt_ok)
        self.assertFalse(ef._smtp_probe("mx", "x@b.com", smtp_cls=FakeSMTP).rcpt_ok)


if __name__ == "__main__":
    unittest.main()
