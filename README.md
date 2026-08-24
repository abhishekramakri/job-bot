# job-bot

Polls company ATS job boards and posts new, non-senior/staff/manager postings to a Discord channel via webhook.

## Setup

1. Create a Discord webhook: Server Settings → Integrations → Webhooks → New Webhook. Copy the URL.
2. In this repo on GitHub: Settings → Secrets and variables → Actions → New repository secret.
   - Name: `DISCORD_WEBHOOK_URL`
   - Value: the webhook URL from step 1.
3. Edit `companies.json` to add/remove target companies. Each entry needs:
   - `name`: display name
   - `ats`: `greenhouse`, `lever`, or `ashby`
   - `slug`: the company's slug on that ATS (found in the careers page URL)
4. Push to GitHub. The workflow in `.github/workflows/poll.yml` runs every 15 minutes automatically, or trigger it manually from the Actions tab ("Run workflow").

## Finding a company's ATS + slug

Look at the URL when you land on their careers page:
- `job-boards.greenhouse.io/{slug}` or `boards.greenhouse.io/{slug}` → greenhouse
- `jobs.lever.co/{slug}` → lever
- `jobs.ashbyhq.com/{slug}` → ashby
- `{company}.wd1.myworkdayjobs.com` (Workday) → not supported by this script; Workday has no clean public JSON endpoint

## Tuning the filter

Edit `filters.json`:
- `exclude_regex`: titles matching this (case-insensitive) are skipped. Defaults to common seniority/management terms (senior, staff, principal, lead, manager, director, etc).
- `include_keywords`: optional list of substrings a title must contain (e.g. `["software engineer", "audio"]`). Leave empty to allow any title that isn't excluded.

## Local testing

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python poller.py
```

Without `DISCORD_WEBHOOK_URL` set, it prints what it would send instead of posting. `seen.json` is updated either way — delete entries from it (or the whole file) if you want to re-trigger alerts for testing.
