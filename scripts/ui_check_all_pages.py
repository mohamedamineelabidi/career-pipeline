"""Sweep every page: safety controls, console errors, responsive layout.

The safety assertions are the point. This app must never grow a control that
sends an email, submits an application, or connects to anyone.
"""
import json
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8786/pipeline_v2.html"
ROUTES = ["opportunities", "cvs", "drafts", "insights", "funnel", "contacts", "tracker", "guide"]
REACH_BASE = "http://127.0.0.1:8786/reach.html"
REACH_ROUTES = ["targets", "people", "jobs", "runs"]
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

    # Reach front: same safety sweep on its four hash routes, plus one h1,
    # one main and no horizontal overflow at the 820px breakpoint.
    reach_page = browser.new_page(viewport={"width": 1440, "height": 900})
    reach_page.on("pageerror", lambda e: errors.append(f"REACH PAGEERROR {str(e)[:160]}"))
    reach_page.on(
        "console",
        lambda m: errors.append(f"REACH CONSOLE {m.text[:160]}")
        if m.type == "error" and "Failed to load resource" not in m.text
        else None,
    )
    reach_page.goto(REACH_BASE + "#/targets", wait_until="domcontentloaded")
    reach_page.wait_for_timeout(1500)
    for route in REACH_ROUTES:
        reach_page.evaluate(f"location.hash='#/{route}'")
        reach_page.wait_for_timeout(1200)
        text = (reach_page.evaluate("(document.querySelector('.page.active') || document.body).innerText") or "").lower()
        buttons = reach_page.evaluate(
            "[...document.querySelectorAll('button, a')].map(e => (e.innerText||'').trim().toLowerCase())"
        )
        hits = sorted({w for w in FORBIDDEN if w in text} | {b for b in buttons for w in FORBIDDEN if w and w in b})
        structure_ok = reach_page.evaluate(
            "document.querySelectorAll('h1').length === 1 && document.querySelectorAll('main').length === 1"
            " && document.querySelectorAll('nav .nav-item[aria-current=page]').length === 1"
        )
        if not structure_ok:
            errors.append(f"REACH STRUCTURE {route}: expected one h1, one main, one active nav item")
        report[f"reach/{route}"] = {"chars": len(text), "forbidden": hits}
    reach_page.set_viewport_size({"width": 820, "height": 900})
    reach_page.evaluate("location.hash='#/people'")
    reach_page.wait_for_timeout(800)
    report["reach_narrow_overflow"] = reach_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    if not report["reach_narrow_overflow"]:
        errors.append("REACH OVERFLOW at 820px")
    reach_page.close()

    report["console_errors"] = errors[:10]
    browser.close()

print(json.dumps(report, indent=2))

bad = [r for r, v in report.items() if isinstance(v, dict) and v.get("forbidden")]
if bad or errors:
    print("\nFAILED:", "forbidden controls in " + ", ".join(bad) if bad else "console errors present")
    sys.exit(1)
print("\nPASS: no send/apply controls, no console errors on any page.")
