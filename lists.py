#!/usr/bin/env python3
"""Pull new-grad roles from community job lists (SimplifyJobs, Jobright), rate each
company, and post a daily digest of the ones worth applying to to Discord."""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

import requests

from poller import HEADERS, ROOT, is_us_job, load_json, request_with_retry, title_passes_filter

SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
JOBRIGHT_URL = "https://raw.githubusercontent.com/jobright-ai/2026-Software-Engineer-New-Grad/master/README.md"

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
# Shown as the sender name in Discord so these posts are distinguishable from
# the main poller's alerts even when they share a channel.
WEBHOOK_USERNAME = "job-bot · list picks"
DISCORD_SUPPRESS_EMBEDS = 1 << 2
DISCORD_MAX_CHARS = 1900

RATING_MODEL = "claude-opus-5-5"
RATING_BATCH_SIZE = 40

POSTED_TIERS = ("creative", "strong", "solid")
SECTIONS = [
    ("creative", "🎨 **Creative**"),
    ("strong", "⭐ **Strong**"),
    ("solid", "👍 **Solid**"),
    ("unsure", "❔ **Not rated yet** (take a look)"),
]

# Short location forms the lists use that is_us_job doesn't recognize on its own.
US_SHORTHANDS = {"sf", "nyc", "la", "dc", "remote", "remote in us", "remote in usa"}

SUFFIX_RE = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|company|co|technologies|technology|industries|group|holdings)\b\.?"
)
JOBRIGHT_LINK_RE = re.compile(r"\*\*\[(.+?)\]\((.+?)\)\*\*")


def _key(name):
    n = re.sub(r"^the\s+", "", name.lower().strip())
    n = SUFFIX_RE.sub("", n)
    return re.sub(r"[^a-z0-9]", "", n)


def company_keys(name):
    """Loose match keys for a company name. A parenthetical counts as an alias,
    so "Block (Square)" matches both "Block" and "Square"."""
    keys = {_key(re.sub(r"\(.*?\)", "", name))}
    keys.update(_key(alias) for alias in re.findall(r"\((.*?)\)", name))
    keys.discard("")
    return keys


def role_key(job):
    # The lists word the same role slightly differently ("... 2026 (US)" vs "..."),
    # so drop years and a trailing country before comparing.
    title = re.sub(r"\b20\d\d\b|\((?:us|usa|united states)\)", "", job["title"].lower())
    return f"{min(company_keys(job['company']) or {''})}|{re.sub(r'[^a-z0-9]', '', title)}"


def is_us_listing(job):
    for loc in job["locations"]:
        if loc.strip().lower() in US_SHORTHANDS or is_us_job({"location": loc}):
            return True
    return False


def fetch_simplify():
    jobs = []
    for x in request_with_retry("GET", SIMPLIFY_URL, timeout=60).json():
        if not x.get("active") or not x.get("is_visible", True):
            continue
        if x.get("degrees") == ["PhD"]:
            continue
        jobs.append(
            {
                "id": "simplify:" + x["id"],
                "company": x["company_name"],
                "title": x["title"],
                "url": x["url"],
                "locations": x.get("locations") or [],
                "posted_ts": x.get("date_posted"),
                "citizenship_required": x.get("sponsorship") == "U.S. Citizenship is Required",
            }
        )
    return jobs


def parse_month_day(text):
    """Jobright dates look like "Oct 28" with no year; assume the most recent one."""
    now = datetime.now()
    try:
        dt = datetime.strptime(f"{text.strip()} {now.year}", "%b %d %Y")
    except ValueError:
        return None
    if dt > now:
        dt = dt.replace(year=now.year - 1)
    return dt.timestamp()


