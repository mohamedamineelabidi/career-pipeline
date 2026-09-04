"""Audit the Reach front on the live server: a11y structure, boot cost, contrast.

Everything is read from the rendered DOM. Exits 1 when a threshold fails so the
script can gate a commit. Also saves 1440px and 820px screenshots under
$LOCALAPPDATA/Temp for a human look.

Usage: uv run python scripts/audit_reach.py [base_url]
"""
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8786/reach.html"
ROUTES = ["targets", "people", "jobs", "runs"]


def scratch_dir():
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "Temp" if base else Path("/tmp")
    root.mkdir(parents=True, exist_ok=True)
    return root


def luminance(hex_color):
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(fg, bg):
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return round((hi + 0.05) / (lo + 0.05), 2)


out = {}
failures = []
api_requests = []

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.on("request", lambda r: api_requests.append(r.url) if "/api/" in r.url else None)
    page.goto(BASE + "#/people", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    out["boot_api_requests"] = len(api_requests)
    out["boot_api_urls"] = [u.split("/api/")[1] for u in api_requests]
    if len(api_requests) > 4:
        failures.append("boot makes more than 4 API requests")

    per_route = {}
    for route in ROUTES:
        page.evaluate(f"location.hash='#/{route}'")
        page.wait_for_timeout(900)
        checks = page.evaluate("""() => {
          const small = [];
          for (const el of document.querySelectorAll('button')) {
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            if (r.height < 24 || r.width < 24) small.push(el.innerText.trim().slice(0, 30) + ' [' + Math.round(r.width) + 'x' + Math.round(r.height) + ']');
          }
          const unlabelled = [];
          for (const el of document.querySelectorAll('input, select, textarea')) {
            if (el.offsetParent === null) continue;
            const hasLabel = el.id && document.querySelector(`label[for="${el.id}"]`);
            if (!hasLabel && !el.getAttribute('aria-label') && !el.getAttribute('aria-labelledby')) unlabelled.push(el.id || el.tagName);
          }
          return {
            small_buttons: small,
            unlabelled_inputs: unlabelled,
            images_no_alt: Array.from(document.images).filter(i => !i.alt).length,
            h1_count: document.querySelectorAll('h1').length,
            main_count: document.querySelectorAll('main').length,
            primary_buttons_in_view: document.querySelectorAll('.page.active .btn-primary').length,
            external_links_without_noopener: [...document.querySelectorAll('a[target=_blank]')].filter(a => !(a.rel || '').includes('noopener')).length,
            shadows: [...document.querySelectorAll('*')].filter(e => getComputedStyle(e).boxShadow !== 'none').length,
          };
        }""")
        per_route[route] = checks
        if checks["small_buttons"]:
            failures.append(f"{route}: buttons under 24px: {checks['small_buttons']}")
        if checks["unlabelled_inputs"]:
            failures.append(f"{route}: unlabelled inputs: {checks['unlabelled_inputs']}")
        if checks["images_no_alt"]:
            failures.append(f"{route}: images without alt")
        if checks["h1_count"] != 1 or checks["main_count"] != 1:
            failures.append(f"{route}: expected one h1 and one main")
        if checks["primary_buttons_in_view"] > 1:
            failures.append(f"{route}: more than one primary button")
        if checks["external_links_without_noopener"]:
            failures.append(f"{route}: external links without rel=noopener")
        if checks["shadows"]:
            failures.append(f"{route}: {checks['shadows']} elements use box-shadow")
    out["routes"] = per_route

    tokens = page.evaluate("""() => {
      const s = getComputedStyle(document.documentElement);
      const read = n => s.getPropertyValue(n).trim();
      return { primary: read('--color-primary'), secondary: read('--color-secondary'), link: read('--color-link'),
               surface: read('--color-surface'), raised: read('--color-surface-raised'), success: read('--color-success'),
               warning: read('--color-warning'), danger: read('--color-danger') };
    }""")
    ratios = {
        "body_on_surface": contrast(tokens["primary"], tokens["surface"]),
        "body_on_raised": contrast(tokens["primary"], tokens["raised"]),
        "secondary_on_surface": contrast(tokens["secondary"], tokens["surface"]),
        "secondary_on_raised": contrast(tokens["secondary"], tokens["raised"]),
        "badge_success_on_raised": contrast(tokens["success"], tokens["raised"]),
        "badge_warning_on_raised": contrast(tokens["warning"], tokens["raised"]),
        "badge_danger_on_raised": contrast(tokens["danger"], tokens["raised"]),
        "link_on_surface": contrast(tokens["link"], tokens["surface"]),
        "primary_button_text": contrast(tokens["surface"], tokens["primary"]),
    }
    out["contrast"] = ratios
    out["min_contrast"] = min(ratios.values())
    if out["min_contrast"] < 4.5:
        failures.append("a text contrast ratio is below 4.5")

    page.evaluate("location.hash='#/people'")
    page.wait_for_timeout(600)
    shots = {}
    wide = scratch_dir() / "reach_people_1440.png"
    page.screenshot(path=str(wide), full_page=True)
    shots["1440"] = str(wide)
    page.set_viewport_size({"width": 820, "height": 900})
    page.wait_for_timeout(500)
    out["narrow_overflow_px"] = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    if out["narrow_overflow_px"] > 0:
        failures.append("horizontal overflow at 820px")
    narrow = scratch_dir() / "reach_people_820.png"
    page.screenshot(path=str(narrow), full_page=True)
    shots["820"] = str(narrow)
    out["screenshots"] = shots
    browser.close()

print(json.dumps(out, indent=2))
if failures:
    print("\nFAILED:")
    for failure in failures:
        print(" -", failure)
    sys.exit(1)
print("\nPASS: reach audit thresholds met.")
