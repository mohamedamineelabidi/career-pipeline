import re, shutil, subprocess, sys, time, urllib.request
from playwright.sync_api import sync_playwright
tmp = "/path/to/AppData/Local/Temp/e2e_shot.sqlite3"; shutil.copy("career_pipeline_v2.sqlite3", tmp)
srv = subprocess.Popen([sys.executable, "migrate_pipeline_v2.py", "serve", "--db", tmp, "--port", "8795"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try: urllib.request.urlopen("http://127.0.0.1:8795/api/summary", timeout=1); break
        except Exception: time.sleep(0.5)
    with sync_playwright() as p:
        b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1460, "height": 760})
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto("http://127.0.0.1:8795/pipeline_v2.html#/opportunities"); pg.wait_for_selector("#opportunity-rows tr.opp-row", timeout=20000); pg.wait_for_timeout(1200)
        print(pg.evaluate("""(()=>{const r=document.querySelector('#opportunity-rows tr.opp-row');const cells=[...r.children];const st=cells[7],ac=cells[8];
          const badge=st.querySelector('.apply-badge span');
          return {statusW:Math.round(st.getBoundingClientRect().width),actionsW:Math.round(ac.getBoundingClientRect().width),
                  badgeClipped:badge.scrollWidth>badge.clientWidth+1,badgeText:badge.textContent,
                  actionsOverflow:[...ac.querySelectorAll('button,a')].some(e=>e.getBoundingClientRect().right>ac.getBoundingClientRect().right+1),
                  rowH:Math.round(r.getBoundingClientRect().height)};})()"""))
        pg.screenshot(path="/path/to/AppData/Local/Temp/pw_status2.png")
        print("errors", errs); b.close()
finally:
    srv.terminate()
