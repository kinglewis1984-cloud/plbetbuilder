"""Intraday settle-only checkpoints for the Paper Account.

Fires several times through each Premier League matchday (Friday through
Monday evenings — see the `crons` list in vercel.json) so results land on
/paper within a couple of hours of full-time, rather than waiting for the
Thursday payout cron. Settle-only: never places new bets (that stays on
Thursday) and is a cheap no-op when nothing pending has actually finished yet.

Manual trigger for testing:  /api/settle?key=<CRON_SECRET>
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
            result = {"pl": settle("pl"), "ucl": settle("ucl")}
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        return self._send(200, result)
