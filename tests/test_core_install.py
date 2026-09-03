"""A core install must run the whole workspace without the optional extras.

The single 1.3 GB dependency bucket (torch, playwright, rendercv) was the biggest
adoption blocker: most people abandon at `uv sync`. Core is now stdlib-plus-four,
and this test is what makes that claim honest rather than aspirational, by hiding
the heavy modules at import time and exercising the real server.
"""
import builtins
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from contextlib import closing
from pathlib import Path

import pipeline_v2

HEAVY_MODULES = (
    "torch", "sentence_transformers", "transformers", "sklearn", "scipy",
    "playwright", "rendercv", "typst", "jobspy", "pypdfium2", "pypdf",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class _HeavyImportsHidden:
    """Make the optional dependencies look uninstalled inside this block."""

    def __enter__(self):
        self._real = builtins.__import__

        def guard(name, *args, **kwargs):
            if name.split(".")[0] in HEAVY_MODULES:
                raise ImportError(f"simulated missing dependency: {name}")
            return self._real(name, *args, **kwargs)

        builtins.__import__ = guard
        return self

    def __exit__(self, *exc):
        builtins.__import__ = self._real
        return False


class CoreInstallTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.db = Path(self._dir.name) / "pipeline.sqlite3"
        pipeline_v2.create_schema(self.db)

    def test_core_modules_import_without_optional_extras(self):
        """Import the core modules in a clean subprocess with the extras hidden.

        Reloading them in-process poisons module state for every later test, so
        the isolation has to be a real process boundary.
        """
        import subprocess
        import sys
        program = (
            "import builtins,sys;"
            f"sys.path.insert(0,{str(PROJECT_ROOT)!r});"
            f"HEAVY={HEAVY_MODULES!r};"
            "real=builtins.__import__;"
            "builtins.__import__=lambda n,*a,**k: "
            "(_ for _ in ()).throw(ImportError(n)) if n.split('.')[0] in HEAVY "
            "else real(n,*a,**k);"
            "import pipeline_v2, semantic_match, profile_validator;"
            "print('ok')"
        )
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_server_serves_dashboard_and_api_without_optional_extras(self):
        with _HeavyImportsHidden():
            server = pipeline_v2.make_server(self.db, PROJECT_ROOT, port=8798)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.shutdown)

            for path in ("/pipeline_v2.html", "/api/summary",
                         "/api/opportunities", "/api/triage/next"):
                with urllib.request.urlopen(
                    f"http://127.0.0.1:8798{path}", timeout=10
                ) as response:
                    self.assertEqual(response.status, 200, path)

    def test_scoring_falls_back_when_embeddings_are_unavailable(self):
        """Without the ml extra, scoring must degrade rather than crash."""
        with _HeavyImportsHidden():
            import semantic_match
            score = semantic_match.similarity_to_score(0.5)
            self.assertEqual(score, 50)

    def test_pyproject_keeps_the_heavy_stack_out_of_core(self):
        import tomllib
        data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        core = " ".join(data["project"]["dependencies"]).lower()
        for heavy in ("torch", "sentence-transformers", "playwright",
                      "rendercv", "scikit-learn", "jobspy"):
            self.assertNotIn(heavy, core, f"{heavy} must be an optional extra")
        extras = data["project"]["optional-dependencies"]
        for name in ("ml", "cv", "browser", "all"):
            self.assertIn(name, extras)


if __name__ == "__main__":
    unittest.main()