def fetch_jobright():
    jobs = []
    company = None
    for line in request_with_retry("GET", JOBRIGHT_URL, timeout=60).text.splitlines():
        if not line.startswith("| ") or line.startswith(("| Company", "| -")):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        # "↳" rows are extra roles at the company named on the row above.
        if cells[0] != "↳":
            m = JOBRIGHT_LINK_RE.search(cells[0])
            company = m.group(1) if m else cells[0].strip("* ")
        m = JOBRIGHT_LINK_RE.search(cells[1])
        if not m or not company:
            continue
        title, url = m.group(1), m.group(2).split("?")[0]
        id_m = re.search(r"/jobs/info/(\w+)", url)
        jobs.append(
            {
                "id": "jobright:" + (id_m.group(1) if id_m else url),
                "company": company,
                "title": title,
                "url": url,
                "locations": [cells[2]],
                "posted_ts": parse_month_day(cells[4]),
                "citizenship_required": False,
            }
        )
    return jobs


def load_ratings(profile_dir):
    """Merge hand ratings over automatic ones into {match key: rating}."""
    ratings = {}
    for name, r in load_json(profile_dir / "company_tiers_auto.json", {}).items():
        for k in company_keys(name):
            ratings[k] = {"tier": r["tier"], "flag": r.get("flag", "")}
    manual = load_json(profile_dir / "company_tiers.json", {})
    flags = manual.get("flags", {})
    for tier in ("creative", "strong", "solid", "pass"):
        for name in manual.get(tier, []):
            for k in company_keys(name):
                ratings[k] = {"tier": tier, "flag": flags.get(name, "")}
    for name, reason in manual.get("skip", {}).items():
        for k in company_keys(name):
            ratings[k] = {"tier": "skip", "flag": reason}
    return ratings


def rating_for(company, ratings):
    for k in company_keys(company):
        if k in ratings:
            return ratings[k]
    return None


