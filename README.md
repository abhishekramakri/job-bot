# job-bot

Polls company ATS job boards and posts new, filtered postings to Discord via webhook.

Supports multiple independent **profiles** (e.g. one person's job search vs. another's), each with its own company list, filters, seen-job tracking, and Discord destination, all sharing the same polling engine.

## Profiles

Each profile lives in `profiles/<name>/` with three files:
- `companies.json` — target companies for this profile
- `filters.json` — title/region filtering rules for this profile
- `seen.json` — tracked job IDs so far (auto-managed, don't hand-edit)

Currently configured: `abhi` (software engineering, US-wide), `aparna` (hardware/FPGA/embedded/DSP, Seattle metro only).

Run a profile with `python poller.py <profile>`.

## Setup for a new profile

1. Create a Discord webhook: Server Settings → Integrations → Webhooks → New Webhook. Copy the URL.
2. In this repo on GitHub: Settings → Secrets and variables → Actions → New repository secret. Use a distinct name per profile (e.g. `DISCORD_WEBHOOK_URL_APARNA`).
3. Create `profiles/<name>/companies.json`, `filters.json`, and an empty `seen.json` (`{}`).
4. Add a job to `.github/workflows/poll.yml` for the new profile, modeled on the existing `poll-abhi` / `poll-aparna` jobs — it needs its own `Run poller` step (`python poller.py <name>`, with the right webhook secret) and its own `Commit updated seen.json` step (pointed at `profiles/<name>/seen.json`).
5. Push. The workflow runs every 15 minutes automatically, or trigger it manually from the Actions tab ("Run workflow").

## companies.json entry format

Each entry needs:
- `name`: display name
- `ats`: one of `greenhouse`, `lever`, `ashby`, `workday`, `phenom`, `phenom_v2`, `workable`, `bamboohr`, `teamtailor`, `apple`, `amazon`, `snap`
- ATS-specific fields — see existing entries in `profiles/*/companies.json` for the shape each type expects (e.g. `slug` for Greenhouse/Lever/Ashby/Workable/BambooHR, `host`/`tenant`/`site` for Workday, `host`/`domain` for Phenom).
- Optional `disabled: true` to keep a company configured but skip polling it (reversible — just remove the flag).
- Optional `query` or `queries: [...]` for ATS types that support server-side search scoping (Phenom, Apple, Amazon) — useful for narrowing a huge company's board instead of pulling everything.

Finding a company's ATS takes some detective work: check the careers page URL for a recognizable pattern (`boards.greenhouse.io/{slug}`, `jobs.lever.co/{slug}`, `jobs.ashbyhq.com/{slug}`, `*.myworkdayjobs.com`), or open browser devtools on the careers page and look at what XHR/fetch requests it makes.

## Tuning the filter

Edit a profile's `filters.json`:
- `exclude_regex`: titles matching this (case-insensitive) are skipped. Defaults to seniority/management terms (senior, staff, principal, lead, manager, director, etc.) plus leveled titles (`II`, `III`, `L4`, numeric levels).
- `include_keywords`: a title must contain at least one of these (case-insensitive substring match) to pass. Empty list means no title requirement.
- `region`: `"us"` (default) requires a US location; `"seattle"` requires a Seattle-metro city specifically (Seattle, Bellevue, Redmond, Kirkland, Tacoma, Everett, etc.) — bare "Remote" postings with no named city won't match either mode.

## Local testing

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python poller.py <profile>
```

Without `DISCORD_WEBHOOK_URL` set, it prints what it would send instead of posting. `seen.json` is updated either way — delete entries from it (or clear the whole file to `{}`) if you want to re-trigger alerts for testing.
