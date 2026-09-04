"""Verify suspected defects: source fragmentation, duplicate jobs, dead fields, install weight."""
import json, re, sqlite3, subprocess, sys
from collections import Counter
import pathlib
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

c = sqlite3.connect(str(REPO_ROOT / "career_pipeline_v2.sqlite3")); c.row_factory = sqlite3.Row
q = lambda s: [dict(r) for r in c.execute(s)]

print("=== 1. SOURCE CASE FRAGMENTATION ===")
raw = Counter(str(r["source"] or "") for r in q("SELECT source FROM opportunities"))
norm = Counter()
for k, v in raw.items():
    key = re.sub(r"[^a-z]", "", k.lower())[:12] or "(empty)"
    norm[key] += v
print(f"  distinct source strings stored: {len(raw)}")
print(f"  distinct after normalizing:     {len(norm)}")
print("  worst collapses:")
for key, tot in norm.most_common(5):
    variants = [k for k in raw if re.sub(r'[^a-z]','',k.lower())[:12] == key]
    if len(variants) > 1:
        print(f"    {key}: {tot} rows across {len(variants)} spellings -> {variants[:4]}")

print("\n=== 2. DUPLICATE JOBS (same title+company, different id) ===")
d = q("""SELECT LOWER(TRIM(title)) t, LOWER(TRIM(company)) co, COUNT(*) n
         FROM opportunities GROUP BY t, co HAVING n > 1 ORDER BY n DESC""")
print(f"  duplicate groups: {len(d)}   extra rows: {sum(r['n']-1 for r in d)}")
for r in d[:5]: print(f"    {r['n']}x  {r['co'][:28]:30} {r['t'][:44]}")

print("\n=== 3. FIELDS THAT EXIST BUT ARE NEVER POPULATED ===")
cols = [r[1] for r in c.execute("PRAGMA table_info(opportunities)")]
total = q("SELECT COUNT(*) n FROM opportunities")[0]["n"]
dead = []
for col in cols:
    n = q(f'SELECT COUNT(*) n FROM opportunities WHERE "{col}" IS NOT NULL AND "{col}" != ""')[0]["n"]
    pct = 100 * n / total
    if pct < 5: dead.append((col, n, pct))
for col, n, pct in dead: print(f"    {col:26} {n:4}/{total}  ({pct:.1f}%)")

print("\n=== 4. STUCK PIPELINE ===")
for r in q("""SELECT status, COUNT(*) n,
             SUM(CASE WHEN description IS NULL OR description='' THEN 1 ELSE 0 END) nodesc
             FROM opportunities GROUP BY status"""):
    print(f"    {r['status']:18} {r['n']:4}  (missing description: {r['nodesc']})")
sem = q("SELECT COUNT(*) n FROM semantic_scores")[0]["n"]
print(f"    scored {sem}/{total} -> {total-sem} never scored")

print("\n=== 5. INSTALL WEIGHT (barrier to adoption) ===")
deps = json.loads(subprocess.run([sys.executable, "-c",
    "import json,tomllib;print(json.dumps(tomllib.load(open('/path/to/cv/pyproject.toml','rb'))['project']['dependencies']))"],
    capture_output=True, text=True).stdout or "[]")
print(f"    declared dependencies: {len(deps)}")
print("   ", ", ".join(deps))
