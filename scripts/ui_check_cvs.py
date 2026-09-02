"""Playwright UI check for the CV workspace: preview renders, generate keeps place."""
import json
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8786/pipeline_v2.html"
out = {}
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(BASE + "#/cvs")
    page.wait_for_selector("#cv-grid .panel[data-opportunity-id]", timeout=20000)
    page.evaluate("window.confirm = () => true")
    cards = page.locator("#cv-grid .panel[data-opportunity-id]")
    out["cards"] = cards.count()

    # 1. Preview PDF
    preview_btn = page.get_by_role("button", name="Preview PDF").first
    preview_btn.click()
    img = page.locator("#cv-grid img.pdf-frame").first
    img.wait_for(state="visible", timeout=20000)
    page.wait_for_function("el => el.complete && el.naturalWidth > 0", arg=img.element_handle(), timeout=20000)
    box = img.bounding_box()
    out["preview"] = {"natural_w": img.evaluate("el => el.naturalWidth"), "natural_h": img.evaluate("el => el.naturalHeight"),
                      "shown_w": round(box["width"]), "shown_h": round(box["height"]),
                      "note": page.locator("#cv-grid img.pdf-frame + div").first.text_content(),
                      "open_link": page.locator("#cv-grid a", has_text="Open PDF in a new tab").count()}
    page.screenshot(path="/path/to/AppData/Local/Temp/pw_preview.png")
    page.get_by_role("button", name="Hide preview").first.click()
    out["preview_hidden"] = page.locator("#cv-grid img.pdf-frame").count() == 0

    # 2. Generate CV on the 3rd card, after scrolling to it
    card = cards.nth(2)
    opp_id = card.get_attribute("data-opportunity-id")
    card.scroll_into_view_if_needed()
    scroll_before = page.evaluate("window.scrollY")
    card.get_by_role("button", name="Generate CV").first.click()
    t0 = time.time()
    status = page.locator(f'#cv-grid .panel[data-opportunity-id="{opp_id}"] .status-line').first
    status.wait_for(state="visible")
    page.wait_for_function(
        "id => { const c=document.querySelector(`#cv-grid .panel[data-opportunity-id=\"${id}\"]`); return c && /registered|failed|error/i.test(c.textContent); }",
        arg=opp_id, timeout=180000)
    out["generate_seconds"] = round(time.time() - t0, 1)
    fresh = page.locator(f'#cv-grid .panel[data-opportunity-id="{opp_id}"]')
    out["generate"] = {
        "status_text": fresh.locator(".status-line").first.text_content(),
        "same_card_exists": fresh.count() == 1,
        "buttons_after": fresh.locator("button").all_text_contents(),
        "flash": fresh.evaluate("el => el.classList.contains('flash')"),
        "in_viewport": fresh.evaluate("el => { const r = el.getBoundingClientRect(); return r.top >= 0 && r.top < innerHeight; }"),
        "focus_inside": fresh.evaluate("el => el.contains(document.activeElement) ? document.activeElement.textContent : null"),
        "hash": page.evaluate("location.hash"),
        "scroll_before": scroll_before, "scroll_after": page.evaluate("window.scrollY"),
    }
    page.screenshot(path="/path/to/AppData/Local/Temp/pw_generate.png")
    out["errors"] = errors
    browser.close()
print(json.dumps(out, indent=1, ensure_ascii=False))
