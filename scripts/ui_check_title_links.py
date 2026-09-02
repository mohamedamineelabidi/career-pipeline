from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); ctx = b.new_context(viewport={"width": 1460, "height": 760}); pg = ctx.new_page()
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:8786/pipeline_v2.html#/opportunities"); pg.wait_for_selector("#opportunity-rows tr.opp-row", timeout=20000); pg.wait_for_timeout(800)
    info = pg.evaluate("""() => { const rows=[...document.querySelectorAll('#opportunity-rows tr.opp-row')]; return {links: rows.filter(r=>r.querySelector('a.link-button.primary')).length, buttons: rows.filter(r=>r.querySelector('button.link-button.primary')).length, first: (()=>{const a=document.querySelector('#opportunity-rows a.link-button.primary'); return a && {href:a.href, target:a.target, rel:a.rel};})()}; }""")
    print("opps title links:", info)
    with ctx.expect_page() as popup:
        pg.click("#opportunity-rows a.link-button.primary")
    new = popup.value; new.wait_for_load_state("domcontentloaded", timeout=15000); print("new tab:", new.url[:90]); new.close()
    pg.click("#opportunity-rows button.link-button.secondary"); pg.wait_for_timeout(800)
    print("company click -> drawer:", pg.evaluate("!document.getElementById('drawer').hidden"), "| drawer listing:", pg.evaluate("document.getElementById('drawer-listing').innerText"))
    pg.goto("http://127.0.0.1:8786/pipeline_v2.html#/tracker"); pg.wait_for_selector(".kcard", timeout=20000); pg.wait_for_timeout(500)
    print("tracker title links:", pg.evaluate("[document.querySelectorAll('.kcard a.kcard-title').length, document.querySelectorAll('.kcard button.kcard-title').length, document.querySelectorAll('.kcard').length]"))
    print("forbidden:", pg.evaluate("[...document.querySelectorAll('button,a')].map(e=>e.textContent.trim()).filter(x=>/\\b(send|apply|connect|submit)\\b/i.test(x))"))
    pg.screenshot(path="/path/to/AppData/Local/Temp/pw_tracker.png")
    print("errors", errs); b.close()
