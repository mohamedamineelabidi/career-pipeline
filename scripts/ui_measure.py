"""Measure Tracker + Opportunities at the user's real viewport (1827x947 CSS px at 125% = ~1460x760)."""
import json, sys
from playwright.sync_api import sync_playwright

W, H = int(sys.argv[1]) if len(sys.argv) > 1 else 1460, int(sys.argv[2]) if len(sys.argv) > 2 else 760
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": W, "height": H})
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:8786/pipeline_v2.html#/tracker"); pg.wait_for_timeout(6000)
    r = pg.evaluate("""() => { const cols=[...document.querySelectorAll('.kanban-col')]; return cols.map(c=>({head:c.querySelector('.kanban-col-head').textContent.trim(), cards:c.querySelectorAll('.kcard').length, skel:c.querySelectorAll('.skeleton').length, w:Math.round(c.getBoundingClientRect().width), right:Math.round(c.getBoundingClientRect().right), cardH: c.querySelector('.kcard')? Math.round(c.querySelector('.kcard').getBoundingClientRect().height):null, cardScrollH: c.querySelector('.kcard')? c.querySelector('.kcard').scrollHeight:null}))}""")
    print("tracker", json.dumps(r))
    print("vw", pg.evaluate("innerWidth"), "docW", pg.evaluate("document.documentElement.scrollWidth"), "docH", pg.evaluate("document.documentElement.scrollHeight"))
    print(pg.evaluate("() => { const k=document.querySelector('.kcard'); if(!k) return null; const cs=getComputedStyle(k); return {h:cs.height, maxH:cs.maxHeight, ov:cs.overflow, board: getComputedStyle(document.getElementById('tracker-board')).gridTemplateColumns, boardH: getComputedStyle(document.getElementById('tracker-board')).height}}"))
    pg.screenshot(path="/path/to/AppData/Local/Temp/pw_tracker.png")
    pg.goto("http://127.0.0.1:8786/pipeline_v2.html#/opportunities"); pg.wait_for_selector("#opportunity-rows tr.opp-row", timeout=20000); pg.wait_for_timeout(1000)
    print("opps", pg.evaluate("() => { const w=document.querySelector('.opp-wrap'); const rows=[...document.querySelectorAll('#opportunity-rows tr.opp-row')]; const wr=w.getBoundingClientRect(); return {wrapW:w.clientWidth, tableW:w.scrollWidth, wrapH: Math.round(wr.height), mainW: document.querySelector('main').clientWidth, visible: rows.filter(r=>{const b=r.getBoundingClientRect(); return b.top>=wr.top && b.bottom<=wr.bottom;}).length, rowH: Math.round(rows[0].getBoundingClientRect().height)}}"))
    pg.screenshot(path="/path/to/AppData/Local/Temp/pw_opps.png")
    print("errors", errs); b.close()
