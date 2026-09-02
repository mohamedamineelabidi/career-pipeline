import shutil, subprocess, sys, time, urllib.request
from playwright.sync_api import sync_playwright
tmp = "/path/to/AppData/Local/Temp/e2e_pop.sqlite3"; shutil.copy("career_pipeline_v2.sqlite3", tmp)
srv = subprocess.Popen([sys.executable, "migrate_pipeline_v2.py", "serve", "--db", tmp, "--port", "8793"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try: urllib.request.urlopen("http://127.0.0.1:8793/api/summary", timeout=1); break
        except Exception: time.sleep(0.5)
    with sync_playwright() as p:
        b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1460, "height": 760})
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e))); pg.on("dialog", lambda d: d.accept())
        pg.goto("http://127.0.0.1:8793/pipeline_v2.html#/opportunities"); pg.wait_for_selector("#opportunity-rows tr.opp-row", timeout=20000); pg.wait_for_timeout(1200)
        before = pg.evaluate("Math.round(document.querySelector('#opportunity-rows tr.opp-row').getBoundingClientRect().height)")
        pg.locator("#opportunity-rows .applied-inline > button").first.click(); pg.wait_for_timeout(400)
        print(pg.evaluate("""(b=>{const r=document.querySelector('#opportunity-rows tr.opp-row');const p=document.querySelector('#opportunity-rows .confirm-panel');
          const pb=p.getBoundingClientRect();const wrap=document.querySelector('.opp-table').parentElement;const wb=wrap.getBoundingClientRect();
          const cs=getComputedStyle(wrap);
          return {rowBefore:b,rowAfter:Math.round(r.getBoundingClientRect().height),rowGrew:Math.round(r.getBoundingClientRect().height)>b,
                  panelBottom:Math.round(pb.bottom),wrapBottom:Math.round(wb.bottom),wrapOverflow:cs.overflow+'/'+cs.overflowY,
                  panelInsideViewport:pb.right<=innerWidth&&pb.bottom<=innerHeight};})""", before))
        pg.screenshot(path="/path/to/AppData/Local/Temp/pw_pop.png")
        # full flow still works
        pg.evaluate("""() => {const p=document.querySelector('#opportunity-rows .confirm-panel');
          const c=p.querySelector('input[type=checkbox]'); c.checked=true; c.dispatchEvent(new Event('change',{bubbles:true}));
          [...p.querySelectorAll('button')].find(b=>b.textContent.includes('Record locally')).click();}"""); pg.wait_for_timeout(1800)
        print("applied badge:", pg.locator("#opportunity-rows .apply-badge.is-applied").first.inner_text(), "| errors", errs)
        b.close()
finally:
    srv.terminate()
