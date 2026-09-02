import shutil, subprocess, sys, time, urllib.request
from playwright.sync_api import sync_playwright
tmp = "/path/to/AppData/Local/Temp/e2e_vis.sqlite3"; shutil.copy("career_pipeline_v2.sqlite3", tmp)
srv = subprocess.Popen([sys.executable, "migrate_pipeline_v2.py", "serve", "--db", tmp, "--port", "8791"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try: urllib.request.urlopen("http://127.0.0.1:8791/api/summary", timeout=1); break
        except Exception: time.sleep(0.5)
    with sync_playwright() as p:
        b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1460, "height": 760})
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto("http://127.0.0.1:8791/pipeline_v2.html#/opportunities"); pg.wait_for_selector("#opportunity-rows tr.opp-row", timeout=20000); pg.wait_for_timeout(1500)
        rowBefore = pg.evaluate("Math.round(document.querySelector('#opportunity-rows tr.opp-row').getBoundingClientRect().height)")
        pg.locator("#opportunity-rows .applied-inline > button").first.click(); pg.wait_for_timeout(600)
        print(pg.evaluate("""(rb) => {const p=document.querySelector('body > .confirm-panel:not([hidden])');
          if(!p) return {found:false};
          const b=p.getBoundingClientRect();const hit=document.elementFromPoint(Math.round(b.left+b.width/2),Math.round(b.top+b.height/2));
          return {found:true,rect:[Math.round(b.left),Math.round(b.top),Math.round(b.width),Math.round(b.height)],
                  panelIsHit:!!(hit&&p.contains(hit)),hitElement:hit?hit.tagName+'.'+hit.className:'none',
                  insideViewport:b.right<=innerWidth&&b.bottom<=innerHeight&&b.left>=0&&b.top>=0,
                  rowBefore:rb,rowAfter:Math.round(document.querySelector('#opportunity-rows tr.opp-row').getBoundingClientRect().height)};}""", rowBefore))
        pg.screenshot(path="/path/to/AppData/Local/Temp/pw_pop3.png")
        pg.locator("body > .confirm-panel input[type=checkbox]").check()
        pg.locator("body > .confirm-panel button:has-text('Record locally')").click(); pg.wait_for_timeout(1800)
        print("badge:", pg.locator("#opportunity-rows .apply-badge.is-applied").first.inner_text())
        print("panel closed after save:", pg.locator("body > .confirm-panel:not([hidden])").count() == 0)
        # outside click closes it
        pg.locator("#opportunity-rows .applied-inline > button").first.click(); pg.wait_for_timeout(400)
        pg.mouse.click(600, 200); pg.wait_for_timeout(400)
        print("closes on outside click:", pg.locator("body > .confirm-panel:not([hidden])").count() == 0)
        print("errors", errs); b.close()
finally:
    srv.terminate()
