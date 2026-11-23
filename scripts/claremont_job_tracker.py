#!/usr/bin/env python3
"""
The Claremont Colleges (Workday) Job Tracker

Fetches open roles from the configured Workday career sites for The Claremont
Colleges tenant, saves a snapshot, and diffs against the last run.

The public HTML shell does not include listings (they load in the browser). This
script calls the same CXS JSON endpoint the site uses:

    POST /wday/cxs/<tenant>/<siteId>/jobs

Usage:
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python claremont_job_tracker.py
    .venv/bin/python claremont_job_tracker.py --all

Also writes claremont_jobs_delta.json (new postings vs previous snapshot) for jobs_viewer.html.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Iterator
from urllib.parse import urlparse

_DEBUG = os.environ.get("CLAREMONT_COLLEGE_JOBS_TRACKER_DEBUG", "").strip() == "1"

from curl_cffi import requests

# ── Config ────────────────────────────────────────────────────────────────────

TENANT = "theclaremontcolleges"
ORIGIN = "https://theclaremontcolleges.wd1.myworkdayjobs.com"
LOCALE_PREFIX = "en-US"
IMPERSONATE = "chrome"

# Career site home URLs (used as Referer; site id is the last path segment).
CAREER_SITE_HOMES = [
    "https://theclaremontcolleges.wd1.myworkdayjobs.com/TCCS_Careers",
    "https://theclaremontcolleges.wd1.myworkdayjobs.com/POM_Careers",
    "https://theclaremontcolleges.wd1.myworkdayjobs.com/en-US/CGU_Careers",
    "https://theclaremontcolleges.wd1.myworkdayjobs.com/SCR_Career_Staff",
    "https://theclaremontcolleges.wd1.myworkdayjobs.com/CMC_Staff",
    "https://theclaremontcolleges.wd1.myworkdayjobs.com/HMC_Careers",
    "https://theclaremontcolleges.wd1.myworkdayjobs.com/PIT_Staff",
    "https://theclaremontcolleges.wd1.myworkdayjobs.com/en-US/KGI_Careers",
]

# Omit User-Agent so curl_cffi uses the exact header matching its TLS impersonation.
API_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": ORIGIN,
}

SAVE_FILE = Path(__file__).parent.parent / "data" / "claremont_jobs_latest.json"
DELTA_FILE = Path(__file__).parent.parent / "data" / "claremont_jobs_delta.json"
# Workday CXS returns HTTP 400 for limit > 20 on this tenant.
PAGE_SIZE = 20
REQUEST_DELAY = 0.35  # seconds between paginated / multi-site calls
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 2.0  # seconds (exponential: 2s, 4s, 8s + jitter)
RETRY_STATUS_CODES = {403, 429, 500, 502, 503, 504}
# Allow carrying over cached listings from the previous snapshot if at most this
# many individual sites fail transiently; fail the run if more fail.
MAX_TOLERATED_SITE_FAILURES = 2


# ── Site helpers ──────────────────────────────────────────────────────────────


def site_id_from_home_url(home_url: str) -> str:
    path = urlparse(home_url).path.strip("/")
    parts = path.split("/")
    return parts[-1]


def jobs_api_url(site_id: str) -> str:
    return f"{ORIGIN}/wday/cxs/{TENANT}/{site_id}/jobs"


def job_listing_url(site_id: str, external_path: str) -> str:
    # Matches in-app links like /en-US/TCCS_Careers/job/...
    return f"{ORIGIN}/{LOCALE_PREFIX}/{site_id}{external_path}"


# Workday site slugs end with _Careers / _Staff / etc.; UI uses full school names.
SCHOOL_DISPLAY_NAMES: dict[str, str] = {
    "TCCS_Careers": "The Claremont Colleges Services",
    "POM_Careers": "Pomona College",
    "CGU_Careers": "Claremont Graduate University",
    "SCR_Career_Staff": "Scripps College",
    "CMC_Staff": "Claremont McKenna College",
    "HMC_Careers": "Harvey Mudd College",
    "PIT_Staff": "Pitzer College",
    "KGI_Careers": "Keck Graduate Institute",
}


def school_display_name(site_id: str) -> str:
    """Human-readable consortium / college name (no Workday suffix like Careers)."""
    if site_id in SCHOOL_DISPLAY_NAMES:
        return SCHOOL_DISPLAY_NAMES[site_id]
    s = site_id.replace("_", " ")
    for suf in (" Career Staff", " Careers", " Career", " Staff"):
        if s.endswith(suf):
            return s[: -len(suf)].strip()
    return s


# ── Fetching ──────────────────────────────────────────────────────────────────

def _location_from_external_path(ext: str) -> str:
    """
    Parse location from a Workday externalPath like
    /job/Claremont/Human-Resources-Business-Partner_REQ-8049-1.

    The path segment after 'job/' is the location slug.
    Slug encoding: '---' encodes a literal dash, '-' encodes a space.
    """
    parts = ext.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "job":
        slug = parts[1]
        # Decode slug: replace encoded dash before encoded space to avoid collision.
        return slug.replace("---", "\x00").replace("-", " ").replace("\x00", " - ").strip()
    return ""


class _WorkdayClient:
    """curl_cffi session with browser TLS impersonation and cookie warm-up."""

    def __init__(self) -> None:
        self.session = self._make_session()
        self._warmed_up = False

    @staticmethod
    def _make_session() -> requests.Session:
        return requests.Session(impersonate=IMPERSONATE)

    def warmup(self, home_url: str) -> None:
        """GET a career site home page so Cloudflare/Workday set session cookies."""
        try:
            resp = self.session.get(
                home_url,
                headers={"Accept-Language": "en-US,en;q=0.9"},
                timeout=30,
            )
            self._warmed_up = True
            if _DEBUG or resp.status_code >= 400:
                cf_ray = resp.headers.get("cf-ray", "")
                ray_info = f" cf-ray={cf_ray}" if cf_ray else ""
                print(
                    f"\n  [warmup] GET {home_url} -> HTTP {resp.status_code}{ray_info}",
                    file=sys.stderr,
                    flush=True,
                )
        except requests.RequestsError as exc:
            print(
                f"\n  [warmup] GET {home_url} failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    def ensure_warmed_up(self, home_url: str) -> None:
        if not self._warmed_up:
            self.warmup(home_url)

    def reset(self, home_url: str) -> None:
        """Recreate the session (fresh TLS connection & cookies) and re-warm."""
        try:
            self.session.close()
        except Exception:
            pass
        self.session = self._make_session()
        self._warmed_up = False
        self.warmup(home_url)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass


def _response_snippet(resp: requests.Response, limit: int = 200) -> str:
    body = " ".join((resp.text or "").split())
    if len(body) > limit:
        body = body[:limit] + "…"
    return body


def _post_jobs_page(
    client: _WorkdayClient,
    site_id: str,
    referer: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """POST one CXS jobs page with retry, exponential backoff, and session reset."""
    url = jobs_api_url(site_id)
    headers = {**API_HEADERS, "Referer": referer}
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        client.ensure_warmed_up(referer)
        try:
            resp = client.session.post(
                url,
                headers=headers,
                json=payload,
                timeout=45,
            )
        except requests.RequestsError as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                delay = RETRY_BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0.2, 1.0)
                print(
                    f"\n  [retry {attempt}/{MAX_RETRIES - 1}] {site_id} offset={payload['offset']}: "
                    f"{type(exc).__name__}: {exc} (waiting {delay:.1f}s)",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
                client.reset(referer)
                continue
            raise

        if resp.status_code == 200:
            return resp.json()

        cf_ray = resp.headers.get("cf-ray", "")
        ray_info = f", cf-ray={cf_ray}" if cf_ray else ""
        snippet = _response_snippet(resp)
        detail = f"HTTP {resp.status_code}{ray_info} from {url}: {snippet or '(empty body)'}"

        if resp.status_code in RETRY_STATUS_CODES and attempt < MAX_RETRIES:
            delay = RETRY_BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0.2, 1.0)
            print(
                f"\n  [retry {attempt}/{MAX_RETRIES - 1}] {site_id} offset={payload['offset']}: "
                f"{detail} (waiting {delay:.1f}s)",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
            client.reset(referer)
            continue

        raise requests.exceptions.HTTPError(detail, response=resp)

    assert last_error is not None
    raise last_error


def _fetch_job_detail_fields(
    client: _WorkdayClient,
    site_id: str,
    ext: str,
    referer: str,
) -> dict[str, str]:
    """
    GET the Workday CXS job-detail JSON endpoint and extract time_type / posted_on.
    The Workday SPA calls this same endpoint; returns {} on any failure.
    """
    url = f"{ORIGIN}/wday/cxs/{TENANT}/{site_id}{ext}"
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": ORIGIN,
        "Referer": referer,
    }
    try:
        resp = client.session.get(
            url,
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if _DEBUG:
            print(f"[DEBUG detail] GET {url} → HTTP {resp.status_code} "
                  f"keys={list(data.keys())[:8]}", file=sys.stderr)
        result: dict[str, str] = {}
        # The detail envelope varies; try common shapes.
        info = data.get("jobPostingInfo") or data
        for src_key, dst_key in (("timeType", "time_type"), ("postedOn", "posted_on"),
                                  ("locationsText", "location")):
            val = info.get(src_key) or ""
            if val:
                result[dst_key] = val
        return result
    except Exception as exc:
        if _DEBUG:
            print(f"[DEBUG detail] FAILED {url}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return {}


def _enrich_missing_fields(
    client: _WorkdayClient,
    jobs: list[dict[str, Any]],
    site_id: str,
    referer: str,
) -> None:
    """
    For jobs that still lack time_type or posted_on after the CXS list call,
    attempt to fill them from the CXS job-detail JSON endpoint.
    Failures are silently ignored.
    """
    needs = [j for j in jobs if not j.get("time_type") or not j.get("posted_on")]
    if not needs:
        return
    enriched = 0
    for job in needs:
        ext = urlparse(job.get("url", "")).path
        # Strip the locale+site prefix to recover the externalPath (/job/…)
        # e.g. /en-US/TCCS_Careers/job/… → /job/…
        idx = ext.find("/job/")
        if idx == -1:
            continue
        ext_path = ext[idx:]
        found = _fetch_job_detail_fields(client, site_id, ext_path, referer)
        for field, value in found.items():
            if not job.get(field) and value:
                job[field] = value
                enriched += 1
        time.sleep(REQUEST_DELAY)
    if _DEBUG:
        print(f"[DEBUG enrich] filled {enriched} field(s) across {len(needs)} job(s)",
              file=sys.stderr)


def fetch_site_jobs(
    client: _WorkdayClient,
    site_id: str,
    referer: str,
) -> tuple[list[dict[str, Any]], int]:
    """Return (normalized job dicts, reported total) for one career site."""
    jobs: list[dict[str, Any]] = []
    offset = 0
    total = None

    while True:
        payload = {
            "appliedFacets": {},
            "limit": PAGE_SIZE,
            "offset": offset,
            "searchText": "",
        }
        data = _post_jobs_page(client, site_id, referer, payload)

        if total is None:
            total = int(data.get("total", 0))

        batch = data.get("jobPostings") or []
        for posting in batch:
            ext = posting.get("externalPath") or ""
            title = (posting.get("title") or "").strip()
            if not ext or not title:
                continue
            location = posting.get("locationsText") or ""
            if not location:
                location = _location_from_external_path(ext)
            jobs.append(
                {
                    "title": title,
                    "url": job_listing_url(site_id, ext),
                    "site_id": site_id,
                    "site": school_display_name(site_id),
                    "location": location,
                    "time_type": posting.get("timeType") or "",
                    "posted_on": posting.get("postedOn") or "",
                }
            )

        offset += len(batch)
        if offset >= total or not batch:
            break
        time.sleep(REQUEST_DELAY)

    _enrich_missing_fields(client, jobs, site_id, referer)
    return jobs, total if total is not None else len(jobs)


def iter_scrape_sites() -> Iterator[tuple[str, list[dict[str, Any]], int]]:
    """Yield (site_id, jobs, api_reported_total) for each configured career home."""
    client = _WorkdayClient()
    try:
        for i, home in enumerate(CAREER_SITE_HOMES):
            site_id = site_id_from_home_url(home)
            jobs, total = fetch_site_jobs(client, site_id, referer=home)
            yield site_id, jobs, total
            if i + 1 < len(CAREER_SITE_HOMES):
                time.sleep(REQUEST_DELAY)
    finally:
        client.close()


def scrape_all_sites(
    previous_jobs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    prev_by_site: dict[str, list[dict[str, Any]]] = {}
    for job in previous_jobs or []:
        sid = job.get("site_id", "")
        if sid:
            prev_by_site.setdefault(sid, []).append(job)

    all_jobs: list[dict[str, Any]] = []
    failed_sites: list[tuple[str, Exception]] = []
    client = _WorkdayClient()
    try:
        for i, home in enumerate(CAREER_SITE_HOMES):
            site_id = site_id_from_home_url(home)
            print(f"  {site_id} ...", end=" ", flush=True)
            try:
                jobs, total = fetch_site_jobs(client, site_id, referer=home)
            except requests.RequestsError as exc:
                failed_sites.append((site_id, exc))
                cached = prev_by_site.get(site_id)
                if cached and len(failed_sites) <= MAX_TOLERATED_SITE_FAILURES:
                    print(f"FAILED (kept {len(cached)} cached).", flush=True)
                    print(
                        f"  [warn] {site_id} failed ({exc}); carrying over "
                        f"{len(cached)} cached postings from previous snapshot.",
                        file=sys.stderr,
                        flush=True,
                    )
                    all_jobs.extend(cached)
                else:
                    print("FAILED.", flush=True)
                    raise
            else:
                print(f"{len(jobs)} postings (total={total}).", flush=True)
                all_jobs.extend(jobs)

            if i + 1 < len(CAREER_SITE_HOMES):
                time.sleep(REQUEST_DELAY)
    finally:
        client.close()
    return all_jobs


# ── Persistence ───────────────────────────────────────────────────────────────


def load_previous() -> dict | None:
    if SAVE_FILE.exists():
        with open(SAVE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return None


def annotate_first_seen(
    current_jobs: list[dict[str, Any]],
    previous_data: dict | None,
    scraped_at: str,
) -> None:
    """Stamp first_seen on newly discovered jobs and preserve it on existing ones."""
    if not previous_data:
        return
    prev_by_key = {job_key(j): j for j in previous_data.get("jobs", [])}
    for job in current_jobs:
        prev = prev_by_key.get(job_key(job))
        if prev is None:
            job["first_seen"] = scraped_at
        elif prev.get("first_seen"):
            job["first_seen"] = prev["first_seen"]


def save_current(jobs: list[dict], scraped_at: str | None = None) -> dict:
    ts = scraped_at or datetime.now(ZoneInfo("America/Los_Angeles")).isoformat(timespec="seconds")
    data = {
        "scraped_at": ts,
        "total": len(jobs),
        "jobs": jobs,
    }
    with open(SAVE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data


def write_delta(
    previous_data: dict | None,
    current_data: dict,
    diff: dict[str, list[dict]],
) -> None:
    """Snapshot of new/removed since last file (for the HTML viewer)."""
    prev_time = previous_data.get("scraped_at") if previous_data else None
    payload = {
        "previous_scraped_at": prev_time,
        "current_scraped_at": current_data.get("scraped_at"),
        "new_count": len(diff["new"]),
        "removed_count": len(diff["removed"]),
        "new_jobs": diff["new"],
    }
    with open(DELTA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


# ── Diffing ───────────────────────────────────────────────────────────────────


def job_key(job: dict) -> str:
    return job["url"] if job.get("url") else f"{job['title']}|{job.get('site_id', '')}"


def diff_jobs(previous: list[dict], current: list[dict]) -> dict:
    prev_keys = {job_key(j): j for j in previous}
    curr_keys = {job_key(j): j for j in current}

    new_jobs = [curr_keys[k] for k in curr_keys if k not in prev_keys]
    removed_jobs = [prev_keys[k] for k in prev_keys if k not in curr_keys]

    return {"new": new_jobs, "removed": removed_jobs}


# ── Display ───────────────────────────────────────────────────────────────────


def print_job(job: dict, prefix: str = ""):
    loc = job.get("location") or "—"
    tt = job.get("time_type") or "—"
    posted = job.get("posted_on") or ""
    extra = f"  |  {posted}" if posted else ""
    print(f"{prefix}  * {job['title']}")
    print(f"{prefix}     {job.get('site', '')}  |  {loc}  |  {tt}{extra}")
    if job.get("url"):
        print(f"{prefix}     {job['url']}")


def print_all_jobs(jobs: list[dict]):
    print(f"\n{'-' * 60}")
    print(f"  ALL CURRENT JOBS ({len(jobs)} total)")
    print(f"{'-' * 60}")
    for i, job in enumerate(jobs, 1):
        print(f"\n  [{i}]", end="")
        print_job(job)


def print_diff(diff: dict, prev_time: str):
    new = diff["new"]
    removed = diff["removed"]

    print(f"\n{'=' * 60}")
    print(f"  CHANGES since last run ({prev_time})")
    print(f"{'=' * 60}")

    if new:
        print(f"\n  NEW ({len(new)}):")
        for job in new:
            print()
            print_job(job, prefix="  ")
    else:
        print("\n  No new postings since last run.")

    if removed:
        print(f"\n  REMOVED ({len(removed)}):")
        for job in removed:
            print()
            print_job(job, prefix="  ")
    else:
        print("\n  No removed postings since last run.")


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Track Claremont Colleges Workday postings.")
    parser.add_argument("-a", "--all", action="store_true", help="Print every current listing.")
    args = parser.parse_args()

    print(f"\n{'=' * 60}")
    print("  The Claremont Colleges — Workday job tracker")
    print(f"{'=' * 60}\n")
    print("Fetching postings (CXS API)...\n")

    previous_data = load_previous()

    try:
        current_jobs = scrape_all_sites(
            previous_jobs=previous_data["jobs"] if previous_data else None
        )
    except requests.RequestsError as e:
        print(f"\nFailed to fetch jobs: {e}", file=sys.stderr)
        return 1

    if not current_jobs:
        print("\nNo postings found. Sites may be down or credentials changed.")
        return 0

    scraped_at = datetime.now(ZoneInfo("America/Los_Angeles")).isoformat(timespec="seconds")
    annotate_first_seen(current_jobs, previous_data, scraped_at)

    diff = (
        diff_jobs(previous_data["jobs"], current_jobs)
        if previous_data
        else {"new": [], "removed": []}
    )

    has_changes = previous_data is None or bool(diff["new"] or diff["removed"])

    # Always save so that Workday's relative posted_on strings (e.g. "Today",
    # "Yesterday") stay current even when the job set hasn't changed.
    current_data = save_current(current_jobs, scraped_at=scraped_at)
    write_delta(previous_data, current_data, diff)
    print(f"\nTotal postings scraped: {len(current_jobs)}")
    print(f"Results saved to:        {SAVE_FILE}")
    print(f"Delta written to:        {DELTA_FILE}")
    if not has_changes:
        print("  (no structural changes — timestamps and posted_on fields refreshed)")

    if previous_data:
        print_diff(diff, previous_data["scraped_at"])
    else:
        print("\n  No previous data — baseline run.")
        print("  Run again later to see new and removed postings.")

    if args.all:
        print_all_jobs(current_jobs)
    else:
        print(f"\n  Tip: run with --all to print every listing.\n")

    print(f"\n{'=' * 60}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
