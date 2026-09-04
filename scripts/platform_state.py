"""Measure the real state of the platform: data health, funnel, and what is actually used."""
import json, sqlite3, urllib.request
from collections import Counter
import pathlib
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

DB = str(REPO_ROOT / "career_pipeline_v2.sqlite3")
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row

def q(sql):
    try: return [dict(r) for r in c.execute(sql)]
    except Exception as e: return [{"err": str(e)}]

print("=== OPPORTUNITY STATUS ===")
for r in q("SELECT status, COUNT(*) n FROM opportunities GROUP BY status ORDER BY n DESC"):
    print(f"  {r.get('status'):22} {r.get('n')}")

print("\n=== SOURCE ===")
for r in q("SELECT source, COUNT(*) n FROM opportunities GROUP BY source ORDER BY n DESC LIMIT 8"):
    print(f"  {str(r.get('source'))[:30]:32} {r.get('n')}")

print("\n=== DATA COMPLETENESS (of all opportunities) ===")
for r in q("""SELECT COUNT(*) total,
  SUM(CASE WHEN description IS NULL OR description='' THEN 1 ELSE 0 END) no_desc,
  SUM(CASE WHEN url IS NULL OR url='' THEN 1 ELSE 0 END) no_url,
  SUM(CASE WHEN salary_min IS NULL AND salary_max IS NULL THEN 1 ELSE 0 END) no_salary,
  SUM(CASE WHEN deadline IS NULL OR deadline='' THEN 1 ELSE 0 END) no_deadline
  FROM opportunities"""):
    print(" ", r)

print("\n=== SCORING COVERAGE ===")
for r in q("""SELECT (SELECT COUNT(*) FROM opportunities) opps,
  (SELECT COUNT(*) FROM semantic_scores) semantic,
  (SELECT COUNT(*) FROM llm_scores) llm,
  (SELECT COUNT(*) FROM cv_artifacts) cvs,
  (SELECT COUNT(*) FROM applications) applications,
  (SELECT COUNT(*) FROM drafts) drafts,
  (SELECT COUNT(*) FROM contacts) contacts"""):
    print(" ", r)

print("\n=== TABLE ROW COUNTS (what is actually used) ===")
tables = [r["name"] for r in q("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%'")]
empty = []
for t in sorted(tables):
    n = q(f"SELECT COUNT(*) n FROM {t}")[0].get("n", 0)
    if n == 0: empty.append(t)
    else: print(f"  {t:28} {n}")
print("  EMPTY TABLES:", ", ".join(empty) if empty else "none")

print("\n=== LAST PIPELINE RUNS ===")
for r in q("SELECT started_at, status FROM pipeline_runs ORDER BY started_at DESC LIMIT 5"):
    print(" ", r.get("started_at"), r.get("status"))
