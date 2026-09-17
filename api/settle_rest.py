"""Intraday settle-only checkpoint for the "Rest of Football" paper book.

Kept as its OWN endpoint/cron track, separate from api/settle.py (which
handles Premier League + Champions League), because a cache-miss
rest_context() costs ~20s - fetching results across 7 competitions - which
would risk blowing api/settle.py's 30s budget on top of the pl/ucl work it
already does every time it fires. Running alone here, that ~20s comfortably
fits this function's own maxDuration.

Fires several times a day, every day (see the `crons` list in vercel.json) -
rest-of-football matches land on all sorts of days, not just the PL weekend
pattern - so results reach /paper within a few hours of full-time instead of
waiting for Thursday's payout cron. Settle-only: never places new bets, and
is a cheap no-op (still pays the ~20s fetch, but writes nothing) when
nothing pending has actually finished yet.

Manual trigger for testing:  /api/settle_rest?key=<CRON_SECRET>
"""

import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        auth = self.headers.get("Authorization", "")
        ok = (CRON_SECRET and auth == f"Bearer {CRON_SECRET}") or \
             (CRON_SECRET and q.get("key", [""])[0] == CRON_SECRET)
        if not ok:
            return self._send(401, {"error": "unauthorized"})
        try:
            from paper import settle
            result = {"rest": settle("rest")}
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        return self._send(200, result)
