# Architecture

Career Pipeline is a single-process Python application with a browser front end and a SQLite database. Everything runs on `127.0.0.1`. There is no framework, no ORM, no bundler and no build step — the whole system is Python standard library plus a small set of libraries that do real work (embeddings, PDF rendering, feed parsing).

---

## 1. Why it is built this way

| Decision | Reason |
|---|---|
| `http.server` instead of FastAPI/Flask | One user, one machine. No need for ASGI, workers or a reverse proxy. Zero framework surface to keep patched. |
| SQLite instead of Postgres | The data is personal and single-writer. A file you can copy, back up and inspect with any SQLite viewer. FTS5 gives full-text search for free. |
| One HTML file instead of React | The interface must work offline, load instantly and never depend on a CDN. No build step means the file you edit is the file that runs. |
| Local embeddings before LLM calls | Scoring must work with no API key, no network and no cost. The LLM is a second opinion, never a dependency. |
| Evidence files as YAML | Career facts are reviewed by a human. YAML diffs cleanly in git and is readable without tooling. |

---

## 2. Layers

```
Presentation   pipeline_v2.html            single-file SPA, hash router, 10 pages
      │
      │  fetch() · JSON · optimistic concurrency via version fields
      ▼
Transport      pipeline_v2.py              request router, validation, error mapping
      │
      ▼
Domain         semantic_match, llm_scoring, cv_render, recruiter_agent,
               outreach_sequences, application_tracker, analytics, keyword_highlight
      │
      ▼
Ingestion      job_sources, rss_sources, fetch_job_descriptions, paste_import
      │
      ▼
Persistence    SQLite (25 tables + FTS5)   migrate_pipeline_v2.py owns the schema
```

---

## 3. Modules

### Transport and orchestration

| Module | Lines | Responsibility |
|---|---:|---|
| `pipeline_v2.py` | 2005 | HTTP router, JSON serialization, status-transition guard, optimistic concurrency, static file serving. |
| `migrate_pipeline_v2.py` | 200 | Schema creation and migration, `init` and `serve` commands. Single source of truth for the database shape. |
| `pipeline_runner.py` | 310 | Runs the full collect → verify → score cycle as one job and records the outcome in `pipeline_runs`. |

### Ingestion

| Module | Lines | Responsibility |
|---|---:|---|
| `job_sources.py` | 425 | ATS board adapters (Greenhouse, Lever, Workable, Ashby) and JobSpy integration. |
| `rss_sources.py` | 319 | RSS/Atom feed parsing and normalization into the common opportunity shape. |
| `fetch_job_descriptions.py` | 402 | Fetches the real description, detects login walls and dead listings, records verification confidence. |
| `paste_import.py` | 156 | Parses a pasted job posting into a structured opportunity. |

### Scoring

| Module | Lines | Responsibility |
|---|---:|---|
| `semantic_match.py` | 604 | Sentence-transformer embeddings of profile vs. description; produces a fit score plus skills-have / skills-missing. Runs fully offline. |
| `llm_scoring.py` | 322 | Optional LLM fit assessment with strict JSON validation and retry. Degrades silently when no key is configured. |
| `llm_client.py` | 112 | Thin OpenAI-compatible client. Reads config from the environment only — no key ever touches the database. |

### CV and application

| Module | Lines | Responsibility |
|---|---:|---|
| `cv_render.py` | 212 | Renders a tailored CV to PDF through RenderCV/Typst. |
| `cv_workspace.py` | 247 | Manages generated CV artifacts and their lifecycle on disk. |
| `recruiter_agent.py` | 1255 | Recruiter-style review of a rendered CV: ATS score, findings, and an improvement round that produces a revised YAML. |
| `keyword_highlight.py` | 479 | Maps job-description keywords onto the CV to show coverage and gaps. |
| `application_prep.py` | 324 | Assembles everything needed to apply: CV, letter, ATS form field values. Prepares only — never submits. |
| `application_tracker.py` | 173 | Board state: which opening sits in which column, with its CV and fit data. |

### Outreach and analysis

