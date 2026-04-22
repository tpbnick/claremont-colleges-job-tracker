# Claremont job viewer + refresh API (Python stdlib http.server).
FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY claremont_job_tracker.py serve_jobs.py index.html favicon.ico ./

# Railway (and most PaaS) inject PORT at runtime. Fall back to 8765 locally.
EXPOSE 8765
CMD ["sh", "-c", "python serve_jobs.py --host 0.0.0.0 --port ${PORT:-8765}"]
