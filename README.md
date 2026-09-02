# Career Pipeline

A local-first job search workspace. It collects openings from public job feeds, scores them against an evidence-backed profile, drafts tailored CVs and outreach messages, and tracks every application on a board.

It runs entirely on `127.0.0.1`. There is no hosted service, no account, and no telemetry. **The application never submits anything on your behalf** — no Apply, no Send, no Connect. It prepares the work and links you to the real listing; you take every final action yourself.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Browser  ·  pipeline_v2.html  ·  single-file SPA, zero dependencies  │
│  Overview · Opportunities · Tracker · CVs · Contacts · Drafts ·       │
│  Funnel · Skill gaps · Insights · Guide                               │
└─────────────────────────────────┬─────────────────────────────────────┘
                                  │  fetch() over JSON
┌─────────────────────────────────┴─────────────────────────────────────┐
│  pipeline_v2.py  ·  http.server request router (~35 JSON endpoints)   │
│  optimistic concurrency (version + 409) · status transition guard     │
└─────┬───────────┬───────────┬────────────┬──────────┬─────────────────┘
      │           │           │            │          │
 ┌────┴────┐ ┌────┴────┐ ┌────┴─────┐ ┌────┴────┐ ┌───┴──────────┐
 │ ingest  │ │ scoring │ │ CV       │ │outreach │ │ analytics    │
 │ RSS,    │ │semantic │ │ render   │ │ drafts, │ │ funnel, KPIs │
 │ JobSpy, │ │ + LLM   │ │ RenderCV │ │sequences│ │ skill gaps   │
 │ paste   │ │ fallback│ │ → PDF    │ │(local)  │ │              │
 └────┬────┘ └────┬────┘ └────┬─────┘ └────┬────┘ └───┬──────────┘
      └───────────┴───────────┴────────────┴──────────┘
                                  │
              ┌───────────────────┴────────────────────┐
              │  SQLite  ·  career_pipeline_v2.sqlite3 │
              │  25 tables + FTS5 full-text index      │
              └────────────────────────────────────────┘
```

## What it does

| Stage | What happens |
|---|---|
| **Collect** | Pulls openings from RSS feeds, ATS boards (Greenhouse, Lever, Workable, Ashby) and JobSpy. Anything else can be pasted in by hand. Deduplicated on a content hash. |
| **Verify** | Fetches the real job description, checks the listing is still live, and records a confidence level. Nothing is scored on a title alone. |
| **Score** | A local sentence-transformer computes semantic fit against your profile and lists the skills you have and the ones you are missing. An optional LLM pass adds a second opinion; without a key the pipeline still works. |
| **Tailor** | Generates a one-page CV per opening through RenderCV, plus an ATS keyword highlight and a recruiter-style review with an improvement round. |
| **Draft** | Writes cover letters and outreach messages locally. Drafts stay in the database until you copy them out. |
| **Track** | A board moves each opening through `discovered → verified → eligible → shortlisted → applied by you → closed`, with a full timeline of state changes. |

## Quick start

```bash
git clone https://github.com/your-github-handle/career-pipeline.git
cd career-pipeline

uv sync                                  # or: pip install -e .
uv run playwright install chromium       # only needed for PDF preview

# Build your profile from the templates
cp reference_cv_2027/data/career_master.example.yaml       reference_cv_2027/data/career_master.yaml
cp reference_cv_2027/data/evidence_register.example.yaml   reference_cv_2027/data/evidence_register.yaml
cp reference_cv_2027/data/tailoring_knowledge.example.yaml reference_cv_2027/data/tailoring_knowledge.yaml
# edit those three files with your own facts

uv run python migrate_pipeline_v2.py init --db career_pipeline_v2.sqlite3
uv run python migrate_pipeline_v2.py serve --db career_pipeline_v2.sqlite3 --port 8786
```

Open <http://127.0.0.1:8786/pipeline_v2.html>. The **Guide** page inside the app explains every screen and the recommended daily routine.

To collect openings:

```bash
uv run python pipeline_runner.py --db career_pipeline_v2.sqlite3
```

### Optional LLM scoring

Copy `.env.example` to `.env` and set a key. Without it the pipeline degrades to local semantic scoring and everything else keeps working.

```
LLM_API_KEY=your-key-here
LLM_MODEL=openai/gpt-oss-120b
LLM_BASE_URL=https://api.groq.com/openai/v1
```

`.env` is gitignored. When LLM scoring is on, the job title, company and description are sent to that provider — the app tells you so in the UI.

## Your data stays yours

- `career_master.yaml`, `evidence_register.yaml` and `tailoring_knowledge.yaml` are **gitignored**. Only the `.example.yaml` templates are in the repository.
- The SQLite database, generated CVs, PDFs and screenshots are gitignored.
- No analytics, no crash reporting, no outbound call except the job sources you configure and the optional LLM endpoint.

## Design rules

1. **Never auto-submit.** There is no Apply, Send, Connect or Submit control anywhere in the interface. A test enforces this.
2. **Never invent a fact.** A CV bullet can only be used if it maps to evidenced content in your profile. Unproven claims are excluded, not softened.
3. **Say when something is missing.** Missing skills, stale listings and failed fetches are shown, not hidden.
4. **Offline-capable interface.** The frontend is one HTML file with no CDN, no build step and no framework. Text is written with `textContent`, never `innerHTML`.
5. **Respect the sources.** Rate-limited fetching, no login-wall bypass, no CAPTCHA solving.

## Documentation

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — modules, data flow, database schema, API surface
- [`CAREER_PIPELINE_V2.md`](CAREER_PIPELINE_V2.md) — product behaviour and workflow rules
- The **Guide** page in the running app — usage and daily routine

## Tests

```bash
uv run python -m pytest tests -q
```

24 test files cover ingestion, scoring, CV rendering, outreach state machines, the HTTP layer and the frontend safety rules.

## Stack

Python 3.11 standard library `http.server` · SQLite with FTS5 · sentence-transformers · scikit-learn · RenderCV + Typst · Jinja2 · feedparser · JobSpy · Playwright · vanilla JavaScript. No web framework, no ORM, no bundler.

## License

MIT