| Module | Lines | Responsibility |
|---|---:|---|
| `outreach_sequences.py` | 647 | Multi-step outreach state machine, follow-up scheduling, and the `applied` confirmation path. Every send is manual. |
| `cover_letter.py` | 260 | Evidence-linked cover letter drafting. |
| `agent_reach_channel.py` | 117 | Contact channel resolution and verification status. |
| `interview_prep.py` | 233 | Generates interview preparation material per opening. |
| `analytics.py` | 504 | Funnel conversion, KPI summaries, skill-gap aggregation across all openings. |

### Front end

`pipeline_v2.html` (2529 lines) is a single file containing markup, CSS and JavaScript. A hash router drives ten pages: Overview, Opportunities, Tracker, CVs, Contacts, Drafts, Funnel, Skill gaps, Insights, Guide. All rendering uses `textContent` and `createElement`; there is no `innerHTML`, no inline event handler and no external asset.

---

## 4. Data flow

```
 job feeds / ATS boards / manual paste
        │
        ▼
 [ingest]  normalize → content_hash → dedupe → INSERT opportunities (status=discovered)
        │
        ▼
 [verify]  fetch description → live? login wall? → verification_confidence
        │                                          status=verified_active
        ▼
 [score]   semantic_match  → semantic_scores (score, skills_have, skills_missing)
           llm_scoring     → llm_scores (fit, payload) ......... optional
        │
        ▼
 [rank]    priority_score + eligibility_status → status=eligible
        │
        ▼
 [tailor]  cv_render → cv_artifacts (PDF)
           recruiter_agent → recruiter_reviews → cv_improvement_rounds
           keyword_highlight → coverage report
        │
        ▼
 [draft]   cover_letter / outreach_sequences → drafts, outreach_steps  (local only)
        │
        ▼
 [apply]   ── you open the real listing and apply yourself ──
           confirmed action → applications (applied_at) + lifecycle_events
        │
        ▼
 [analyse] analytics → funnel conversion, skill gaps, KPIs
```

Each stage writes its own table and never overwrites the previous stage's output, so a re-score never destroys a CV and a re-fetch never rewrites your application history.

---

## 5. Database

SQLite, 25 tables plus an FTS5 virtual index. Schema owned entirely by `migrate_pipeline_v2.py`.

### Core

| Table | Columns | Purpose |
|---|---:|---|
| `opportunities` | 32 | The central record: title, company, location, url, source, description, requirements, deadline, salary range, job type, remote flag, `content_hash` for dedupe, plus scoring and status fields. |
| `opportunities_fts` | 3 | FTS5 index over title, company and description for instant search. |
| `applications` | 8 | One row per opening you applied to: status, `applied_at`, linked CV artifact, notes. |
| `lifecycle_events` | 7 | Append-only audit trail of every status change, including `confirmed_by_user`. |
| `metadata` | 3 | Schema version and runtime state. |

### Scoring

| Table | Columns | Purpose |
|---|---:|---|
| `semantic_scores` | 7 | Local embedding score, skills have/missing, `content_hash` so a score is invalidated when the description changes. |
| `llm_scores` | 5 | Optional LLM fit, model name and raw payload. |

### CV and review

| Table | Columns | Purpose |
|---|---:|---|
| `cv_artifacts` | 5 | Generated CV files with type and label. |
| `recruiter_reviews` | 8 | ATS score, recommendation and structured findings. |
| `cv_improvement_rounds` | 9 | Before/after ATS score with the edits applied in that round. |
| `interview_preps` | 3 | Generated preparation payload per opening. |
| `application_preps` | 9 | ATS form field values prepared for a manual submission. |

### Contacts and outreach

| Table | Columns | Purpose |
|---|---:|---|
| `contacts` | 7 | People associated with a company. |
| `contact_routes` | 5 | Channels for a contact, each with a verification flag. |
| `drafts` | 11 | Local message drafts with channel, subject, body and status. |
| `cover_letter_drafts` | 7 | Cover letters with the evidence ids they were built from. |
| `outreach_sequences` | 9 | Multi-step sequence state per contact/opening. |
| `outreach_steps` | 12 | Individual steps with due date, body and state. |
| `outreach_events` | 8 | Recorded outreach actions. |

