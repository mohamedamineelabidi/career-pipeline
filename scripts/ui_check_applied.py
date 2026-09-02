"""E2E: 'I applied' from the Opportunities table on a Discovered job, against a throwaway DB copy on port 8799."""
import shutil, subprocess, time, sys, urllib.request
from playwright.sync_api import sync_playwright
src = "career_pipeline_v2.sqlite3"; tmp = "/path/to/AppData/Local/Temp/e2e_applied.sqlite3"
shutil.copy(src, tmp)
srv = subprocess.Popen([sys.executable, "migrate_pipeline_v2.py", "serve", "--db", tmp, "--port", "8799"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try: urllib.request.urlopen("http://127.0.0.1:8799/api/summary", timeout=1); break
        except Exception: time.sleep(0.5)
    with sync_playwright() as p:
        b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1460, "height": 760})
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e))); pg.on("dialog", lambda d: d.accept())
        pg.goto("http://127.0.0.1:8799/pipeline_v2.html#/opportunities"); pg.wait_for_selector("#opportunity-rows tr.opp-row", timeout=20000); pg.wait_for_timeout(800)
        pg.select_option("#opportunity-status-filter", "discovered"); pg.wait_for_timeout(600)
        row = pg.locator("#opportunity-rows tr.opp-row").first
        title = row.locator("a.link-button.primary, button.link-button.primary").first.inner_text()
        print("job:", title); print("select options:", row.locator("select option").all_inner_texts())
        row.locator(".applied-inline > button").click(); pg.wait_for_timeout(200)
        panel = row.locator(".confirm-panel"); print("panel visible:", panel.is_visible())
        panel.locator("button:has-text('Record locally')").click(); pg.wait_for_timeout(600)
        print("without tick:", panel.locator(".status-line").inner_text())
        panel.locator("input[type=checkbox]").check(); panel.locator("button:has-text('Record locally')").click(); pg.wait_for_timeout(1500)
        pg.select_option("#opportunity-status-filter", "user_applied"); pg.wait_for_timeout(600)
        texts = pg.locator("#opportunity-rows tr.opp-row").all_inner_texts()
        print("applied filter rows:", len(texts), "| contains job:", any(title.split(' ')[0] in x for x in texts))
        print("applied note:", pg.locator("#opportunity-rows .applied-note").first.inner_text())
        pg.goto("http://127.0.0.1:8799/pipeline_v2.html#/tracker"); pg.wait_for_selector(".kcard", timeout=20000); pg.wait_for_timeout(500)
        print("tracker Applied column has job:", pg.evaluate("t => [...document.querySelectorAll('.kanban-col[data-status=user_applied] .kcard')].some(c => c.innerText.includes(t))", title.split(' ')[0]))
        pg.screenshot(path="/path/to/AppData/Local/Temp/pw_applied.png"); print("errors", errs); b.close()
finally:
    srv.terminate()
