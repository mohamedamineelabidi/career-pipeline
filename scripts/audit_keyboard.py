"""Keyboard reachability and drawer behaviour -- the parts a daily user touches."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.goto("http://127.0.0.1:8786/pipeline_v2.html", wait_until="domcontentloaded")
    pg.wait_for_timeout(3000)
    pg.evaluate("location.hash='#/opportunities'")
    pg.wait_for_timeout(2000)

    # How many tabs to reach the first data row action?
    pg.keyboard.press("Tab")
    seq = []
    for _ in range(14):
        info = pg.evaluate("""() => {
          const a = document.activeElement;
          if (!a) return 'none';
          return (a.tagName + ':' + (a.id || (a.innerText||'').trim().slice(0,20) || a.className.slice(0,18)));
        }""")
        seq.append(info)
        pg.keyboard.press("Tab")
    print("TAB ORDER (first 14):")
    for i, s in enumerate(seq, 1):
        print(f"  {i:2d}. {s}")

    # Is there a skip link?
    has_skip = pg.evaluate(
        "!!document.querySelector('a[href^=\"#\"][class*=skip], .skip-link')")
    print("\nskip-to-content link:", has_skip)

    # Drawer: open first row, check focus + escape
    pg.evaluate("document.querySelector('.opp-row').click()")
    pg.wait_for_timeout(900)
    print("drawer open:", pg.evaluate("!!document.querySelector('#drawer.open, #drawer[data-open=true]')"))
    print("focus inside drawer:", pg.evaluate(
        "!!document.querySelector('#drawer') && document.querySelector('#drawer').contains(document.activeElement)"))
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(600)
    print("closed on Escape:", not pg.evaluate("!!document.querySelector('#drawer.open, #drawer[data-open=true]')"))
    b.close()
