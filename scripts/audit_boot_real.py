"""Real boot time (no artificial waits) + proof coverage still loads on demand."""
import time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8786/pipeline_v2.html"

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    calls = []
    pg.on("request", lambda r: calls.append(r.url) if "/highlight" in r.url else None)

    t0 = time.time()
    pg.goto(BASE, wait_until="domcontentloaded")
    # Wait for the real signal: the overview stats populated.
    pg.wait_for_function(
        "() => document.querySelectorAll('#page-overview .stat, #page-overview .panel').length > 0",
        timeout=30000)
    print(f"time to overview content: {round(time.time()-t0, 2)}s")
    print(f"highlight calls on boot:  {len(calls)}")

    # Now actually go to the CV page and confirm coverage arrives.
    pg.evaluate("location.hash='#/cvs'")
    pg.wait_for_timeout(3500)
    print(f"highlight calls after visiting CVs: {len(calls)}")

    bars = pg.evaluate("document.querySelectorAll('#page-cvs .coverage-fill, #page-cvs .coverage').length")
    fallback = pg.evaluate(
        "Array.from(document.querySelectorAll('#page-cvs *')).filter(e => e.textContent.trim() === 'No coverage yet').length")
    print(f"coverage elements rendered: {bars}, 'No coverage yet' fallbacks: {fallback}")

    # Scrolling should pull in more, not everything at once.
    pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    pg.wait_for_timeout(2500)
    print(f"highlight calls after scrolling to bottom: {len(calls)}")
    b.close()
