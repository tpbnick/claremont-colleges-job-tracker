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
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Iterator
from urllib.parse import urlparse

_DEBUG = os.environ.get("CLAREMONT_COLLEGE_JOBS_TRACKER_DEBUG", "").strip() == "1"

import requests

# ── Config ────────────────────────────────────────────────────────────────────

TENANT = "theclaremontcolleges"
ORIGIN = "https://theclaremontcolleges.wd1.myworkdayjobs.com"
LOCALE_PREFIX = "en-US"

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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
}

SAVE_FILE = Path(__file__).parent.parent / "data" / "claremont_jobs_latest.json"
DELTA_FILE = Path(__file__).parent.parent / "data" / "claremont_jobs_delta.json"
# Workday CXS returns HTTP 400 for limit > 20 on this tenant.
PAGE_SIZE = 20
REQUEST_DELAY = 0.35  # seconds between paginated / multi-site calls


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


def _fetch_job_detail_fields(
    session: requests.Session,
    site_id: str,
    ext: str,
    referer: str,
) -> dict[str, str]:
    """
    GET the Workday CXS job-detail JSON endpoint and extract time_type / posted_on.
    The Workday SPA calls this same endpoint; returns {} on any failure.
    """
    url = f"{ORIGIN}/wday/cxs/{TENANT}/{site_id}{ext}"
    try:
        resp = session.get(
            url,
            headers={**HEADERS, "Referer": referer},
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
    session: requests.Session,
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
        found = _fetch_job_detail_fields(session, site_id, ext_path, referer)
        for field, value in found.items():
            if not job.get(field) and value:
                job[field] = value
                enriched += 1
        time.sleep(REQUEST_DELAY)
    if _DEBUG:
        print(f"[DEBUG enrich] filled {enriched} field(s) across {len(needs)} job(s)",
              file=sys.stderr)


def fetch_site_jobs(
    session: requests.Session,
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
        headers = {**HEADERS, "Referer": referer}
        resp = session.post(
            jobs_api_url(site_id),
            headers=headers,
            json=payload,
            timeout=45,
        )
        resp.raise_for_status()
        data = resp.json()

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

    _enrich_missing_fields(session, jobs, site_id, referer)
    return jobs, total if total is not None else len(jobs)


def iter_scrape_sites() -> Iterator[tuple[str, list[dict[str, Any]], int]]:
    """Yield (site_id, jobs, api_reported_total) for each configured career home."""
    session = requests.Session()
    for i, home in enumerate(CAREER_SITE_HOMES):
        site_id = site_id_from_home_url(home)
        jobs, total = fetch_site_jobs(session, site_id, referer=home)
        yield site_id, jobs, total
        if i + 1 < len(CAREER_SITE_HOMES):
            time.sleep(REQUEST_DELAY)


def scrape_all_sites() -> list[dict[str, Any]]:
    all_jobs: list[dict[str, Any]] = []
    for site_id, jobs, total in iter_scrape_sites():
        print(f"  {site_id} ...", end=" ", flush=True)
        print(f"{len(jobs)} postings (total={total}).")
        all_jobs.extend(jobs)
    return all_jobs


# ── Persistence ───────────────────────────────────────────────────────────────


def load_previous() -> dict | None:
    if SAVE_FILE.exists():
        with open(SAVE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_current(jobs: list[dict]) -> dict:
    data = {
        "scraped_at": datetime.now(ZoneInfo("America/Los_Angeles")).isoformat(timespec="seconds"),
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

    try:
        current_jobs = scrape_all_sites()
    except requests.RequestException as e:
        print(f"\nFailed to fetch jobs: {e}", file=sys.stderr)
        return 1

    if not current_jobs:
        print("\nNo postings found. Sites may be down or credentials changed.")
        return 0

    previous_data = load_previous()
    diff = (
        diff_jobs(previous_data["jobs"], current_jobs)
        if previous_data
        else {"new": [], "removed": []}
    )

    has_changes = previous_data is None or bool(diff["new"] or diff["removed"])

    # Always save so that Workday's relative posted_on strings (e.g. "Today",
    # "Yesterday") stay current even when the job set hasn't changed.
    current_data = save_current(current_jobs)
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
