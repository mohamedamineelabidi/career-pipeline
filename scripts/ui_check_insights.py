import json
from playwright.sync_api import sync_playwright

errs = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.goto("http://127.0.0.1:8786/pipeline_v2.html", wait_until="domcontentloaded")
    pg.wait_for_timeout(3000)
    pg.evaluate("location.hash='#/insights'")
    pg.wait_for_selector(".chart", timeout=30000)
    pg.wait_for_timeout(1200)

    out = {}
    out["charts"] = pg.eval_on_selector_all(".chart", "e => e.length")
    out["chart_labels"] = pg.eval_on_selector_all(".chart", "e => e.map(c => c.getAttribute('aria-label'))")
    out["funnel_bars"] = pg.eval_on_selector_all(".funnel-fill", "e => e.length")
    out["heat_cells"] = pg.eval_on_selector_all(".heat-cell", "e => e.length")
    out["plot_dots"] = pg.eval_on_selector_all(".plot-dot", "e => e.length")
    out["far_dots"] = pg.eval_on_selector_all(".plot-dot.is-far", "e => e.length")
    out["tooltips"] = pg.eval_on_selector_all(".chart title", "e => e.length")
    out["sample_tooltip"] = pg.eval_on_selector(".funnel-fill ~ title, .chart title", "e => e.textContent")
    # Charts must have real geometry, not zero-width bars.
    out["widest_bar_px"] = pg.eval_on_selector_all(
        ".funnel-fill", "e => Math.round(Math.max(...e.map(r => r.getBoundingClientRect().width)))"
    )
    out["console_errors"] = errs[:5]
    print(json.dumps(out, indent=2))
    b.close()
