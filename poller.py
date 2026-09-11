#!/usr/bin/env python3
"""Poll company ATS job boards, filter by title, alert new matches to Discord."""

import html
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).parent

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


def _metro_re(cities, extra_phrases=()):
    pattern = r"\b(" + "|".join(re.escape(c) for c in cities) + r")\b"
    for phrase in extra_phrases:
        pattern += r"|\b" + phrase + r"\b"
    return re.compile(pattern, re.IGNORECASE)


METRO_REGEX = {
    "seattle": _metro_re(
        {
            "seattle", "bellevue", "redmond", "kirkland", "renton", "tacoma",
            "everett", "bothell", "issaquah", "sammamish", "kent", "auburn",
            "federal way", "mercer island", "woodinville", "lynnwood",
            "kenmore", "shoreline", "burien", "seatac", "tukwila", "newcastle",
            "snoqualmie", "duvall", "marysville", "edmonds",
            "mountlake terrace", "mukilteo", "puyallup", "bremerton",
            "snohomish", "monroe",
        },
        ["greater seattle", "seattle metro", "puget sound"],
    ),
    "san francisco": _metro_re(
        {
            "san francisco", "oakland", "berkeley", "san jose", "palo alto",
            "mountain view", "menlo park", "sunnyvale", "santa clara",
            "redwood city", "cupertino", "fremont", "south san francisco",
            "san mateo", "burlingame", "daly city", "emeryville", "alameda",
            "walnut creek", "san rafael", "foster city", "milpitas",
            "campbell", "los gatos", "saratoga", "pleasanton", "dublin",
            "san ramon", "concord", "richmond", "hayward", "union city",
            "morgan hill", "los altos", "belmont", "millbrae",
        },
        ["bay area", "sf bay area", "silicon valley"],
    ),
    "chicago": _metro_re(
        {
            "chicago", "evanston", "oak park", "schaumburg", "naperville",
            "skokie", "niles", "cicero", "elgin", "aurora", "joliet",
            "waukegan", "arlington heights", "downers grove", "oak brook",
            "rosemont", "deerfield", "northbrook", "glenview", "wheaton",
            "hoffman estates", "elk grove village", "bolingbrook",
            "orland park", "tinley park", "palatine",
        },
        ["chicagoland"],
    ),
    "new york": _metro_re(
        {
            "new york", "brooklyn", "manhattan", "queens", "bronx",
            "staten island", "jersey city", "hoboken", "long island city",
            "newark", "weehawken", "white plains", "yonkers", "secaucus",
            "stamford", "greenwich",
        },
        ["nyc", "new york city"],
    ),
}


def is_seattle_job(job):
    return is_in_metro(job, ["seattle"])


def is_in_metro(job, metro_names):
    text = (job.get("location") or "").strip()
    if not text:
        return False
    for name in metro_names:
        regex = METRO_REGEX.get(name)
        if regex and regex.search(text):
            return True
    return False


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


def parse_workday_relative_date(text):
    if not text:
        return None
    lower = text.strip().lower()
    now = time.time()
    if "today" in lower:
        return now
    if "yesterday" in lower:
        return now - 86400
    m = re.search(r"(\d+)\+?\s*days?\s*ago", lower)
    if m:
        return now - int(m.group(1)) * 86400
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
    resp = request_with_retry("GET", url, params={"content": "true"})
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
                "description": strip_html(j.get("content", "")),
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
            "description": strip_html(j.get("descriptionPlain") or j.get("description", "")),
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
                "description": strip_html(j.get("descriptionPlain", "")),
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
    max_pages = company.get("max_pages")
    jobs = []
    page = 1
    total = None
    while True:
        if max_pages is not None and page > max_pages:
            break
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


