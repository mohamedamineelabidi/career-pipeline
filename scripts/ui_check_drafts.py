import json
from playwright.sync_api import sync_playwright

errs = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.goto("http://127.0.0.1:8786/pipeline_v2.html#/drafts", wait_until="domcontentloaded")
    pg.wait_for_selector(".draft-row", timeout=25000)
    pg.wait_for_timeout(1200)

    out = {}
    out["rows"] = pg.eval_on_selector_all(".draft-row", "e => e.length")
    out["active_on_load"] = pg.eval_on_selector_all(".draft-row.is-active", "e => e.length")
    out["detail_heading"] = pg.eval_on_selector(".draft-detail h3", "e => e.textContent")
    out["body_chars"] = pg.eval_on_selector(".draft-body", "e => e.textContent.length")
    out["placeholder_chips"] = pg.eval_on_selector_all(".draft-placeholder", "e => e.length")

    # Click the third draft: the detail pane must follow.
    pg.eval_on_selector_all(".draft-row", "els => els[2].click()")
    pg.wait_for_timeout(600)
    out["heading_after_click"] = pg.eval_on_selector(".draft-detail h3", "e => e.textContent")
    out["switched"] = out["heading_after_click"] != out["detail_heading"]

    # Status must NOT change from copying.
    out["status_before_copy"] = pg.eval_on_selector(".draft-detail select", "e => e.value")
    pg.eval_on_selector_all(".draft-controls button", "els => els[els.length-1].click()")
    pg.wait_for_timeout(700)
    out["status_after_copy"] = pg.eval_on_selector(".draft-detail select", "e => e.value")
    out["status_unchanged"] = out["status_before_copy"] == out["status_after_copy"]

    page_text = pg.eval_on_selector("#page-drafts", "e => e.innerText")
    out["forbidden"] = [w for w in ("Send draft", "Send email", "Apply now", "Submit") if w in page_text]
    out["console_errors"] = errs[:5]
    print(json.dumps(out, indent=2))
    b.close()
