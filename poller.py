#!/usr/bin/env python3
"""Poll company ATS job boards, filter by title, alert new matches to Discord."""

import json
import os
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).parent
COMPANIES_FILE = ROOT / "companies.json"
SEEN_FILE = ROOT / "seen.json"
FILTERS_FILE = ROOT / "filters.json"

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}


def fetch_greenhouse(company):
    slug = company["slug"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    resp = requests.get(url, timeout=20, headers=HEADERS)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [
        {
            "id": str(j["id"]),
            "title": j["title"],
            "url": j.get("absolute_url", ""),
        }
        for j in jobs
    ]


def fetch_lever(company):
    slug = company["slug"]
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = requests.get(url, timeout=20, headers=HEADERS)
    resp.raise_for_status()
    jobs = resp.json()
    return [
        {
            "id": j.get("id", j.get("text", "")),
            "title": j.get("text", ""),
            "url": j.get("hostedUrl", ""),
        }
        for j in jobs
    ]


def fetch_ashby(company):
    slug = company["slug"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    resp = requests.get(url, timeout=20, headers=HEADERS)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [
        {
            "id": j.get("id", ""),
            "title": j.get("title", ""),
            "url": j.get("jobUrl", ""),
        }
        for j in jobs
    ]


def fetch_phenom(company):
    host = company["host"]
    domain = company["domain"]
    careers_path = company.get("careers_path", "/careers/job")
    query = company.get("query", "")
    api_url = f"https://{host}/api/pcsx/search"

    jobs = []
    start = 0
    page_size = 10
    total = None
    while True:
        resp = requests.get(
            api_url,
            params={"domain": domain, "query": query, "location": "", "start": start},
            timeout=20,
            headers=HEADERS,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        if total is None:
            total = data.get("count", 0)
        positions = data.get("positions", [])
        if not positions:
            break
        for p in positions:
            jobs.append(
                {
                    "id": str(p.get("id", "")),
                    "title": p.get("name", ""),
                    "url": f"https://{host}{careers_path}/{p.get('id', '')}",
                }
            )
        start += page_size
        if start >= total:
            break
    return jobs


def fetch_phenom_v2(company):
    host = company["host"]
    query = company.get("query", "")
    api_url = f"https://{host}/api/apply/v2/jobs"

    jobs = []
    start = 0
    page_size = 20
    total = None
    while True:
        resp = requests.get(
            api_url,
            params={"query": query, "start": start, "num": page_size},
            timeout=20,
            headers=HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        if total is None:
            total = data.get("count", 0)
        positions = data.get("positions", [])
        if not positions:
            break
        for p in positions:
            jobs.append(
                {
                    "id": str(p.get("id", "")),
                    "title": p.get("name", ""),
                    "url": p.get("canonicalPositionUrl", ""),
                }
            )
        start += page_size
        if start >= total:
            break
    return jobs


def fetch_apple(company):
    query = company.get("query", "")
    jobs = []
    page = 1
    total = None
    while True:
        resp = requests.get(
            "https://jobs.apple.com/en-us/search",
            params={"search": query, "page": page},
            timeout=20,
            headers=HEADERS,
        )
        resp.raise_for_status()
        html = resp.text
        marker = 'window.__staticRouterHydrationData = JSON.parse("'
        start = html.find(marker)
        if start == -1:
            break
        start += len(marker)
        end = html.find('");</script', start)
        raw = html[start:end].encode().decode("unicode_escape")
        data = json.loads(raw)
        search = data.get("loaderData", {}).get("search", {})
        if total is None:
            total = search.get("totalRecords", 0)
        results = search.get("searchResults", [])
        if not results:
            break
        for r in results:
            position_id = r.get("positionId", "")
            jobs.append(
                {
                    "id": r.get("id", position_id),
                    "title": r.get("postingTitle", ""),
                    "url": f"https://jobs.apple.com/en-us/details/{position_id}",
                }
            )
        page += 1
        if len(jobs) >= total:
            break
    return jobs


def fetch_amazon(company):
    query = company.get("query", "")
    jobs = []
    offset = 0
    limit = 20
    total = None
    while True:
        resp = requests.get(
            "https://www.amazon.jobs/en/search.json",
            params={"base_query": query, "result_limit": limit, "offset": offset},
            timeout=20,
            headers=HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        if total is None:
            total = data.get("hits", 0)
        results = data.get("jobs", [])
        if not results:
            break
        for j in results:
            jobs.append(
                {
                    "id": str(j.get("id_icims", "")),
                    "title": j.get("title", ""),
                    "url": "https://www.amazon.jobs" + j.get("job_path", ""),
                }
            )
        offset += limit
        if offset >= total:
            break
    return jobs


def fetch_workable(company):
    slug = company["slug"]
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    resp = requests.get(url, timeout=20, headers=HEADERS)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [
        {
            "id": j.get("shortcode", ""),
            "title": j.get("title", ""),
            "url": j.get("url", ""),
        }
        for j in jobs
    ]


def fetch_bamboohr(company):
    slug = company["slug"]
    url = f"https://{slug}.bamboohr.com/careers/list"
    resp = requests.get(url, timeout=20, headers=HEADERS)
    resp.raise_for_status()
    jobs = resp.json().get("result", [])
    return [
        {
            "id": str(j.get("id", "")),
            "title": j.get("jobOpeningName", ""),
            "url": f"https://{slug}.bamboohr.com/careers/{j.get('id', '')}",
        }
        for j in jobs
    ]


def fetch_teamtailor(company):
    host = company["host"]
    url = f"https://{host}/jobs.json"
    resp = requests.get(url, timeout=20, headers=HEADERS)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return [
        {
            "id": j.get("id", ""),
            "title": j.get("title", ""),
            "url": j.get("url", ""),
        }
        for j in items
    ]


def fetch_workday(company):
    host = company["host"]
    tenant = company["tenant"]
    site = company["site"]
    api_url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    careers_url = f"https://{host}/{site}"

    jobs = []
    offset = 0
    limit = 20
    total = None
    while True:
        resp = requests.post(
            api_url,
            json={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""},
            timeout=20,
            headers=HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        if total is None:
            total = data.get("total", 0)
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for j in postings:
            path = j.get("externalPath", "")
            jobs.append(
                {
                    "id": path or j.get("title", ""),
                    "title": j.get("title", ""),
                    "url": careers_url + path,
                }
            )
        offset += limit
        if offset >= total:
            break
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workday": fetch_workday,
    "phenom": fetch_phenom,
    "phenom_v2": fetch_phenom_v2,
    "apple": fetch_apple,
    "amazon": fetch_amazon,
    "workable": fetch_workable,
    "bamboohr": fetch_bamboohr,
    "teamtailor": fetch_teamtailor,
}


def load_json(path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def title_passes_filter(title, exclude_re, include_keywords):
    if exclude_re.search(title):
        return False
    if include_keywords:
        return any(kw.lower() in title.lower() for kw in include_keywords)
    return True


def send_discord_alert(company, job):
    if not WEBHOOK_URL:
        print(f"[no webhook set] would alert: {company} - {job['title']}")
        return
    content = f"**New job at {company}**: {job['title']}\n{job['url']}"
    resp = requests.post(WEBHOOK_URL, json={"content": content}, timeout=20, headers=HEADERS)
    if resp.status_code >= 300:
        print(f"Discord webhook error {resp.status_code}: {resp.text}", file=sys.stderr)


def main():
    companies = load_json(COMPANIES_FILE, [])
    seen = load_json(SEEN_FILE, {})
    filters = load_json(FILTERS_FILE, {"exclude_regex": "", "include_keywords": []})

    exclude_re = re.compile(filters["exclude_regex"], re.IGNORECASE)
    include_keywords = filters.get("include_keywords", [])

    for company in companies:
        name = company["name"]
        ats = company["ats"]
        key = company.get("slug") or name
        fetcher = FETCHERS.get(ats)
        if not fetcher:
            print(f"[skip] {name}: unsupported ats '{ats}'", file=sys.stderr)
            continue

        queries = company.get("queries") or [company.get("query", "")]
        jobs_by_id = {}
        fetch_failed = False
        for q in queries:
            company_for_query = {**company, "query": q}
            try:
                for job in fetcher(company_for_query):
                    jobs_by_id[job["id"]] = job
            except Exception as e:
                print(f"[error] {name} (query={q!r}): {e}", file=sys.stderr)
                fetch_failed = True
        if fetch_failed and not jobs_by_id:
            continue
        jobs = list(jobs_by_id.values())

        seen_ids = set(seen.get(key, []))
        current_ids = set()

        for job in jobs:
            current_ids.add(job["id"])
            if job["id"] in seen_ids:
                continue
            if title_passes_filter(job["title"], exclude_re, include_keywords):
                send_discord_alert(name, job)

        seen[key] = sorted(current_ids)

    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