### Runs

| Table | Columns | Purpose |
|---|---:|---|
| `pipeline_runs` | 7 | Each full pipeline execution with stage results, log and digest. |
| `automation_runs` | 6 | Individual automation executions with status and timing. |

---

## 6. API surface

All endpoints return JSON and are served from the same process as the HTML.

**Read**

```
GET  /api/summary                     dashboard counters
GET  /api/opportunities               filter, sort, paginate
GET  /api/opportunities/{id}          single record with scores
GET  /api/search                      FTS5 search
GET  /api/tracker                     board columns
GET  /api/tracker/timeline/{id}       lifecycle history
GET  /api/analytics/summary           KPIs
GET  /api/funnel                      conversion by stage
GET  /api/match/gaps                  aggregated skill gaps
GET  /api/cvs                         generated CVs
GET  /api/cvs/{id}/pdf                CV file
GET  /api/cvs/{id}/preview.png        first-page image
GET  /api/cvs/{id}/highlight          ATS keyword coverage
GET  /api/contacts                    contacts and routes
GET  /api/drafts                      local drafts
GET  /api/outreach/due                follow-ups due
GET  /api/pipeline/latest             last run
GET  /api/pipeline/runs/{id}          run detail
GET  /api/applications/preps          prepared applications
```

**Write**

```
POST  /api/pipeline/run               run collect → verify → score
POST  /api/opportunities/paste        import a pasted posting
PATCH /api/opportunities/{id}         status change (version-checked)
POST  /api/opportunities/{id}/applied confirm you applied yourself
POST  /api/match/recompute            re-run semantic scoring
POST  /api/llm-score/recompute        re-run LLM scoring
POST  /api/cvs/generate               tailor a CV
POST  /api/recruiter/review           review a CV
POST  /api/recruiter/improve          run an improvement round
POST  /api/cover-letters/generate     draft a cover letter
POST  /api/interview/generate         build interview prep
POST  /api/applications/prepare       prepare an application
POST  /api/tracker/move               move a card
POST  /api/outreach/sequences         create a sequence
PATCH /api/outreach/steps/{id}        update a step
POST  /api/outcomes                   record an outcome
```

There is no endpoint that submits an application, sends an email, or contacts anyone. That is a deliberate architectural boundary, not an omission.

---

## 7. Concurrency and safety

**Optimistic concurrency.** Every mutating request carries the record's `version`. If the stored version moved on, the server answers `409` and the interface asks you to reload rather than silently overwriting.

**Transition guard.** Status changes are validated against an explicit transition table. `user_applied` is reachable from any open stage — you may apply at any point — but always requires an explicit confirmation flag. `closed` is terminal.

**Confirmation required.** Recording an application needs `confirmed: true` from a deliberate user action. The database keeps `confirmed_by_user` on the lifecycle event.

**Front-end rules enforced by tests.** No `innerHTML`, no inline handlers, no external resources, and no control labelled Apply, Send, Connect or Submit.

---

## 8. Testing

```bash
uv run python -m pytest tests -q
```

24 test files. Unit tests cover parsing, scoring, state machines and rendering. HTTP tests exercise the router against a temporary database. Frontend tests parse `pipeline_v2.html` and assert the safety rules above. Playwright scripts under `scripts/` measure the real interface at a fixed viewport and check for clipping, overflow and JavaScript errors — these run against a copy of the database, never the live one.

---

## 9. Configuration

| Setting | Where | Default |
|---|---|---|
| Database path | `--db` flag | `career_pipeline_v2.sqlite3` |
| Port | `--port` flag | `8786` |
| LLM key / model / base URL | `.env` or environment | unset — scoring falls back to local embeddings |
| Job feeds | `job_rss_feeds.json` | shipped sample list |
| Search queries | `job_search_queries.json` | shipped sample list |
| Skill taxonomy | `skills_taxonomy.json` | shipped |
| ATS form profiles | `ats_form_profiles.json` | shipped |

Career facts live in `reference_cv_2027/data/`. Only `.example.yaml` templates are committed; your real files are gitignored.
