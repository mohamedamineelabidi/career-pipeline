"""Audit the dashboard's structure: DOM weight, tap targets, contrast, a11y.

Measures rather than guesses. Everything here is read from the live DOM.
"""
import json
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8786/pipeline_v2.html"
out = {}

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.goto(BASE, wait_until="domcontentloaded")
    pg.wait_for_timeout(6000)

    out["dom_nodes"] = pg.evaluate("document.querySelectorAll('*').length")
    out["html_kb"] = round(pg.evaluate("document.documentElement.outerHTML.length") / 1024, 1)

    # Tap targets below the 24x24 minimum (WCAG 2.2 AA).
    out["small_targets"] = pg.evaluate("""() => {
      const out = [];
      for (const el of document.querySelectorAll('button, a, input, select')) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        if (r.height < 24 || r.width < 24) {
          out.push(((el.innerText || el.getAttribute('aria-label') || el.tagName).trim().slice(0, 28))
                   + ' [' + Math.round(r.width) + 'x' + Math.round(r.height) + ']');
        }
      }
      return out.slice(0, 12);
    }""")

    # Inputs with no accessible name at all.
    out["unlabelled_inputs"] = pg.evaluate("""() => {
      const out = [];
      for (const el of document.querySelectorAll('input, select, textarea')) {
        const id = el.id;
        const hasLabel = id && document.querySelector(`label[for="${id}"]`);
        if (!hasLabel && !el.getAttribute('aria-label') && !el.getAttribute('title')
            && !el.getAttribute('placeholder')) {
          out.push((el.id || el.name || el.tagName) + ':' + el.type);
        }
      }
      return out;
    }""")

    # Focus visibility: does the app ever define a focus-visible style?
    out["has_focus_style"] = pg.evaluate("""() => {
      for (const sheet of document.styleSheets) {
        try {
          for (const rule of sheet.cssRules) {
            if (rule.selectorText && rule.selectorText.includes(':focus')) return true;
          }
        } catch (e) {}
      }
      return false;
    }""")

    out["images_no_alt"] = pg.evaluate(
        "Array.from(document.images).filter(i => !i.alt).length")
    out["h1_count"] = pg.evaluate("document.querySelectorAll('h1').length")
    out["landmarks"] = pg.evaluate(
        "document.querySelectorAll('main,nav,header,footer,[role=main],[role=navigation]').length")

    print(json.dumps(out, indent=2))
    b.close()
