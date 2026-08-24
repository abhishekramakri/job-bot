#!/usr/bin/env python3
"""Poll company ATS job boards, filter by title, alert new matches to Discord."""

import json
import os
import re
import sys
import time
from datetime import datetime
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

STALE_DAYS = 14

US_LOCATION_RE = re.compile(r"\bunited states\b|\bu\.s\.a?\.?\b|\busa\b", re.IGNORECASE)

US_STATE_ABBREVS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
    "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
    "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
}
US_CITY_HINTS = {
    "san francisco", "new york", "seattle", "boston", "austin", "chicago",
    "los angeles", "san jose", "cupertino", "redmond", "mountain view", "sunnyvale",
    "menlo park", "palo alto", "bellevue", "denver", "atlanta", "dallas", "houston",
    "phoenix", "san diego", "portland", "pittsburgh", "raleigh", "durham",
    "nashville", "miami", "minneapolis", "detroit", "philadelphia",
    "salt lake city", "charlotte", "santa barbara", "santa clara",
}


def is_us_job(job):
    country_code = job.get("country_code")
    if country_code:
        return country_code.upper() in ("US", "USA")

    text = (job.get("location") or "").strip()
    if not text:
        return False
    lower = text.lower()
    if US_LOCATION_RE.search(lower):
        return True
    if any(re.search(rf"\b{re.escape(name)}\b", lower) for name in US_STATE_NAMES):
        return True
    if any(city in lower for city in US_CITY_HINTS):
        return True
    last_token = re.split(r"[,/]", text)[-1].strip().upper()
    return last_token in US_STATE_ABBREVS


def parse_date(text, fmt):
    if not text:
        return None
    try:
        return datetime.strptime(text, fmt).timestamp()
    except ValueError:
        return None


def parse_iso_date(text):
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def request_with_retry(method, url, max_retries=5, timeout=20, **kwargs):
    for attempt in range(max_retries):
        try:
            resp = requests.request(method, url, timeout=timeout, headers=HEADERS, **kwargs)
        except requests.exceptions.RequestException:
            if attempt < max_retries - 1:
                time.sleep(min(2**attempt, 30))
                continue
            raise
        if resp.status_code in (429, 503) and attempt < max_retries - 1:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else min(2**attempt, 30)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp




