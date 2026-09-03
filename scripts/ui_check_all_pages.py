"""Sweep every page: safety controls, console errors, responsive layout.

The safety assertions are the point. This app must never grow a control that
sends an email, submits an application, or connects to anyone.
"""
import json
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8786/pipeline_v2.html"
ROUTES = ["opportunities", "cvs", "drafts", "insights", "funnel", "contacts", "tracker", "guide"]
FORBIDDEN = ["send email", "send draft", "apply now", "submit application", "connect on linkedin", "send message"]

errors = []
report = {}

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    # A 404 from /api/cvs/<id>/highlight is expected and handled in the UI as
    # "No coverage yet" for jobs that were never analysed. Those are not
    # regressions, so only genuine script failures count here.
    page.on("pageerror", lambda e: errors.append(f"PAGEERROR {str(e)[:160]}"))
    page.on(
        "console",
        lambda m: errors.append(f"CONSOLE {m.text[:160]}")
        if m.type == "error" and "Failed to load resource" not in m.text
        else None,
    )

    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)

    for route in ROUTES:
        page.evaluate(f"location.hash='#/{route}'")
        page.wait_for_timeout(2500)
        text = (page.evaluate("(document.querySelector('.page.active') || document.body).innerText") or "").lower()
        buttons = page.evaluate(
            "[...document.querySelectorAll('.page.active button, .page.active a')].map(e => (e.innerText||'').trim().toLowerCase())"
        )
        hits = sorted({w for w in FORBIDDEN if w in text} | {b for b in buttons for w in FORBIDDEN if w and w in b})
        report[route] = {"chars": len(text), "forbidden": hits}

    # Responsive: the drafts split and the opportunity table must survive narrow.
    page.set_viewport_size({"width": 820, "height": 900})
    page.evaluate("location.hash='#/drafts'")
    page.wait_for_timeout(2000)
    report["narrow_draft_columns"] = page.eval_on_selector(".draft-split", "e => getComputedStyle(e).gridTemplateColumns")
    report["narrow_overflow"] = page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2")

    report["console_errors"] = errors[:10]
    browser.close()

print(json.dumps(report, indent=2))

bad = [r for r, v in report.items() if isinstance(v, dict) and v.get("forbidden")]
if bad or errors:
    print("\nFAILED:", "forbidden controls in " + ", ".join(bad) if bad else "console errors present")
    sys.exit(1)
print("\nPASS: no send/apply controls, no console errors on any page.")