def fetch_atlassian(company):
    resp = request_with_retry("GET", "https://www.atlassian.com/endpoint/careers/listings")
    postings = resp.json()
    jobs = []
    for j in postings:
        locations = j.get("locations") or []
        updated = j.get("portalJobPost", {}).get("updatedDate")
        posted_ts = None
        if updated:
            try:
                posted_ts = datetime.strptime(updated, "%Y-%m-%d %I:%M %p").timestamp()
            except ValueError:
                posted_ts = None
        jobs.append(
            {
                "id": str(j.get("id", "")),
                "title": j.get("title", ""),
                "url": j.get("applyUrl", ""),
                "location": ", ".join(locations),
                "posted_ts": posted_ts,
            }
        )
    return jobs


def fetch_salesforce(company):
    resp = request_with_retry(
        "GET", "https://a.sfdcstatic.com/digital/xsf/careers/prod/jobs_1.json", timeout=30
    )
    data = resp.json()
    jobs = []
    for e in data.get("Report_Entry", []):
        if "United States of America" not in (e.get("Countries") or []):
            continue
        location = ", ".join(e.get("Locations") or []) or e.get("Job_Requisition_Primary_Location", "")
        jobs.append(
            {
                "id": e.get("Job_Requisition_Ref_ID", ""),
                "title": e.get("Job_Posting_Title", ""),
                "url": e.get("External_Job_Posting_Site", ""),
                "location": location,
                "country_code": "US",
                "posted_ts": parse_iso_date(e.get("External_Job_Posting_Start_Date")),
                "description": strip_html(e.get("Job_Description", "")),
            }
        )
    return jobs


def fetch_ea(company):
    jobs = []
    offset = 0
    limit = 20
    while True:
        resp = request_with_retry(
            "GET",
            "https://jobs.ea.com/en_US/careers/Home/",
            params={"jobRecordsPerPage": limit, "jobOffset": offset},
            timeout=30,
        )
        html = resp.text
        cards = re.findall(
            r'<article class="article article--result article--non-toggle".*?</article>',
            html,
            re.DOTALL,
        )
        if not cards:
            break
        for card in cards:
            title_m = re.search(
                r'<a class="link link_result" href="([^"]+)"[^>]*>\s*([^<]+?)\s*</a>', card, re.DOTALL
            )
            if not title_m:
                continue
            loc_m = re.search(r'list-item-location">([^<]+)</span>', card)
            id_m = re.search(r"list-item-id\">Role ID (\d+)</span>", card)
            url = title_m.group(1)
            jobs.append(
                {
                    "id": id_m.group(1) if id_m else url,
                    "title": title_m.group(2).strip(),
                    "url": url,
                    "location": loc_m.group(1).strip() if loc_m else "",
                }
            )
        offset += limit
    return jobs


def fetch_amd(company):
    query = company.get("query", "")
    jobs = []
    page = 1
    total = None
    while True:
        resp = request_with_retry(
            "GET",
            "https://careers.amd.com/api/jobs",
            params={
                "page": page,
                "sortBy": "relevance",
                "descending": "false",
                "internal": "false",
                "keywords": query,
            },
        )
        data = resp.json()
        if total is None:
            total = data.get("totalCount", 0)
        entries = data.get("jobs", [])
        if not entries:
            break
        for entry in entries:
            j = entry.get("data", {})
            req_id = j.get("req_id", "")
            location = f"{j.get('city', '')}, {j.get('state', '')}".strip(", ")
            jobs.append(
                {
                    "id": req_id,
                    "title": j.get("title", ""),
                    "url": f"https://careers.amd.com/careers-home/jobs/{req_id}?lang=en-us",
                    "location": location,
                    "country_code": j.get("country_code"),
                    "posted_ts": parse_iso_date(j.get("posted_date")),
                }
            )
        page += 1
        if len(jobs) >= total:
            break
    return jobs


