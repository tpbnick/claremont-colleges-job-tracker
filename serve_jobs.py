#!/usr/bin/env python3
"""
Local development server for jobs_viewer.html.

Serves the static files from this directory (HTML, JSON, favicon) and
redirects / → /jobs_viewer.html. In production the site is hosted on
Cloudflare Pages — this script is only needed for local testing.

  python3 serve_jobs.py
  python3 serve_jobs.py --port 9000
  open the URL printed in the terminal (default 8765).
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_PORT_TRIES = 50

ALLOWED_STATIC = frozenset({
    "/index.html",
    "/favicon.ico",
    "/claremont_jobs_latest.json",
    "/claremont_jobs_delta.json",
})


def maybe_reexec_with_venv_python() -> None:
    """Re-run with the project .venv interpreter if it exists and isn't already active."""
    venv_bin = ROOT / ".venv" / "bin"
    for name in ("python3", "python"):
        vpy = venv_bin / name
        if not vpy.is_file():
            continue
        try:
            same = Path(sys.executable).resolve() == vpy.resolve()
        except OSError:
            same = False
        if same:
            return
        os.execv(str(vpy), [str(vpy), str(ROOT / "serve_jobs.py"), *sys.argv[1:]])


class JobsRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:;",
        )
        super().end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/" or path == "":
            self.send_response(302)
            self.send_header("Location", "/index.html")
            self.end_headers()
            return
        if path not in ALLOWED_STATIC:
            self.send_error(404)
            return
        super().do_GET()

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write(
            "%s - - [%s] %s\n" % (
                self.address_string(),
                self.log_date_time_string(),
                fmt % args,
            )
        )


def bind_httpd(handler_factory, host: str, first_port: int) -> tuple[ThreadingHTTPServer, int]:
    last: OSError | None = None
    for i in range(MAX_PORT_TRIES):
        port = first_port + i
        try:
            return ThreadingHTTPServer((host, port), handler_factory), port
        except OSError as e:
            last = e
            if e.errno != errno.EADDRINUSE:
                raise
    assert last is not None
    raise last


def main() -> int:
    maybe_reexec_with_venv_python()

    p = argparse.ArgumentParser(description="Local dev server for the job viewer.")
    p.add_argument(
        "--host",
        default=DEFAULT_HOST,
        metavar="ADDR",
        help=f"Bind address (default {DEFAULT_HOST}). Use 0.0.0.0 in Docker.",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", DEFAULT_PORT)),
        metavar="N",
        help=f"First port to try (default {DEFAULT_PORT}, or $PORT env var).",
    )
    args = p.parse_args()

    httpd, port = bind_httpd(JobsRequestHandler, args.host, args.port)
    if port != args.port:
        print(f"Port {args.port} is in use; bound to {port} instead.", file=sys.stderr)

    print(f"Serving {ROOT} at http://{args.host}:{port}/")
    print(f"Open http://{'localhost' if args.host == DEFAULT_HOST else args.host}:{port}/")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
