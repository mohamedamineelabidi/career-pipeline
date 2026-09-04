"""Measure what actually costs the user time: boot, DOM weight per page, interaction latency."""
import json, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8786/pipeline_v2.html"
out = {}

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900})

    t0 = time.time()
    pg.goto(BASE, wait_until="domcontentloaded")
    # The app boots to Overview; the big table lives on #/opportunities.
    pg.wait_for_timeout(6000)
    pg.evaluate("location.hash='#/opportunities'")
    pg.wait_for_selector(".opp-row", state="attached", timeout=30000)
    out["time_to_first_row_s"] = round(time.time() - t0, 2)

    pg.wait_for_timeout(4000)

    # Where do the nodes live?
    out["nodes_by_page"] = pg.evaluate("""() => {
      const o = {};
      for (const el of document.querySelectorAll('[id^="page-"]')) {
        o[el.id.replace('page-','')] = el.querySelectorAll('*').length;
      }
      return Object.fromEntries(Object.entries(o).sort((a,b) => b[1]-a[1]).slice(0,8));
    }""")

    out["opp_rows_in_dom"] = pg.evaluate("document.querySelectorAll('.opp-row').length")
    out["total_opportunities"] = pg.evaluate("(window.state && state.opportunities || []).length")

    # Typing latency in the big table's search box.
    t0 = time.time()
    pg.fill("#opportunity-search", "engineer")
    pg.wait_for_timeout(50)
    out["search_filter_ms"] = round((time.time() - t0) * 1000)
    pg.fill("#opportunity-search", "")
    pg.wait_for_timeout(500)

    # Cost of switching to the heaviest page.
    for route in ("cvs", "drafts", "insights"):
        t0 = time.time()
        pg.evaluate(f"location.hash='#/{route}'")
        pg.wait_for_timeout(1200)
        out[f"switch_{route}_s"] = round(time.time() - t0, 2)

    print(json.dumps(out, indent=2))
    b.close()