def fetch_smartrecruiters(company):
    identifier = company["identifier"]
    query = company.get("query", "")
    url = f"https://api.smartrecruiters.com/v1/companies/{identifier}/postings"

    jobs = []
    offset = 0
    limit = 100
    total = None
    while True:
        resp = request_with_retry(
            "GET",
            url,
            params={"limit": limit, "offset": offset, "q": query},
        )
        data = resp.json()
        if total is None:
            total = data.get("totalFound", 0)
        postings = data.get("content", [])
        if not postings:
            break
        for p in postings:
            loc = p.get("location") or {}
            jobs.append(
                {
                    "id": p.get("id", ""),
                    "title": p.get("name", ""),
                    "url": f"https://jobs.smartrecruiters.com/{identifier}/{p.get('id', '')}",
                    "location": loc.get("fullLocation", ""),
                    "country_code": loc.get("country"),
                    "posted_ts": parse_iso_date(p.get("releasedDate")),
                }
            )
        offset += limit
        if offset >= total:
            break
    return jobs


def fetch_workday(company):
    host = company["host"]
    tenant = company["tenant"]
    site = company["site"]
    query = company.get("query", "")
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
            json={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": query},
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
                    "posted_ts": parse_workday_relative_date(j.get("postedOn")),
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
    "salesforce": fetch_salesforce,
    "ea": fetch_ea,
    "workable": fetch_workable,
    "bamboohr": fetch_bamboohr,
    "teamtailor": fetch_teamtailor,
    "snap": fetch_snap,
    "amd": fetch_amd,
    "atlassian": fetch_atlassian,
    "smartrecruiters": fetch_smartrecruiters,
}


def load_json(path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def title_passes_filter(title, exclude_re, include_keywords, require_keywords=None):
    if exclude_re.search(title):
        return False
    if include_keywords:
        if not any(kw.lower() in title.lower() for kw in include_keywords):
            return False
    if require_keywords:
        if not any(kw.lower() in title.lower() for kw in require_keywords):
            return False
    return True


def strip_html(text):
    if not text:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


YEARS_RE = re.compile(r"(?:(\d+)\s*(?:-|–|—|to)\s*)?(\d+)\s*(\+)?\s*years?\b", re.IGNORECASE)


def exceeds_experience_cap(text, cap, context_window=60):
    """True only if the description clearly states a requirement above `cap` years.
    Returns False (keep the job) whenever the signal is missing or ambiguous."""
    if not text:
        return False
    for m in YEARS_RE.finditer(text):
        number = int(m.group(2))
        if number <= cap:
            continue
        start, end = m.start(), m.end()
        window = text[max(0, start - context_window) : min(len(text), end + context_window)].lower()
        has_context = (
            "experience" in window
            or "years of" in window
            or "yrs of" in window
            or re.search(r"years?\s+(in|managing|working|of)\b", window)
        )
        if has_context:
            return True
    return False


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
    if len(sys.argv) < 2:
        print("usage: poller.py <profile>", file=sys.stderr)
        sys.exit(1)
    profile = sys.argv[1]
    profile_dir = ROOT / "profiles" / profile
    if not profile_dir.is_dir():
        print(f"unknown profile: {profile}", file=sys.stderr)
        sys.exit(1)
    companies_file = profile_dir / "companies.json"
    seen_file = profile_dir / "seen.json"
    filters_file = profile_dir / "filters.json"

    companies = load_json(companies_file, [])
    seen = load_json(seen_file, {})
    filters = load_json(filters_file, {"exclude_regex": "", "include_keywords": []})

    exclude_re = re.compile(filters["exclude_regex"], re.IGNORECASE)
    include_keywords = filters.get("include_keywords", [])
    require_keywords = filters.get("require_keywords", [])
    max_years_experience = filters.get("max_years_experience")
    region = filters.get("region", "us")
    if region == "us":
        location_check = is_us_job
    else:
        metro_names = region if isinstance(region, list) else [region]
        location_check = lambda job: is_in_metro(job, metro_names)

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
            if not location_check(job):
                continue
            if not title_passes_filter(job["title"], exclude_re, include_keywords, require_keywords):
                continue
            if max_years_experience is not None and exceeds_experience_cap(
                job.get("description", ""), max_years_experience
            ):
                continue
            send_discord_alert(name, job)

        seen[key] = sorted(seen_ids | current_ids)

    with open(seen_file, "w") as f:
        json.dump(seen, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
