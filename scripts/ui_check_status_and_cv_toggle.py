import re
"""Verify the new Status cell (badge + stage select + I applied) and the CV Plan/Preview toggle."""
import shutil, subprocess, sys, time, urllib.request
from playwright.sync_api import sync_playwright
tmp = "/path/to/AppData/Local/Temp/e2e_ui.sqlite3"; shutil.copy("career_pipeline_v2.sqlite3", tmp)
srv = subprocess.Popen([sys.executable, "migrate_pipeline_v2.py", "serve", "--db", tmp, "--port", "8797"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try: urllib.request.urlopen("http://127.0.0.1:8797/api/summary", timeout=1); break
        except Exception: time.sleep(0.5)
    with sync_playwright() as p:
        b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1460, "height": 760})
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e))); pg.on("dialog", lambda d: d.accept())
        pg.goto("http://127.0.0.1:8797/pipeline_v2.html#/opportunities"); pg.wait_for_selector("#opportunity-rows tr.opp-row", timeout=20000); pg.wait_for_timeout(900)
        print("badges:", pg.locator(".apply-badge").count(), "| first:", pg.locator(".apply-badge").first.inner_text())
        print("I applied buttons:", pg.locator(".applied-inline > button").count())
        print("status cell height:", pg.evaluate("document.querySelector('.opp-status').getBoundingClientRect().height"))
        row = pg.locator("#opportunity-rows tr.opp-row").first
        row.locator(".applied-inline > button").click(); pg.wait_for_timeout(400)
        pg.locator("body > .confirm-panel input[type=checkbox]").check()
        pg.locator("body > .confirm-panel button:has-text('Record locally')").click(); pg.wait_for_timeout(1500)
        print("after apply badge:", pg.locator("#opportunity-rows .apply-badge.is-applied").first.inner_text())
        print("button gone on applied row:", pg.locator("#opportunity-rows tr.opp-row").first.locator(".applied-inline").count() == 0)
        pg.screenshot(path="/path/to/AppData/Local/Temp/pw_status.png")
        # CV tab toggle: pick an opportunity that already has a CV
        pg.goto("http://127.0.0.1:8797/pipeline_v2.html#/cvs"); pg.wait_for_timeout(1500)
        pg.goto("http://127.0.0.1:8797/pipeline_v2.html#/opportunities"); pg.wait_for_selector("#opportunity-rows tr.opp-row", timeout=20000); pg.wait_for_timeout(800)
        cvbtn = pg.locator("#opportunity-rows a:has-text('View PDF')").first
        row2 = cvbtn.locator("xpath=ancestor::tr")
        row2.locator("button:has-text('Details')").click(); pg.wait_for_timeout(1200)
        pg.locator("#drawer button").filter(has_text=re.compile(r"^CV$")).first.click(); pg.wait_for_timeout(2500)
        print("segment buttons:", pg.locator("#drawer .segment-button").all_inner_texts())
        print("active:", pg.locator("#drawer .segment-button.is-active").inner_text())
        pg.locator("#drawer .segment-button:has-text('Preview')").click(); pg.wait_for_timeout(2500)
        print("preview img visible:", pg.locator("#drawer img.pdf-frame").is_visible(), "| plan hidden:", pg.locator("#drawer .cv-pane").first.is_hidden())
        pg.screenshot(path="/path/to/AppData/Local/Temp/pw_cvtoggle.png")
        print("errors", errs); b.close()
finally:
    srv.terminate()