def fetch_greenhouse(company):
    slug = company["slug"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    resp = request_with_retry("GET", url)
    jobs = resp.json().get("jobs", [])
    result = []
    for j in jobs:
        offices = j.get("offices") or []
        location = offices[0].get("location", "") if offices else (j.get("location") or {}).get("name", "")
        result.append(
            {
                "id": str(j["id"]),
                "title": j["title"],
                "url": j.get("absolute_url", ""),
                "location": location,
                "posted_ts": parse_iso_date(j.get("first_published")),
            }
        )
    return result


def fetch_lever(company):
    slug = company["slug"]
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = request_with_retry("GET", url)
    jobs = resp.json()
    return [
        {
            "id": j.get("id", j.get("text", "")),
            "title": j.get("text", ""),
            "url": j.get("hostedUrl", ""),
            "country_code": j.get("country"),
        }
        for j in jobs
    ]


def fetch_ashby(company):
    slug = company["slug"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    resp = request_with_retry("GET", url)
    jobs = resp.json().get("jobs", [])
    result = []
    for j in jobs:
        country = (
            (j.get("address") or {}).get("postalAddress", {}).get("addressCountry")
        )
        result.append(
            {
                "id": j.get("id", ""),
                "title": j.get("title", ""),
                "url": j.get("jobUrl", ""),
                "location": j.get("location", "") or country or "",
                "posted_ts": parse_iso_date(j.get("publishedAt")),
            }
        )
    return result


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
        resp = request_with_retry(
            "GET",
            api_url,
            params={"domain": domain, "query": query, "location": "", "start": start},
        )
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
                    "posted_ts": p.get("postedTs"),
                    "location": ", ".join(p.get("locations") or []),
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
        resp = request_with_retry(
            "GET",
            api_url,
            params={"query": query, "start": start, "num": page_size},
        )
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
                    "posted_ts": p.get("t_create"),
                    "location": p.get("location", ""),
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
        resp = request_with_retry(
            "GET",
            "https://jobs.apple.com/en-us/search",
            params={"search": query, "page": page, "location": "united-states-USA"},
            timeout=30,
        )
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
            locations = r.get("locations") or []
            location = ", ".join(loc.get("countryName", "") for loc in locations)
            jobs.append(
                {
                    "id": r.get("id", position_id),
                    "title": r.get("postingTitle", ""),
                    "url": f"https://jobs.apple.com/en-us/details/{position_id}",
                    "posted_ts": parse_date(r.get("postingDate"), "%b %d, %Y"),
                    "location": location,
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
        resp = request_with_retry(
            "GET",
            "https://www.amazon.jobs/en/search.json",
            params={"base_query": query, "result_limit": limit, "offset": offset},
        )
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
                    "posted_ts": parse_date(j.get("posted_date"), "%B %d, %Y"),
                    "country_code": j.get("country_code"),
                }
            )
        offset += limit
        if offset >= total:
            break
    return jobs


def fetch_workable(company):
    slug = company["slug"]
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    resp = request_with_retry("GET", url)
    jobs = resp.json().get("jobs", [])
    result = []
    for j in jobs:
        locations = j.get("locations") or []
        country_code = locations[0].get("countryCode") if locations else None
        result.append(
            {
                "id": j.get("shortcode", ""),
                "title": j.get("title", ""),
                "url": j.get("url", ""),
                "country_code": country_code,
            }
        )
    return result


def fetch_bamboohr(company):
    slug = company["slug"]
    url = f"https://{slug}.bamboohr.com/careers/list"
    resp = request_with_retry("GET", url)
    jobs = resp.json().get("result", [])
    return [
        {
            "id": str(j.get("id", "")),
            "title": j.get("jobOpeningName", ""),
            "url": f"https://{slug}.bamboohr.com/careers/{j.get('id', '')}",
            "location": f"{(j.get('location') or {}).get('city', '')}, {(j.get('location') or {}).get('state', '')}",
        }
        for j in jobs
    ]


def fetch_teamtailor(company):
    host = company["host"]
    url = f"https://{host}/jobs.json"
    resp = request_with_retry("GET", url)
    items = resp.json().get("items", [])
    result = []
    for j in items:
        job_locations = j.get("_jobposting", {}).get("jobLocation") or []
        country_code = None
        if job_locations:
            country_code = job_locations[0].get("address", {}).get("addressCountry")
        result.append(
            {
                "id": j.get("id", ""),
                "title": j.get("title", ""),
                "url": j.get("url", ""),
                "country_code": country_code,
                "posted_ts": parse_iso_date(j.get("date_published")),
            }
        )
    return result


def extract_js_object(html, marker):
    start = html.find(marker)
    if start == -1:
        return None
    start += len(marker)
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(html)):
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return html[start : i + 1]
    return None


def fetch_snap(company):
    resp = request_with_retry("GET", "https://careers.snap.com/jobs")
    raw = extract_js_object(resp.text, "window.ASYNC_DATA_CONTROLLER_CACHE = ")
    if not raw:
        return []
    data = json.loads(raw)
    jobs = []
    for entry in data.values():
        for hit in entry.get("data", {}).get("body", []):
            src = hit.get("_source", {})
            offices = src.get("offices") or []
            location = offices[0].get("location", "") if offices else src.get("primary_location", "")
            jobs.append(
                {
                    "id": src.get("id", ""),
                    "title": src.get("title", ""),
                    "url": src.get("absolute_url", ""),
                    "location": location,
                }
            )
    return jobs


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
        resp = request_with_retry(
            "POST",
            api_url,
            json={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""},
        )
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
                    "location": j.get("locationsText", ""),
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
    "snap": fetch_snap,
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
    for attempt in range(5):
        resp = requests.post(WEBHOOK_URL, json={"content": content}, timeout=20, headers=HEADERS)
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 1)
            time.sleep(retry_after + 0.1)
            continue
        if resp.status_code >= 300:
            print(f"Discord webhook error {resp.status_code}: {resp.text}", file=sys.stderr)
        break
    time.sleep(0.5)


def main():
    companies = load_json(COMPANIES_FILE, [])
    seen = load_json(SEEN_FILE, {})
    filters = load_json(FILTERS_FILE, {"exclude_regex": "", "include_keywords": []})

    exclude_re = re.compile(filters["exclude_regex"], re.IGNORECASE)
    include_keywords = filters.get("include_keywords", [])

    for company in companies:
        if company.get("disabled"):
            continue
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

        stale_cutoff = time.time() - STALE_DAYS * 86400
        for job in jobs:
            current_ids.add(job["id"])
            if job["id"] in seen_ids:
                continue
            posted_ts = job.get("posted_ts")
            if posted_ts is not None and posted_ts < stale_cutoff:
                continue
            if not is_us_job(job):
                continue
            if title_passes_filter(job["title"], exclude_re, include_keywords):
                send_discord_alert(name, job)

        seen[key] = sorted(seen_ids | current_ids)

    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
