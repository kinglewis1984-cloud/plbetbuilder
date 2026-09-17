"""Read-only JSON of this round's ACTUAL pending paper bets (goals/BTTS
singles only), for the private Betfair coupon book to mirror exactly.

Unlike /api/picks (which independently re-derives suggest() legs from
whatever fixtures are currently upcoming - liable to drift from what /paper
actually locked in, e.g. if this is queried after /paper's own placement
round has moved on to a new week), this exposes the real bet rows already
sitting in coupon_paper_bets, so the Betfair account bets on the literal
same fixtures/markets/lines /paper did - just priced from the real Betfair
ladder instead of the fixed table.

Only covers pl/ucl/rest (the books /paper actually has) - there's no
Europa League book here to copy from, so the private app still derives UEL
picks independently via /api/picks.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paper import _read_bets  # noqa: E402

_GOALS_MARKETS = {"goals", "goals_u", "btts"}


def _bets_for(comp):
    """Same shape as /api/picks (grouped by fixture, each with a "legs" list)
    so the coupon book's place() loop works unchanged regardless of which
    endpoint a comp's bets came from."""
    by_fixture = {}
    rows = _read_bets(f"&comp=eq.{comp}&status=eq.pending&bet_type=eq.single")
    for b in rows:
        if b["market"] not in _GOALS_MARKETS:
            continue
        home, sep, away = (b["fixture"] or "").partition(" v ")
        if not sep:
            continue
        fx = by_fixture.setdefault(b["fixture_id"], {
            "fixture_id": b["fixture_id"], "home": home, "away": away,
            "kickoff": b["kickoff"], "league": b.get("league"), "legs": [],
        })
        fx["legs"].append({"market": b["market"], "line": float(b["line"]), "text": b["leg_text"]})
    return list(by_fixture.values())


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body = json.dumps({
                "pl": _bets_for("pl"), "ucl": _bets_for("ucl"), "rest": _bets_for("rest"),
            }).encode()
            code = 200
        except Exception as e:  # noqa: BLE001
            body = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        self.wfile.write(body)
