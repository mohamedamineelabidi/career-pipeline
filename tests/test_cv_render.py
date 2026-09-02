import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

import yaml
from pypdf import PdfWriter

import cv_render

BASE_DOC = {
    "cv": {
        "name": "the candidate",
        "location": "Rabat, Morocco",
        "email": "you@example.com",
        "phone": "+351 912345678",
        "photo": "aminephoto.png",
        "social_networks": [{"network": "LinkedIn", "username": "your-linkedin-handle"}],
        "sections": {
            "Summary": ["Data & AI Engineer building RAG applications and data pipelines."],
            "Professional Experience": [
                {
                    "company": "Upfund",
                    "position": "Data & AI Engineer",
                    "location": "Paris, France, Remote",
                    "start_date": "2025-08",
                    "end_date": "2026-02",
                    "highlights": [
                        "Developed Angular research features connected to Vertex AI and FastAPI services on GCP.",
                        "**Skills & technologies:** Angular, GCP, Vertex AI, FastAPI",
                    ],
                }
            ],
            "Education": [
                {"institution": "ENSAH", "area": "Data Engineering", "degree": "Ing.",
                 "start_date": "2024-09", "end_date": "2027-06"}
            ],
        },
    },
    "design": {"theme": "sb2nov", "text": {"font_family": "XCharter"}},
}


def fake_runner_factory(pages: int, record: dict):
    def runner(yaml_path: Path, out_dir: Path, pdf_path: Path, timeout: int) -> None:
        record["yaml"] = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8"))
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=595, height=842)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        with open(pdf_path, "wb") as handle:
            writer.write(handle)
    return runner


class CvRenderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.yaml_path = self.root / "cv.yaml"
        self.yaml_path.write_text(yaml.safe_dump(BASE_DOC, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def test_render_enforces_one_column_theme_arial_no_photo_and_locale(self):
        record = {}
        result = cv_render.render_cv_yaml(
            self.yaml_path, self.root / "out", "fr", runner=fake_runner_factory(1, record)
        )
        doc = record["yaml"]
        self.assertNotIn("photo", doc["cv"])
        self.assertIn(doc["design"]["theme"], cv_render.ONE_COLUMN_THEMES)
        self.assertEqual(doc["design"]["text"]["font_family"], "Arial")
        self.assertEqual(doc["design"]["header"]["name_font_family"], "Arial")
        self.assertEqual(doc["locale"]["language"], "fr")
        self.assertEqual(doc["locale"]["present"], "présent")
        self.assertEqual(result["pages"], 1)
        self.assertTrue(Path(result["pdf_path"]).is_file())
        self.assertTrue(Path(result["text_path"]).is_file())
        self.assertEqual(result["language"], "fr")
        self.assertEqual(result["theme"], doc["design"]["theme"])

    def test_english_locale_default_and_invalid_language_rejected(self):
        record = {}
        cv_render.render_cv_yaml(self.yaml_path, self.root / "out", "en", runner=fake_runner_factory(1, record))
        self.assertEqual(record["yaml"]["locale"]["language"], "en")
        with self.assertRaises(cv_render.CvRenderError):
            cv_render.render_cv_yaml(self.yaml_path, self.root / "out", "de", runner=fake_runner_factory(1, {}))

    def test_two_pages_fail_unless_approved_exception(self):
        with self.assertRaises(cv_render.CvRenderError) as ctx:
            cv_render.render_cv_yaml(self.yaml_path, self.root / "out", "en", runner=fake_runner_factory(2, {}))
        self.assertIn("2 page", str(ctx.exception))
        result = cv_render.render_cv_yaml(
            self.yaml_path, self.root / "out", "en",
            approved_two_page_exception=True, runner=fake_runner_factory(2, {}),
        )
        self.assertEqual(result["pages"], 2)
        with self.assertRaises(cv_render.CvRenderError):
            cv_render.render_cv_yaml(
                self.yaml_path, self.root / "out", "en",
                approved_two_page_exception=True, runner=fake_runner_factory(3, {}),
            )

    def test_missing_pdf_from_renderer_is_an_error(self):
        def broken_runner(yaml_path, out_dir, pdf_path, timeout):
            return None

        with self.assertRaises(cv_render.CvRenderError):
            cv_render.render_cv_yaml(self.yaml_path, self.root / "out", "en", runner=broken_runner)

    def test_yaml_text_helper_collects_visible_strings(self):
        text = cv_render.yaml_visible_text(BASE_DOC)
        self.assertIn("Vertex AI", text)
        self.assertIn("Upfund", text)
        self.assertNotIn("aminephoto", text)

    @unittest.skipUnless(
        importlib.util.find_spec("rendercv") is not None and not os.environ.get("CV_RENDER_SKIP_REAL"),
        "rendercv not installed",
    )
    def test_real_rendercv_render_produces_one_page_pdf(self):
        result = cv_render.render_cv_yaml(self.yaml_path, self.root / "real", "en")
        self.assertEqual(result["pages"], 1)
        text = Path(result["text_path"]).read_text(encoding="utf-8")
        self.assertIn("Upfund", text)
        self.assertIn("+351", text)


if __name__ == "__main__":
    unittest.main()
