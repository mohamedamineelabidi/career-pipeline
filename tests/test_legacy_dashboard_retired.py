from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_legacy_dashboard_is_retired_to_pipeline_v2():
    source = (ROOT / "jobhunt.html").read_text(encoding="utf-8")
    assert "pipeline_v2.html" in source
    assert "innerHTML" not in source
    assert "jobs_digest.json" not in source
