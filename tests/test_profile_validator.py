"""The profile must fail loudly, with a fix, when it is wrong.

This is the bug that silently broke cover letters: career_master used
evidence_status 'confirmed', which is not in ACCEPTED_EVIDENCE_STATUSES, so
evidence loading yielded zero facts and generation failed with an opaque HTTP 400.
A newcomer writing their first profile has no way to guess the valid values.
"""
import unittest

import profile_validator


class ProfileValidatorTests(unittest.TestCase):
    def test_valid_profile_reports_no_errors(self):
        report = profile_validator.validate({
            "experiences": [{
                "title": "Data Engineer", "company": "Acme",
                "evidence_status": "user_confirmed",
                "bullets": ["Built a pipeline."],
            }],
        })
        self.assertEqual(report["errors"], [])
        self.assertTrue(report["ok"])

    def test_invalid_evidence_status_names_the_valid_values(self):
        report = profile_validator.validate({
            "experiences": [{
                "title": "Data Engineer", "company": "Acme",
                "evidence_status": "confirmed",
                "bullets": ["Built a pipeline."],
            }],
        })
        self.assertFalse(report["ok"])
        message = " ".join(report["errors"])
        self.assertIn("confirmed", message)
        self.assertIn("user_confirmed", message)
        self.assertIn("experiences[0]", message)

    def test_missing_required_field_is_located(self):
        report = profile_validator.validate({
            "experiences": [{"company": "Acme", "evidence_status": "verified"}],
        })
        self.assertFalse(report["ok"])
        self.assertIn("experiences[0].title", " ".join(report["errors"]))

    def test_profile_with_no_usable_facts_is_an_error_not_silence(self):
        """The exact failure mode that produced an opaque HTTP 400."""
        report = profile_validator.validate({"experiences": []})
        self.assertFalse(report["ok"])
        self.assertIn("no facts", " ".join(report["errors"]).lower())

    def test_every_error_is_actionable(self):
        report = profile_validator.validate({
            "experiences": [{"title": "X", "company": "Y", "evidence_status": "nope"}],
        })
        for error in report["errors"]:
            self.assertRegex(error, r"(expected|add|use|one of)",
                             f"error is not actionable: {error}")


if __name__ == "__main__":
    unittest.main()