RATING_SCHEMA = {
    "type": "object",
    "properties": {
        "companies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "tier": {"type": "string", "enum": ["creative", "strong", "solid", "pass", "skip", "unsure"]},
                    "reason": {"type": "string"},
                    "flag": {"type": "string"},
                },
                "required": ["name", "tier", "reason", "flag"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["companies"],
    "additionalProperties": False,
}

RATING_INSTRUCTIONS = """You rate employers for a new-grad software engineer deciding where to apply. They want either creative companies (games, music and audio, film, animation, media, design and creative tools; they are especially into audio/DSP and multimedia) or reputable, well-paying tech companies that aren't evil.

Put each company in one tier:
- creative: games, music/audio, film/animation/media, design or creative tools.
- strong: well-known, well-paid, reputable tech or quant firm; clearly worth applying.
- solid: reputable with decent pay but less exciting; still worth a look.
- pass: not worth it: staffing or consulting body shops, low-paying or non-tech employers, unremarkable small companies.
- skip: a hard no: defense, military or weapons work; intelligence-community contractors; crypto, blockchain or web3; gambling, sports betting or casinos; surveillance tech.
- unsure: you don't recognize the company and the job details don't settle it.

For creative, strong and solid, set `flag` to a short note on any ethical gray area (a defense side business, a crypto arm, privacy controversies), or an empty string if there is none. For skip, put the reason in `flag`. Keep `reason` under 12 words.

Small unknown companies hiring in Fort Meade, Annapolis Junction, Columbia MD, Chantilly, Herndon, Reston, Huntsville or Colorado Springs are usually defense or intelligence contractors.

These ratings were made by hand; stay consistent with them:
<hand_ratings>
{hand_ratings}
</hand_ratings>"""


def rate_companies(unknown, profile_dir):
    """Ask Claude to rate companies with no rating yet. Returns {company: rating}.
    Returns {} (so they show up as "not rated yet") without an API key or on failure."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"[rate] no ANTHROPIC_API_KEY; {len(unknown)} companies left unrated", file=sys.stderr)
        return {}
    import anthropic

    client = anthropic.Anthropic()
    hand = load_json(profile_dir / "company_tiers.json", {})
    hand.pop("_about", None)
    system = RATING_INSTRUCTIONS.replace("{hand_ratings}", json.dumps(hand, ensure_ascii=False))

    names = sorted(unknown)
    results = {}
    for start in range(0, len(names), RATING_BATCH_SIZE):
        batch = names[start : start + RATING_BATCH_SIZE]
        lines = []
        for name in batch:
            samples = "; ".join(f"{j['title']} ({', '.join(j['locations'][:2])})" for j in unknown[name][:3])
            lines.append(f"- {name}: {samples}")
        prompt = (
            "Rate these companies. Use each name exactly as written. "
            "Next to each name are some of its open roles.\n\n" + "\n".join(lines)
        )
        try:
            response = client.beta.messages.create(
                model=RATING_MODEL,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "medium", "format": {"type": "json_schema", "schema": RATING_SCHEMA}},
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIStatusError as e:
            print(f"[rate] API error {e.status_code}: {e.message}", file=sys.stderr)
            continue
        except anthropic.APIConnectionError as e:
            print(f"[rate] connection error: {e}", file=sys.stderr)
            continue
        if response.stop_reason in ("refusal", "max_tokens"):
            print(f"[rate] batch not rated (stop_reason={response.stop_reason})", file=sys.stderr)
            continue
        text = next(b.text for b in response.content if b.type == "text")
        wanted = set(batch)
        for r in json.loads(text)["companies"]:
            if r["name"] in wanted:
                results[r["name"]] = r
    return results


def build_messages(picks, filtered_count, today):
    header = (
        f"📋 **New-grad list picks · {today}**\n"
        f"{len(picks)} new roles from the Simplify and Jobright lists, at companies the main bot doesn't watch."
    )
    blocks = [header]
    for tier, title in SECTIONS:
        rows = [p for p in picks if p["tier"] == tier]
        if not rows:
            continue
        blocks.append(f"\n{title}")
        by_company = {}
        for p in rows:
            by_company.setdefault(p["job"]["company"], []).append(p)
        for company in sorted(by_company, key=str.lower):
            group = by_company[company]
            flag = group[0]["flag"]
            blocks.append(f"**{company}**" + (f" ⚠️ _{flag}_" if flag else ""))
            for p in sorted(group, key=lambda p: p["job"]["title"]):
                job = p["job"]
                locs = job["locations"]
                where = ", ".join(locs[:2]) + (f" +{len(locs) - 2}" if len(locs) > 2 else "")
                line = f"• [{job['title']}]({job['url']}) · {where}"
                if job["citizenship_required"]:
                    line += " · ⚠️ _US citizenship required_"
                blocks.append(line)
    if filtered_count:
        blocks.append(f"\n_Left out {filtered_count} roles at companies rated not worth it or a hard no._")

    messages, current = [], ""
    for block in blocks:
        if current and len(current) + len(block) + 1 > DISCORD_MAX_CHARS:
            messages.append(current)
            current = block.lstrip("\n")
        else:
            current = f"{current}\n{block}" if current else block
    if current:
        messages.append(current)
    return messages


def post_to_discord(content):
    payload = {
        "content": content,
        "username": WEBHOOK_USERNAME,
        "flags": DISCORD_SUPPRESS_EMBEDS,
        "allowed_mentions": {"parse": []},
    }
    for attempt in range(5):
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=20, headers=HEADERS)
        if resp.status_code == 429:
            time.sleep(resp.json().get("retry_after", 1) + 0.1)
            continue
        if resp.status_code >= 300:
            print(f"Discord webhook error {resp.status_code}: {resp.text}", file=sys.stderr)
        break
    time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile")
    parser.add_argument("--dry-run", action="store_true", help="print the digest; don't post or save anything")
    parser.add_argument("--max-age-days", type=int, help="override the profile's max_age_days")
    args = parser.parse_args()

    profile_dir = ROOT / "profiles" / args.profile
    if not profile_dir.is_dir():
        print(f"unknown profile: {args.profile}", file=sys.stderr)
        sys.exit(1)
    seen_file = profile_dir / "seen.json"
    auto_file = profile_dir / "company_tiers_auto.json"

    filters = load_json(profile_dir / "filters.json", {"exclude_regex": "", "include_keywords": []})
    exclude_re = re.compile(filters["exclude_regex"], re.IGNORECASE)
    include_keywords = filters.get("include_keywords", [])
    require_keywords = filters.get("require_keywords", [])
    max_age_days = args.max_age_days or filters.get("max_age_days", 14)

    tracked = set()
    tracked_by = filters.get("skip_companies_tracked_by")
    if tracked_by:
        for c in load_json(ROOT / "profiles" / tracked_by / "companies.json", []):
            if not c.get("disabled"):
                tracked |= company_keys(c["name"])

    jobs = []
    for source, fetcher in (("simplify", fetch_simplify), ("jobright", fetch_jobright)):
        try:
            jobs.extend(fetcher())
        except Exception as e:
            print(f"[error] {source}: {e}", file=sys.stderr)

    seen = load_json(seen_file, {})
    seen_ids = set(seen.get("ids", []))
    seen_roles = set(seen.get("roles", []))
    new_ids, new_roles = set(), set()
    stale_cutoff = time.time() - max_age_days * 86400

    candidates = []
    for job in jobs:
        if not is_us_listing(job):
            continue
        if not title_passes_filter(job["title"], exclude_re, include_keywords, require_keywords):
            continue
        # Checked before marking seen, so a later run with a longer lookback
        # (--max-age-days) can still post older roles.
        if job["posted_ts"] is not None and job["posted_ts"] < stale_cutoff:
            continue
        rk = role_key(job)
        # The same role often shows up on both lists, so dedupe on company+title too.
        already_seen = job["id"] in seen_ids or rk in seen_roles or rk in new_roles
        new_ids.add(job["id"])
        new_roles.add(rk)
        if already_seen:
            continue
        if company_keys(job["company"]) & tracked:
            continue
        candidates.append(job)

    ratings = load_ratings(profile_dir)
    unknown = {}
    for job in candidates:
        if rating_for(job["company"], ratings) is None:
            unknown.setdefault(job["company"], []).append(job)
    auto = rate_companies(unknown, profile_dir) if unknown else {}
    auto_saved = load_json(auto_file, {})
    today = datetime.now().strftime("%Y-%m-%d")
    for name, r in auto.items():
        if r["tier"] == "unsure":
            continue
        auto_saved[name] = {"tier": r["tier"], "reason": r["reason"], "flag": r["flag"], "rated": today}
        print(f"[rate] {name}: {r['tier']} ({r['reason']}){' ⚠ ' + r['flag'] if r['flag'] else ''}")
        for k in company_keys(name):
            ratings[k] = {"tier": r["tier"], "flag": r["flag"]}

    picks, filtered_count = [], 0
    for job in candidates:
        rating = rating_for(job["company"], ratings) or {"tier": "unsure", "flag": ""}
        if rating["tier"] in POSTED_TIERS or rating["tier"] == "unsure":
            picks.append({"job": job, "tier": rating["tier"], "flag": rating["flag"]})
        else:
            filtered_count += 1

    print(f"{len(jobs)} listings, {len(candidates)} new candidates, {len(picks)} picks, {filtered_count} left out")
    if picks:
        if not args.dry_run and not WEBHOOK_URL:
            print("[no webhook set] would post:")
        for content in build_messages(picks, filtered_count, today):
            if args.dry_run or not WEBHOOK_URL:
                print(f"{content}\n")
            else:
                post_to_discord(content)

    if args.dry_run:
        return
    seen["ids"] = sorted(seen_ids | new_ids)
    seen["roles"] = sorted(seen_roles | new_roles)
    with open(seen_file, "w") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if auto:
        with open(auto_file, "w") as f:
            json.dump(dict(sorted(auto_saved.items())), f, indent=2, ensure_ascii=False)
            f.write("\n")


if __name__ == "__main__":
    main()
