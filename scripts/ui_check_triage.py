"""Verify the triage UI against the live DOM: does it render, and do the keys work?"""
import asyncio, json, sys
from playwright.async_api import async_playwright

URL = "http://127.0.0.1:8786/pipeline_v2.html#/triage"


async def main():
    out = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1200)

        out["page_active"] = await page.eval_on_selector(
            "#page-triage", "el => el.classList.contains('active')")
        out["card_visible"] = await page.is_visible("#triage-card")
        out["count"] = (await page.text_content("#triage-count") or "").strip()
        out["company"] = (await page.text_content("#triage-company") or "").strip()
        out["role"] = (await page.text_content("#triage-role") or "").strip()
        out["scores"] = (await page.text_content("#triage-scores") or "").strip()
        desc = await page.text_content("#triage-description") or ""
        out["description_chars"] = len(desc.strip())

        # No control on this page may apply or send.
        out["forbidden_controls"] = await page.eval_on_selector_all(
            "#page-triage button, #page-triage a",
            "els => els.map(e => e.textContent.trim()).filter(t => /apply|send|submit|connect/i.test(t))")

        # Press S: the served job must change (skip = 'not now').
        before = out["role"]
        await page.keyboard.press("s")
        await page.wait_for_timeout(1500)
        after = (await page.text_content("#triage-role") or "").strip()
        out["skip_advanced_queue"] = before != after
        out["role_after_skip"] = after
        out["count_after_skip"] = (await page.text_content("#triage-count") or "").strip()

        out["console_errors"] = errors[:5]
        await browser.close()
    print(json.dumps(out, indent=2, ensure_ascii=False))


asyncio.run(main())
