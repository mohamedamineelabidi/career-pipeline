# Career Pipeline v2

Career Pipeline v2 is the local source of truth for the candidate's opportunity, CV, contact, outreach-draft and conversion workflow.

## Safety boundary

- The system never sends email or LinkedIn messages, opens or fills LinkedIn composers, sends connection requests, or submits applications.
- The dashboard contains local tracking and copy controls only. It has no Send, Connect, Apply or Submit action.
- `user_applied` and `sent_by_user` events require explicit user confirmation and only record actions Mohamed performed outside the dashboard.
- Email addresses remain unverified unless intentionally published on an official vacancy/company page or a credible person-controlled professional page. Syntax, MX and enrichment output do not verify a mailbox.
- Generated JSON is a read-only compatibility artifact. SQLite is the operational database.

## Files

- `career_pipeline_v2.sqlite3`: local operational database, ignored by Git.
- `pipeline_v2.py`: schema, migrations, scoring, validation, safe API and exports.
- `migrate_pipeline_v2.py`: CLI for migration, validation, export and local server.
- `pipeline_v2.html`: same-origin local dashboard.
- `jobs_digest.json`: generated, read-only compatibility snapshot.
- `legacy/jobs_digest_v1_2026-08-29.json`: preserved pre-v2 input.
- `backups/2026-08-29_pre_pipeline_v2/`: checksum-verified rollback snapshot.

## Install and quality gates

```bash
cd /path/to/cv
uv sync
uv run python -m pytest tests/test_pipeline_v2.py tests/test_pipeline_v2_html.py reference_cv_2027/tests/test_tailor_cv_agent.py -q
```

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`.

## One-time legacy migration

```bash
uv run python migrate_pipeline_v2.py migrate --source jobs_digest.json --db career_pipeline_v2.sqlite3
uv run python migrate_pipeline_v2.py validate --db career_pipeline_v2.sqlite3
```

Migration is transactional and idempotent. Stable IDs are deterministic, primary IDs are protected by immutable-ID triggers, and the database uses foreign keys, WAL mode, score-schema checks and lifecycle history.

## Serve locally

```bash
uv run python migrate_pipeline_v2.py serve --db career_pipeline_v2.sqlite3 --port 8786
```

Open `http://127.0.0.1:8786/pipeline_v2.html`. The server binds only to `127.0.0.1`.

## Generate the compatibility snapshot

```bash
uv run python migrate_pipeline_v2.py export --db career_pipeline_v2.sqlite3 --output jobs_digest.json
```

The export is written atomically, includes `generated_read_only: true`, and is marked read-only. Legacy scripts that try to mutate it fail closed. The active CV generator also refuses to overwrite generated snapshots.

## Score schema v2

Every opportunity stores separate fields:

- `fit_score`: evidence-backed role/skill fit, 0–100.
- `eligibility_status`: `eligible`, `unknown`, or `blocked`.
- `freshness_status`: `active`, `recent`, `unknown`, `stale`, or `expired`.
- `verification_confidence`: canonical-source confidence, 0–100.
- `priority_score`: versioned computed ranking, 0–100.
- `score_schema_version`: currently `2`.
- `score_breakdown_json`: component values and gate result.

Blocked, stale and expired roles receive priority zero and a reason-coded archive state. Only eligibility-passed and fresh roles can become `eligible` or `shortlisted`.

## Opportunity lifecycle

`discovered` → `verified_active` → `eligible` → `shortlisted` → `user_applied`

`closed` is used for archived, rejected, expired or blocked records. Every status transition is written transactionally to `lifecycle_events`. Stale browser updates are rejected with HTTP 409.

## Conversion funnel

The dashboard reports:

1. Discovered
2. Verified active
3. Eligible
4. Shortlisted
5. Approved by user
6. Applied manually
7. Response received
8. Screening
9. Interview
10. Offer
11. Rejection

Application and outreach outcomes are manual history only. The API does not perform the external action.

## CV policy

- `role_family`: title or summary-based positioning. This is the honest classification for all migrated legacy records.
- `exact_vacancy`: requires a substantive complete JD in `full_job_description` or `job_description`; short dashboard summaries cannot qualify.
- Exact-vacancy manifests include evidence-to-requirement mappings, keyword coverage, missing-skill reporting and a source fingerprint that changes when the JD, candidate profile, evidence register, tailoring knowledge or generator changes.
- One page is the default. A two-page result requires an explicit, reasoned exception.
- Vacancy language is detected. A render whose canonical profile language does not match is marked `manual_translation_required`, not ready.
- No professional-role CV may contain PFE positioning.

## Contact and draft controls

- Every migrated contact receives an explicit verification state; absent values become `unverified`.
- Every draft is linted by the same universal linter.
- Approval requires a verified route, clean lint and no active company-level outreach collision.
- `sent_by_user` requires `confirmed_by_user=true` and records an event; it never sends anything.

## Rollback

1. Stop only the v2 server process.
2. Preserve the current SQLite file for diagnosis.
3. Restore `jobs_digest.json` from `backups/2026-08-29_pre_pipeline_v2/jobs_digest.json` or `legacy/jobs_digest_v1_2026-08-29.json`.
4. Verify the backup with `backups/2026-08-29_pre_pipeline_v2/SHA256SUMS`.
5. Do not resume legacy composer automation. The no-composer boundary remains permanent.
