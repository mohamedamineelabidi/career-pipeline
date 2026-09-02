import shutil, subprocess, sys, time, urllib.request
from playwright.sync_api import sync_playwright
tmp = "/path/to/AppData/Local/Temp/e2e_stab.sqlite3"; shutil.copy("career_pipeline_v2.sqlite3", tmp)
srv = subprocess.Popen([sys.executable, "migrate_pipeline_v2.py", "serve", "--db", tmp, "--port", "8792"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try: urllib.request.urlopen("http://127.0.0.1:8792/api/summary", timeout=1); break
        except Exception: time.sleep(0.5)
    with sync_playwright() as p:
        b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1460, "height": 760})
        pg.goto("http://127.0.0.1:8792/pipeline_v2.html#/opportunities"); pg.wait_for_selector("#opportunity-rows tr.opp-row", timeout=20000); pg.wait_for_timeout(1500)
        pg.locator("#opportunity-rows .applied-inline > button").first.click()
        for delay in (100, 600, 1500, 3000, 6000):
            pg.wait_for_timeout(delay if delay == 100 else delay - prev if False else 0)
            time.sleep(0)
        prev = 0
        for t_ms in (100, 600, 1500, 3000, 6000):
            pg.wait_for_timeout(t_ms - prev); prev = t_ms
            print(t_ms, "ms ->", pg.evaluate("!!document.querySelector('#opportunity-rows .confirm-panel:not([hidden])')"))
            if t_ms == 600:
                pg.screenshot(path="/path/to/AppData/Local/Temp/pw_pop2.png")
        b.close()
finally:
    srv.terminate()
