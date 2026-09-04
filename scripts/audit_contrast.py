"""Contrast check + how the big table scales."""
import json
from playwright.sync_api import sync_playwright

def lum(c):
    def f(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])

def ratio(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return round((hi + 0.05) / (lo + 0.05), 2)

def parse(s):
    nums = [int(x) for x in s.replace("rgba(", "").replace("rgb(", "").replace(")", "").split(",")[:3]]
    return nums

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.goto("http://127.0.0.1:8786/pipeline_v2.html", wait_until="domcontentloaded")
    pg.wait_for_timeout(3000)

    samples = pg.evaluate("""() => {
      const out = [];
      const sels = ['.secondary', '.muted', '.badge', '.chart-label', 'th', 'body'];
      for (const s of sels) {
        const el = document.querySelector(s);
        if (!el) continue;
        const cs = getComputedStyle(el);
        let bg = cs.backgroundColor, n = el;
        while (bg === 'rgba(0, 0, 0, 0)' && n.parentElement) { n = n.parentElement; bg = getComputedStyle(n).backgroundColor; }
        out.push({sel: s, fg: cs.color, bg, size: cs.fontSize});
      }
      return out;
    }""")

    print("CONTRAST (WCAG AA needs 4.5 for body, 3.0 for >=18.66px bold/24px):")
    for s in samples:
        try:
            r = ratio(parse(s["fg"]), parse(s["bg"]))
            size = float(s["size"].replace("px", ""))
            need = 3.0 if size >= 24 else 4.5
            flag = "  <-- BELOW AA" if r < need else ""
            print(f"  {s['sel']:14s} {r:5.2f}:1  ({s['size']}){flag}")
        except Exception as e:
            print(f"  {s['sel']:14s} parse fail {e}")

    # Table scale
    pg.evaluate("location.hash='#/opportunities'")
    pg.wait_for_timeout(2000)
    info = pg.evaluate("""() => ({
      rows: document.querySelectorAll('.opp-row').length,
      pager: !!document.querySelector('[id*=page], .pager, [class*=paging]'),
      body_h: Math.round(document.body.scrollHeight)
    })""")
    print("\nTABLE:", json.dumps(info))
    b.close()
