# LinkedIn people scan (read-only)

`reach/linkedin_people_scan.py` opens LinkedIn people search result pages in
your own logged-in Chrome, reads profile links and headline text from the DOM,
and stores them in `people_candidates` with `discovered_via='linkedin_logged_in'`
and `verification_status='unverified'`. Nothing else.

## Safety rules (verbatim, enforced in code and by `tests/test_linkedin_scan_static.py`)

1. HARD STOP and print a clear report if the URL contains 'checkpoint', 'captcha',
   'login', 'authwall' or 'uas/login'.
2. It must NEVER navigate to /messaging, /mynetwork, any URL containing 'compose'
   or 'invite', and NEVER click anything.
3. Only `https://www.linkedin.com/search/results/people/?keywords=<q>&origin=GLOBAL_SEARCH_HEADER`
   pages are opened, one per query, with a 4-5 second pause after each load.
4. Only `a[href*="/in/"]` hrefs and their visible headline text are collected,
   deduplicated by href.
5. No message, connection request, follow or reaction is ever sent. The script
   reads the DOM and writes to the local sqlite database only.
6. The browser helpers are looked up at runtime; importing the file in plain
   Python does nothing and `python reach/linkedin_people_scan.py` only prints usage.

Run it rarely (a handful of queries per day) and stop for the day if it reports a
HARD STOP.

## Prerequisites

- The target company exists in `target_companies` (see `reach/DESIGN.md`).
- You are logged in to LinkedIn in the Chrome that browser_exec drives.
- `CAREER_PIPELINE_DB` points to the pipeline sqlite file, or the default
  `pipeline_v2.sqlite3` in the repo root is used.

## Exact browser_exec invocation

Ask Hermes to run this through the `browser_exec` tool (the `code` argument is
executed in the harness where `new_tab`, `goto_url`, `wait_for_load`, `js` and
`page_info` exist):

```python
# Reading LinkedIn people results for one target company
import sys
path = "/path/to/cv/reach/linkedin_people_scan.py"
ns = dict(globals())          # carries new_tab, goto_url, wait_for_load, js, page_info
ns["__name__"] = "reach_scan"  # keeps the __main__ guard quiet
ns["__file__"] = path
exec(compile(open(path, encoding="utf-8").read(), path, "exec"), ns)
ns["main"](["--target", "Inwi", "--limit", "10", "--db", "/path/to/cv/pipeline_v2.sqlite3"])
```

The script is executed inside a namespace that already contains the browser
helpers, so `helpers()` finds them via `globals()` and the scan runs. Outside the
harness the same `main()` prints usage and exits with code 2.

## After the scan

Candidates stay `unverified`. Score them with `reach/scoring.py`, confirm the
current role by hand (set `current_role_confirmed_at`), and only then call
`promote()`. Drafts are created with `reach/drafts.py` and are never sent by code.
