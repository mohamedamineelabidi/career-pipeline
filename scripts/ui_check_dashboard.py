"""Measure the live dashboard DOM. Screenshots prove nothing; computed style does.

Usage: uv run python scripts/ui_check_dashboard.py [--base http://127.0.0.1:8786]

The dashboard polls, so `networkidle` never settles: always wait for
`domcontentloaded` and then for the specific node under test.
"""
import argparse
import json
import sys

from playwright.sync_api import sync_playwright

FORBIDDEN = ("apply now", "send email", "send message", "connect", "submit application")


def check(base: str) -> dict:
    out: dict = {}
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(base + "/pipeline_v2.html", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # Design tokens actually applied
        out["font"] = page.evaluate(
            "getComputedStyle(document.body).fontFamily.split(',')[0].replace(/\"/g,'')"
        )
        out["panel_shadow_soft"] = page.evaluate(
            "!!document.querySelector('.panel') &&"
            " getComputedStyle(document.querySelector('.panel')).boxShadow !== 'none'"
        )

        # Opportunities: unified badge row
        page.goto(base + "/pipeline_v2.html#/opportunities", wait_until="domcontentloaded")
        page.wait_for_selector(".opp-row", timeout=20000)
        page.wait_for_timeout(1500)
        out["opp_rows"] = page.eval_on_selector_all(".opp-row", "els => els.length")
        out["badge_rows"] = page.eval_on_selector_all(".opp-table .badge-row", "els => els.length")
        out["thead_color"] = page.evaluate(
            "getComputedStyle(document.querySelector('.data-table thead th')).color"
        )
        out["sample_badges"] = page.eval_on_selector_all(
            ".opp-row:first-child .badge", "els => els.map(e => e.textContent)"
        )
        out["checkboxes"] = page.eval_on_selector_all(".opp-row .opp-check input", "e => e.length")
        out["link_icons"] = page.eval_on_selector_all(".opp-link-icon", "e => e.length")
        page.evaluate("[...document.querySelectorAll('.opp-row .opp-check input')].slice(0,3).forEach(b=>{b.checked=true;b.dispatchEvent(new Event('change'))})")
        page.wait_for_timeout(400)
        out["bulk_label"] = page.eval_on_selector("#opp-bulk-count", "e => e.textContent")
        out["bulk_visible"] = page.eval_on_selector("#opp-bulk-bar", "e => !e.hidden")

        body = (page.inner_text("body") or "").lower()
        out["forbidden_controls"] = [w for w in FORBIDDEN if w in body]
        out["console_errors"] = errors[:5]
        browser.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8786")
    args = ap.parse_args()
    result = check(args.base)
    print(json.dumps(result, indent=2))
    ok = (
        result["badge_rows"] > 0
        and result["opp_rows"] > 0
        and not result["forbidden_controls"]
        and not result["console_errors"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
