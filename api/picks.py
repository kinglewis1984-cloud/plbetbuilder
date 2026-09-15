"""Read-only JSON of the current round's suggested legs, for the Betfair-priced
coupon book in the private betting-paper app to consume.

Only the goals section (Over/Under total goals + BTTS) — the markets Betfair
reliably lists for every PL / UCL fixture.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paper import _rows_for  # noqa: E402

_GOALS = {"goals", "goals_u", "btts"}


def _picks(comp):
    out = []
    for r in _rows_for(comp):
        f = r["fx"]
        legs = [
            {"market": leg["check"][0], "line": float(leg["check"][1]),
             "text": leg["text"]}
            for leg in r["legs"] if leg["check"][0] in _GOALS
        ]
        if legs:
            out.append({
                "fixture_id": f["id"],
                "home": f.get("home") or f.get("home_abbr"),
                "away": f.get("away") or f.get("away_abbr"),
                "kickoff": f["date"],
                "legs": legs,
            })
    return out


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body = json.dumps({"pl": _picks("pl"), "ucl": _picks("ucl")}).encode()
            code = 200
        except Exception as e:  # noqa: BLE001
            body = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "public, max-age=900")
        self.end_headers()
        self.wfile.write(body)
