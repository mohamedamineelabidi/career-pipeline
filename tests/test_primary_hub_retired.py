from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_primary_hub_routes_to_pipeline_v2_after_migration():
    source = (ROOT / "hub.html").read_text(encoding="utf-8")
    assert "pipeline_v2.html" in source
    assert "jobs_digest.json" not in source
    assert "innerHTML" not in source
