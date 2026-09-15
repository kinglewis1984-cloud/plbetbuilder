"""Vercel serverless entry — renders the Weekend Coupon on request.

Fresh data every call; Vercel's edge caches the response for an hour and serves
stale-while-revalidate for a week, so a dead ESPN API never blanks the page.
The Friday cron (see vercel.json) just keeps the cache warm.
"""

import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build import generate_html, generate_ucl_html  # noqa: E402
from paper import generate_paper_html, run_all as paper_run_all  # noqa: E402

CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/").lower()
        q = urllib.parse.parse_qs(parsed.query)
        is_ucl = path in ("/champions-league", "/champions", "/ucl")
        is_paper = path in ("/paper", "/paper-account")

        # Manual paper-account run: /paper?run=<CRON_SECRET> (settle + place now).
        # Returns the run summary as JSON. The weekly automatic run rides the
        # Thursday payout cron.
        if is_paper and q.get("run"):
            if not (CRON_SECRET and q["run"][0] == CRON_SECRET):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"unauthorized"}')
                return
            try:
                out = paper_run_all(wipe=bool(q.get("wipe")))
            except Exception as e:  # noqa: BLE001
                out = {"error": f"{type(e).__name__}: {e}"}
            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        try:
            builder = (generate_paper_html if is_paper else
                       generate_ucl_html if is_ucl else generate_html)
            body = builder().encode("utf-8")
            status = 200
        except Exception as e:  # noqa: BLE001
            body = (
                '<meta charset="utf-8"><title>Weekend Coupon</title>'
                '<div style="font-family:sans-serif;max-width:640px;margin:40px auto">'
                "<h1>Coupon temporarily unavailable</h1>"
                f"<p>Could not build the sheet: {type(e).__name__}. "
                "The data source may be down &mdash; try again shortly.</p></div>"
            ).encode("utf-8")
            status = 503

        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header(
            "Cache-Control",
            "no-store" if is_paper else
            "public, max-age=0, s-maxage=600, stale-while-revalidate=604800",
        )
        self.end_headers()
        self.wfile.write(body)
