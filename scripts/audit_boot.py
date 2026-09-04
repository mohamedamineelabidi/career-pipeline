"""Count boot API calls and find N+1 fan-out patterns."""
import json, time, collections, re
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8786/pipeline_v2.html"
events = []

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900})

    pg.on("request", lambda r: events.append(("req", r.url, time.time())) if "/api/" in r.url else None)
    pg.on("response", lambda r: events.append(("res", r.url, time.time())) if "/api/" in r.url else None)

    t0 = time.time()
    pg.goto(BASE, wait_until="domcontentloaded")
    pg.wait_for_timeout(9000)

    reqs = [e for e in events if e[0] == "req"]
    print(f"total API requests during boot: {len(reqs)}")

    # Group by endpoint shape, collapsing ids.
    shapes = collections.Counter()
    for _, url, _ in reqs:
        path = url.split("/api/")[1].split("?")[0]
        shape = re.sub(r"opp_[a-f0-9]+", "<id>", path)
        shapes[shape] += 1
    print("\nendpoint shapes (count):")
    for shape, n in shapes.most_common(10):
        flag = "   <-- N+1 fan-out" if n > 3 else ""
        print(f"  {n:3d}x  {shape}{flag}")

    first = min(t for _, _, t in reqs)
    last = max(t for _, _, t in reqs)
    print(f"\nAPI activity window: {round((last-first)*1000)} ms")
    print(f"first request at:    {round((first-t0)*1000)} ms after goto")
    b.close()
