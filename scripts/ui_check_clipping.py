import shutil, subprocess, sys, time, urllib.request
from playwright.sync_api import sync_playwright
tmp = "/path/to/AppData/Local/Temp/e2e_clip.sqlite3"; shutil.copy("career_pipeline_v2.sqlite3", tmp)
srv = subprocess.Popen([sys.executable, "migrate_pipeline_v2.py", "serve", "--db", tmp, "--port", "8794"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try: urllib.request.urlopen("http://127.0.0.1:8794/api/summary", timeout=1); break
        except Exception: time.sleep(0.5)
    with sync_playwright() as p:
        b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1460, "height": 760})
        pg.goto("http://127.0.0.1:8794/pipeline_v2.html#/opportunities"); pg.wait_for_selector("#opportunity-rows tr.opp-row", timeout=20000); pg.wait_for_timeout(1200)
        print(pg.evaluate("""(()=>{const r=document.querySelector('#opportunity-rows tr.opp-row');const cells=[...r.children];const ac=cells[8];
          const box=ac.getBoundingClientRect();
          const kids=[...ac.querySelectorAll('button,a')].map(e=>{const b=e.getBoundingClientRect();return {t:e.textContent.trim(),right:Math.round(b.right),clipped:e.scrollWidth>e.clientWidth+1};});
          const wrap=document.querySelector('.table-wrap')||document.querySelector('.opp-table').parentElement;
          const w=wrap.getBoundingClientRect();
          return {viewport:innerWidth,actionsRight:Math.round(box.right),wrapRight:Math.round(w.right),wrapScrollW:wrap.scrollWidth,wrapClientW:wrap.clientWidth,tableW:Math.round(document.querySelector('.opp-table').getBoundingClientRect().width),kids};})()"""))
        pg.locator("#opportunity-rows .applied-inline > button").first.click(); pg.wait_for_timeout(400)
        pg.screenshot(path="/path/to/AppData/Local/Temp/pw_panel.png")
        print(pg.evaluate("""(()=>{const p=document.querySelector('#opportunity-rows .confirm-panel');const b=p.getBoundingClientRect();
          return {visible:!p.hidden,right:Math.round(b.right),viewport:innerWidth,h:Math.round(b.height)};})()"""))
        b.close()
finally:
    srv.terminate()
