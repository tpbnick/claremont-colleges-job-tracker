# Claremont Colleges Job Tracker

A lightweight job listing aggregator for the [Claremont Colleges](https://www.claremont.edu/). Scrapes all seven campuses' Workday career sites and presents them in a single, searchable, sortable table.

**Live site:** [claremontcollegejobs.com](https://claremontcollegejobs.com)

---

## How it works

- A GitHub Actions workflow runs every 30 minutes, executing `claremont_job_tracker.py`
- The scraper fetches listings from each college's Workday CXS API and writes two JSON files:
  - `claremont_jobs_latest.json` — full current listing
  - `claremont_jobs_delta.json` — diff from the previous run (new/removed jobs)
- The workflow commits those files back to `main`
- Cloudflare Pages detects the commit and re-deploys the static site automatically
- `jobs_viewer.html` fetches the JSON files directly — no backend required

## Colleges covered

| College | Workday Site ID |
|---|---|
| The Claremont Colleges Services | TCCS_Careers |
| Pomona College | POM_Careers |
| Claremont Graduate University | CGU_Careers |
| Scripps College | SCR_Career_Staff |
| Claremont McKenna College | CMC_Staff |
| Harvey Mudd College | HMC_Careers |
| Pitzer College | PIT_Staff |
| Keck Graduate Institute | KGI_Careers |

## Local development

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Run the scraper once to generate JSON
python claremont_job_tracker.py

# Serve the viewer locally
python serve_jobs.py
# Open http://localhost:8765/
```

Docker is also supported:

```bash
docker compose up --build
# Open http://localhost:8765/
```

## Deployment

1. Push this repo to GitHub
2. Connect to [Cloudflare Pages](https://pages.cloudflare.com/): build command blank, output directory `/`
3. Add your custom domain in the Pages dashboard
4. Trigger the scraper once manually via Actions → "Scrape jobs" → Run workflow

The cron job (`*/30 * * * *`) handles all subsequent updates automatically.

## Disclaimer

This project is not affiliated with, endorsed by, or connected to The Claremont Colleges or any of their member institutions. It aggregates publicly available job listings from each college's Workday career site.

## License

[MIT](LICENSE)
