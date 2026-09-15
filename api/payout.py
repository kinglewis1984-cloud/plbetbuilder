"""Weekly / season payout run.

Fires from a Vercel cron every Thursday. It does NOT send tokens — it works out
who is owed what, writes it to the `coupon_payouts` ledger as 'pending', and
Telegrams the list + a bulk-sender blob. A human then sends from the payout
wallet. (Auto-signing is a later upgrade.)

Manual trigger for testing:  /api/payout?key=<CRON_SECRET>&mode=weekly
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from build import SUPA_URL, SSB_PAYOUT_WALLET, payout_round  # noqa: E402

CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = "".join(ch for ch in os.environ.get("TELEGRAM_CHAT_ID", "") if ch in "-0123456789")
WEEKLY_CAP = int(os.environ.get("PAYOUT_WEEKLY_CAP", "200000"))
UA = {"User-Agent": "curl/8.4.0", "Accept": "application/json",
      "Content-Type": "application/json"}


def _sb(method, path, body=None, key=None):
    key = key or SERVICE_KEY
    h = dict(UA); h["apikey"] = key; h["Authorization"] = f"Bearer {key}"
    if method == "POST":
        h["Prefer"] = "return=representation"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{SUPA_URL}/rest/v1/{path}", data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
        return json.loads(raw) if raw else []


def _telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        return "no token/chat"
    try:
        body = json.dumps({"chat_id": TG_CHAT, "text": text,
                           "disable_web_page_preview": True},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=body, headers={"Content-Type": "application/json; charset=utf-8"})
        urllib.request.urlopen(req, timeout=10).read()
        return "ok"
    except Exception as e:  # noqa: BLE001
        try:
            return f"fail: {e.code} {e.read().decode()[:200]}"
        except Exception:
            return f"fail: {type(e).__name__}: {e}"


def run(mode):
    round_id, desc, rows = payout_round(mode)

    existing = _sb("GET", f"coupon_payouts?round=eq.{urllib.parse.quote(round_id)}&select=player")
    if existing:
        return {"status": "already_done", "round": round_id, "rows": len(existing)}

    payable = [r for r in rows if r.get("wallet")]
    skipped = [r for r in rows if not r.get("wallet")]
    total = sum(r["ssb"] for r in payable)

    if mode == "weekly" and total > WEEKLY_CAP:
        _telegram(
            f"⚠️ Weekend Coupon payout PAUSED\n{desc}\nRound {round_id}\n"
            f"Total {total:,} SSB exceeds the {WEEKLY_CAP:,} cap "
            f"({len(payable)} wallets). Nothing written. Check for farming / a bug, "
            f"then raise PAYOUT_WEEKLY_CAP or pay manually.")
        return {"status": "capped", "round": round_id, "total": total, "cap": WEEKLY_CAP}

    if payable:
        _sb("POST", "coupon_payouts", [
            {"round": round_id, "player": r["player"], "wallet": r["wallet"],
             "points": r["points"], "ssb": r["ssb"], "status": "pending"}
            for r in payable
        ])

    lines = [f"\U0001f3c6 Weekend Coupon — {mode} payout", desc, f"Round: {round_id}", ""]
    if payable:
        lines.append(f"Send from {SSB_PAYOUT_WALLET[:6]}…{SSB_PAYOUT_WALLET[-4:]} :")
        for r in payable:
            lines.append(f"• {r['player']}  {r['points']} pts = {r['ssb']:,} SSB  →  {r['wallet']}")
        lines.append("")
        lines.append(f"Total: {total:,} SSB to {len(payable)} wallet(s)")
    else:
        lines.append("No one to pay this round.")
    if skipped:
        lines.append("")
        lines.append("No wallet linked (not paid): " +
                     ", ".join(f"{r['player']} ({r['ssb']:,})" for r in skipped))
    if payable:
        lines.append("")
        lines.append("--- bulk-sender (address,amount) ---")
        lines.extend(f"{r['wallet']},{r['ssb']}" for r in payable)
    tg = _telegram("\n".join(lines))

    return {"status": "ok", "round": round_id, "mode": mode,
            "paid_count": len(payable), "total_ssb": total,
            "skipped_no_wallet": [r["player"] for r in skipped], "telegram": tg}


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
        if not SERVICE_KEY:
            return self._send(500, {"error": "SUPABASE_SERVICE_KEY not set"})
        mode = q.get("mode", ["weekly"])[0]
        if mode not in ("weekly", "season"):
            return self._send(400, {"error": "mode must be weekly or season"})
        try:
            result = run(mode)
        except Exception as e:  # noqa: BLE001
            _telegram(f"⚠️ Payout run FAILED ({mode}): {type(e).__name__}: {e}")
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

        # Paper account — settle finished bets + place the coming week's.
        # Best-effort: a failure here must never fail the payout run.
        try:
            from paper import run_all
            result["paper"] = run_all()
        except Exception as e:  # noqa: BLE001
            result["paper"] = {"error": f"{type(e).__name__}: {e}"}
        return self._send(200, result)
