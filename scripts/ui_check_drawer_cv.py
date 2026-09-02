"""Playwright check: drawer CV tab (generate, preview, next steps) + CV page title opens drawer."""
import json
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8786/pipeline_v2.html"
out = {}
with sync_playwright() as p:
    b = p.chromium.launch(); page = b.new_page(viewport={"width": 1400, "height": 900})
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    bad = []
    page.on("response", lambda r: bad.append(r.url.split("8786")[1]) if r.status >= 400 and "/highlight" not in r.url else None)
    page.goto(BASE + "#/cvs"); page.wait_for_selector("#cv-grid .panel[data-opportunity-id]", timeout=20000)
    page.evaluate("window.confirm = () => true")
    # click title of a card WITHOUT cv (Document: No CV generated yet)
    card = page.locator("#cv-grid .panel[data-opportunity-id]", has_text="No CV generated yet").first
    opp = card.get_attribute("data-opportunity-id")
    card.locator("h3 button.link-button").click()
    page.wait_for_selector("#drawer:not([hidden])")
    out["drawer_tab_selected"] = page.locator("#drawer-tabs button[aria-selected=true]").text_content()
    out["tabs"] = page.locator("#drawer-tabs button").all_text_contents()
    body = page.locator("#drawer-body")
    body.locator(".next-steps").wait_for()
    out["steps_before"] = body.locator(".next-steps li").all_text_contents()
    body.get_by_role("button", name="Generate CV").click()
    page.wait_for_function("() => [...document.querelectorAll?[]:document.querySelectorAll('#drawer-body button')].some(b => b.textContent==='Hide preview')", timeout=120000)
    img = body.locator("img.pdf-frame")
    page.wait_for_function("el => el.complete && el.naturalWidth > 0", arg=img.element_handle(), timeout=30000)
    out["after_generate"] = {
        "steps": body.locator(".next-steps li").all_text_contents(),
        "done_count": body.locator(".next-steps li.done").count(),
        "buttons": body.locator("button, a.compact-button").all_text_contents(),
        "img_natural": [img.evaluate("e=>e.naturalWidth"), img.evaluate("e=>e.naturalHeight")],
        "drawer_title": page.locator("#drawer-title").text_content(),
        "hash": page.evaluate("location.hash"),
    }
    page.screenshot(path="/path/to/AppData/Local/Temp/pw_drawer_cv.png")
    body.get_by_role("button", name="Next: prepare the form").click()
    page.wait_for_timeout(1500)
    out["prep_tab"] = page.locator("#drawer-tabs button[aria-selected=true]").text_content()
    out["forbidden"] = page.evaluate("[...document.querySelectorAll('button,a')].map(e=>e.textContent.trim()).filter(t=>/^(Send|Apply|Connect|Submit)$/i.test(t))")
    out["errors"] = errs; out["http_errors"] = bad
    b.close()
print(json.dumps(out, indent=1, ensure_ascii=False))
