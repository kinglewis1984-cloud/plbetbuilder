"""Paper account — forward test of the model's own coupon.

Each week it auto-places the legs `suggest()` would put on (flat GBP 10 singles,
plus one GBP 10 combined "coupon of the week" builder), then settles them from
ESPN results. Premier League and Champions League are kept as separate books,
each starting from a GBP 1,000 bankroll. No real money — a bookkeeping exercise.

Odds: "Over 2.5 goals" and "Both teams to score" legs are priced from real
bookmaker odds (The Odds API, free tier — needs env `ODDS_API_KEY`) taken at
placement time; every other leg (Over/Under 1.5/3.5 goals, corners, cards, red)
comes from the fixed `PRICES` table. Falls back entirely to the table if the key
is missing or the call fails. Each bet records `price_src` = 'real' | 'table'.

Driven from `api/payout.py` (the Thursday cron): `run_all()` settles everything
that has finished and places everything due for the coming week. The dashboard is
`generate_paper_html()`, served at /paper.
"""
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import build
from build import (
    SUPA_URL, SUPA_KEY, compute_rows, fixtures, grade, suggest, team_blends,
    ucl_context, uk_now, weekend_fixtures, weekend_windows, _match_totals,
    _ucl_scoreboard_events, _ucl_cache_get, _ucl_cache_put, rest_context,
)

_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()

BOOKS = ("pl", "ucl", "rest")
STAKES = {"pl": 10.0, "ucl": 10.0, "rest": 10.0}
START_BANKROLL = 1000.0

# One "comp" can span several real leagues (rest of football does), so every
# bet also records which actual league it's on - lets the Recent Bets table
# filter down to just one (e.g. Ligue 1) instead of the whole rest-of-
# football bucket.
LEAGUE_LABELS = {
    "esp.1": "La Liga", "ger.1": "Bundesliga", "ita.1": "Serie A",
    "fra.1": "Ligue 1", "eng.2": "Championship", "eng.fa": "FA Cup",
    "eng.league_cup": "EFL Cup",
}


def _league_label(comp, f):
    if comp == "pl":
        return "Premier League"
    if comp == "ucl":
        return "Champions League"
    return LEAGUE_LABELS.get(f.get("league"), "Rest of Football")

# Realistic-ish Premier League bookmaker prices. Keyed by the (metric, line)
# tuple `suggest()` puts in each leg's "check". Tweak in one place; history can
# be re-priced later by re-running settle with new numbers.
PRICES = {
    ("goals", 2.5): 1.95,
    ("goals", 1.5): 1.30,
    ("goals_u", 3.5): 1.40,
    ("btts", 0): 1.83,
    ("corners", 10.5): 2.10,
    ("corners", 9.5): 1.80,
    ("corners", 8.5): 1.55,
    ("cards", 4.5): 2.75,
    ("cards", 3.5): 2.00,
    ("cards", 2.5): 1.45,
    ("red", 1): 4.50,
}

UA = {"User-Agent": "curl/8.4.0", "Accept": "application/json",
      "Content-Type": "application/json"}

# --- real odds (The Odds API, free tier) -----------------------------------
# Free tier covers the featured "totals" market (main Over/Under 2.5 goals line)
# and, where the plan allows it, "btts". Everything else stays on PRICES.
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
_ODDS_SPORT = {"pl": "soccer_epl", "ucl": "soccer_uefa_champs_league"}
_REAL_MARKETS = {("goals", 2.5), ("btts", 0.0)}


def _norm_tokens(name):
    return {w for w in re.findall(r"[a-z]{4,}", (name or "").lower())}


def _fetch_odds(sport):
    if not ODDS_API_KEY:
        return []
    for markets in ("totals,btts", "totals"):
        url = (f"https://api.the-odds-api.com/v4/sports/{sport}/odds"
               f"?apiKey={ODDS_API_KEY}&regions=uk&markets={markets}"
               f"&oddsFormat=decimal")
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (400, 422):   # market not on this plan — retry simpler
                continue
            return []
        except Exception:  # noqa: BLE001
            return []
    return []


def _real_odds(comp):
    """[(home_tokens, away_tokens, {(metric,line): price})] for upcoming games."""
    if comp not in _ODDS_SPORT:   # e.g. "rest" - spans too many leagues for one sport key
        return []
    events = _fetch_odds(_ODDS_SPORT[comp])
    out = []
    for ev in events:
        prices = {}
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                k = mk.get("key")
                for o in mk.get("outcomes", []):
                    if k == "totals" and o.get("name") == "Over" \
                            and abs(float(o.get("point", 0)) - 2.5) < 0.01:
                        prices[("goals", 2.5)] = max(
                            prices.get(("goals", 2.5), 0), float(o["price"]))
                    elif k == "btts" and o.get("name", "").lower() == "yes":
                        prices[("btts", 0.0)] = max(
                            prices.get(("btts", 0.0), 0), float(o["price"]))
        if prices:
            out.append((_norm_tokens(ev.get("home_team")),
                        _norm_tokens(ev.get("away_team")),
                        {k: round(v, 2) for k, v in prices.items()}))
    return out


def _odds_for(fx, book):
    """Real-odds dict for one ESPN fixture, matched on team-name tokens."""
    h = _norm_tokens(fx.get("home") or fx.get("home_abbr"))
    a = _norm_tokens(fx.get("away") or fx.get("away_abbr"))
    for oh, oa, prices in book:
        if (h & oh) and (a & oa):
            return prices
    return {}


# --------------------------------------------------------------------------- #
#  Supabase
# --------------------------------------------------------------------------- #
def _sb(method, path, body=None, key=None, prefer=None):
    key = key or _SERVICE_KEY
    h = dict(UA)
    h["apikey"] = key
    h["Authorization"] = f"Bearer {key}"
    if prefer:
        h["Prefer"] = prefer
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{SUPA_URL}/rest/v1/{path}", data=data,
                                 headers=h, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else []


def _read_bets(where=""):
    return _sb("GET", f"coupon_paper_bets?select=*&order=placed_at.asc{where}",
               key=SUPA_KEY)


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def _dt(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _future(iso):
    try:
        return _dt(iso) > datetime.now(timezone.utc)
    except Exception:  # noqa: BLE001
        return False


def _price(check):
    return PRICES.get(tuple(check))


def _name(f):
    home = f.get("home") or f.get("home_abbr") or "?"
    away = f.get("away") or f.get("away_abbr") or "?"
    return f"{home} v {away}"


def _leg_price(check, fx_odds):
    """(price, src) — real bookmaker odds if we have them, else the table."""
    key = (check[0], float(check[1]))
    real = fx_odds.get(key)
    if real:
        return real, "real"
    tbl = _price(check)
    return (tbl, "table") if tbl is not None else (None, None)


def _rows_for(comp):
    """(list of {fx, legs, score, h, a}) sorted best-first, for the upcoming
    round. h/a are each side's blended per-game stats behind the model's
    number - carried through so a leg's pick can be explained, not just
    stated."""
    if comp == "pl":
        blends = team_blends()
        (_, _), fx = weekend_fixtures(weekend_windows()[1])
        return [{"fx": r["fx"], "legs": r["legs"], "score": r["score"],
                 "h": r["h"], "a": r["a"], "x": r["x"]}
                for r in compute_rows(fx, blends)]
    upcoming, _ = ucl_context() if comp == "ucl" else rest_context()
    rows = [{"fx": u["fx"], "legs": suggest(u["x"]), "score": u["score"],
             "h": u.get("h"), "a": u.get("a"), "x": u["x"]}
            for u in upcoming]
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def _opening_run(matches, key_for, key_against):
    """Current-season matches only (oldest first): has this team scored AND
    conceded in every one of its opening league matches so far?"""
    season_start = f"{build.THIS_SEASON}-08-01"
    cur = sorted((m for m in matches if m["date"] >= season_start), key=lambda m: m["date"])
    if len(cur) < 3:
        return None
    both = sum(1 for m in cur if m[key_for] > 0 and m[key_against] > 0)
    if both != len(cur):
        return None
    return {"played": len(cur), "goals": int(sum(m[key_for] for m in cur))}


def _btts_bullet(name, matches, is_home_side):
    if not matches:
        return None
    last10 = matches[:10]
    hits10 = sum(1 for m in last10 if m["btts"])
    side = [m for m in matches if m["home"] == is_home_side][:20]
    word = "at home" if is_home_side else "away"
    if len(side) >= 5:
        hits_side = sum(1 for m in side if m["btts"])
        pct = round(100 * (hits10 + hits_side) / (len(last10) + len(side)))
        return (f"{name} have recorded BTTS in {hits10} of their last {len(last10)} matches "
                f"and {hits_side} of their last {len(side)} {word} "
                f"&mdash; a {pct}% hit rate across both samples.")
    pct = round(100 * hits10 / len(last10)) if last10 else 0
    return f"{name} have recorded BTTS in {hits10} of their last {len(last10)} matches &mdash; {pct}%."


def _goals_bullet(name, matches, line, over):
    if not matches:
        return None
    last10 = matches[:10]
    hits = sum(1 for m in last10 if (m["total"] > line) == over)
    pct = round(100 * hits / len(last10)) if last10 else 0
    word = "over" if over else "under"
    return (f"{name} have gone {word} {line:g} combined goals in {hits} of their "
            f"last {len(last10)} matches &mdash; {pct}%.")


def _corners_bullet(name, matches, line, over):
    sample = [m for m in matches if m.get("has_stats")][:10]
    if not sample:
        return None
    word = "over" if over else "under"
    hits = sum(1 for m in sample if (m["corners"] > line) == over)
    pct = round(100 * hits / len(sample)) if sample else 0
    return (f"{name} have gone {word} {line:g} combined corners in {hits} of their "
            f"last {len(sample)} matches with data &mdash; {pct}%.")


def _cards_bullet(name, matches, line, over):
    sample = [m for m in matches if m.get("has_stats")][:10]
    if not sample:
        return None
    word = "over" if over else "under"
    hits = sum(1 for m in sample if (m["cards"] > line) == over)
    pct = round(100 * hits / len(sample)) if sample else 0
    return (f"{name} have gone {word} {line:g} combined cards in {hits} of their "
            f"last {len(sample)} matches with data &mdash; {pct}%.")


def _key_stats(bullets):
    bullets = [b for b in bullets if b]
    if not bullets:
        return None
    items = "".join(f"<li>{b}</li>" for b in bullets)
    return f'<div class="key-stats"><span class="ks-tag">THE KEY STATS</span><ul>{items}</ul></div>'


def _reason_for(leg, x, h, a, home, away, fx=None, comp=None, league_label=None):
    """Human-readable explanation of why the model picked this leg. Goals,
    BTTS, corners and cards get a real "last 10 matches" streak breakdown
    (fetched fresh - only ever called once, at placement time, and the
    result is stored - never recomputed on a page view); red-card risk
    keeps the simpler season-total comparison (too rare an event for a
    10-match sample to say much)."""
    if h is None or a is None or x is None:
        return None
    metric, line = leg["check"]
    if metric == "red":
        return (f"Combined red cards last season: {h['rc'] + a['rc']:.0f} "
                f"({home} {h['rc']:.0f}, {away} {a['rc']:.0f})")
    if metric not in ("goals", "goals_u", "btts", "corners", "cards") or fx is None:
        return None

    hl = build.team_domestic_league(fx.get("home_id"), comp, fx.get("league"))
    al = build.team_domestic_league(fx.get("away_id"), comp, fx.get("league"))
    hm = build.team_recent_matches(fx.get("home_id"), hl)
    am = build.team_recent_matches(fx.get("away_id"), al)
    label = league_label or "league"

    if metric == "btts":
        bullets = [_btts_bullet(home, hm, True), _btts_bullet(away, am, False)]
        for who, matches in ((away, am), (home, hm)):
            r = _opening_run(matches, "gf", "ga")
            if r:
                bullets.append(f"{who} have scored and conceded in all {r['played']} of their opening "
                               f"{label} matches, scoring {r['goals']} goals across those fixtures.")
                break
        return _key_stats(bullets)
    if metric in ("goals", "goals_u"):
        over = metric == "goals"
        return _key_stats([_goals_bullet(home, hm, line, over), _goals_bullet(away, am, line, over)])
    if metric == "corners":
        return _key_stats([_corners_bullet(home, hm, line, True), _corners_bullet(away, am, line, True)])
    if metric == "cards":
        return _key_stats([_cards_bullet(home, hm, line, True), _cards_bullet(away, am, line, True)])
    return None


# --------------------------------------------------------------------------- #
#  place
# --------------------------------------------------------------------------- #
def place(comp):
    rows = [r for r in _rows_for(comp) if _future(r["fx"]["date"])]
    if not rows:
        return {"comp": comp, "placed": 0, "note": "no upcoming fixtures"}

    book = _real_odds(comp)
    round_id = f"{comp}:{_dt(rows[0]['fx']['date']).date().isoformat()}"
    bets = []
    real_used = 0

    for r in rows:
        f = r["fx"]
        fx_odds = _odds_for(f, book)
        league_label = _league_label(comp, f)
        home = f.get("home") or f.get("home_abbr")
        away = f.get("away") or f.get("away_abbr")
        for leg in r["legs"]:
            pr, src = _leg_price(leg["check"], fx_odds)
            if pr is None:
                continue
            if src == "real":
                real_used += 1
            reason = _reason_for(leg, r.get("x"), r.get("h"), r.get("a"), home, away,
                                  fx=f, comp=comp, league_label=league_label)
            bets.append({
                "comp": comp, "league": league_label,
                "round": round_id, "bet_type": "single",
                "fixture_id": f["id"], "fixture": _name(f), "kickoff": f["date"],
                "market": leg["check"][0], "line": float(leg["check"][1]),
                "leg_text": leg["text"], "legs": None, "reason": reason,
                "price": pr, "price_src": src, "stake": STAKES[comp], "status": "pending",
            })

    # one combined builder on the top-rated fixture of the round
    top = rows[0]
    tf = top["fx"]
    tf_odds = _odds_for(tf, book)
    top_priced = []
    for leg in top["legs"]:
        pr, src = _leg_price(leg["check"], tf_odds)
        if pr is not None:
            top_priced.append((leg["check"][0], float(leg["check"][1]),
                               leg["text"], pr, src))
    if len(top_priced) >= 2:
        combo = 1.0
        for *_, p, _s in top_priced:
            combo *= p
        srcs = {s for *_, s in top_priced}
        src = "real" if srcs == {"real"} else ("mixed" if "real" in srcs else "table")
        bets.append({
            "comp": comp, "league": _league_label(comp, tf),
            "round": round_id, "bet_type": "builder",
            "fixture_id": tf["id"], "fixture": _name(tf), "kickoff": tf["date"],
            "market": "builder", "line": 0.0,
            "leg_text": " + ".join(t for _, _, t, _, _ in top_priced),
            "legs": [{"market": m, "line": l, "text": t}
                     for m, l, t, _, _ in top_priced],
            "reason": None,   # combines several legs - no single "reason" applies
            "price": round(combo, 2), "price_src": src,
            "stake": STAKES[comp], "status": "pending",
        })

    if not bets:
        return {"comp": comp, "placed": 0, "note": "nothing priced"}

    url = "coupon_paper_bets?on_conflict=comp,fixture_id,bet_type,market,line"
    pref = "return=minimal,resolution=ignore-duplicates"
    try:
        _sb("POST", url, bets, prefer=pref)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        # table predates the price_src column — drop it and retry
        for b in bets:
            b.pop("price_src", None)
        _sb("POST", url, bets, prefer=pref)
    return {"comp": comp, "round": round_id, "placed": len(bets),
            "singles": sum(1 for b in bets if b["bet_type"] == "single"),
            "builders": sum(1 for b in bets if b["bet_type"] == "builder"),
            "real_priced_legs": real_used, "odds_feed": bool(book)}


# --------------------------------------------------------------------------- #
#  settle
# --------------------------------------------------------------------------- #
def _pl_actuals(kickoffs):
    d1 = (min(_dt(k) for k in kickoffs) - timedelta(days=1)).strftime("%Y%m%d")
    d2 = (max(_dt(k) for k in kickoffs) + timedelta(days=2)).strftime("%Y%m%d")
    return {f["id"]: f["result"] for f in fixtures(d1, d2) if f["result"]}


def _ucl_actuals():
    out = {}
    for ev in _ucl_scoreboard_events():
        comp = ev["competitions"][0]
        if not comp["status"]["type"].get("completed"):
            continue
        cs = comp["competitors"]
        h = next(c for c in cs if c["homeAway"] == "home")
        a = next(c for c in cs if c["homeAway"] == "away")
        try:
            hs, as_ = int(h.get("score")), int(a.get("score"))
        except (TypeError, ValueError):
            continue
        out[ev["id"]] = _match_totals(comp, cs, hs, as_)
    return out


def _rest_actuals():
    """Unlike _ucl_actuals (one cheap scoreboard call, always fetched fresh),
    rest-of-football spans 7 competitions (~20s) - use the cached
    rest_context() so settle() doesn't pay that cost on every cron tick."""
    _, finished = rest_context()
    return finished


def _leg_ok(market, line, a):
    """grade() with a guard: missing corner data -> None (void), not a loss."""
    if a is None or not a.get("has_stats"):
        return None
    if market == "corners" and a.get("corners", 0) == 0:
        return None
    return grade((market, line), a)


def settle(comp):
    pending = _read_bets(f"&comp=eq.{comp}&status=eq.pending")
    if not pending:
        return {"comp": comp, "settled": 0}

    kickoffs = [b["kickoff"] for b in pending if b.get("kickoff")]
    try:
        if comp == "pl":
            actuals = _pl_actuals(kickoffs)
        elif comp == "ucl":
            actuals = _ucl_actuals()
        else:
            actuals = _rest_actuals()
    except Exception as e:  # noqa: BLE001
        return {"comp": comp, "settled": 0, "error": f"{type(e).__name__}: {e}"}

    done = 0
    for b in pending:
        a = actuals.get(b["fixture_id"])
        if a is None:
            continue  # not finished yet — try again next run

        if b["bet_type"] == "single":
            ok = _leg_ok(b["market"], float(b["line"]), a)
        else:
            legs = b.get("legs") or []
            checks = [_leg_ok(l["market"], float(l["line"]), a) for l in legs]
            if any(c is None for c in checks):
                ok = None
            else:
                ok = all(checks)

        if ok is None:
            status, pnl = "void", 0.0
        elif ok:
            status, pnl = "won", round(b["stake"] * (float(b["price"]) - 1), 2)
        else:
            status, pnl = "lost", -float(b["stake"])

        _sb("PATCH", f"coupon_paper_bets?id=eq.{b['id']}",
            {"status": status, "pnl": pnl,
             "settled_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
            prefer="return=minimal")
        done += 1

    return {"comp": comp, "settled": done, "pending_left": len(pending) - done}


# --------------------------------------------------------------------------- #
#  orchestration
# --------------------------------------------------------------------------- #
# Throttled "settle on page view" — borrows the small generic key/value cache
# table the Champions League page already uses (`coupon_ucl_cache`), under its
# own key, so a visit to /paper can trigger a fresh settle check between the
# scheduled cron checkpoints, without hammering ESPN on every single page load.
_SETTLE_CHECK_TTL = 20 * 60  # seconds


def _maybe_settle_on_view():
    if not _SERVICE_KEY:
        return
    try:
        row = _ucl_cache_get().get("paper_settle")
        if row:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(row["refreshed_at"])).total_seconds()
            if age < _SETTLE_CHECK_TTL:
                return
        _ucl_cache_put("paper_settle", {"at": datetime.now(timezone.utc)
                                         .isoformat(timespec="seconds")})
        # "rest" deliberately excluded here: unlike pl/ucl (one cheap scoreboard
        # call each), a cache-miss rest_context() costs ~20s - fine on the
        # 60s-budget Thursday payout cron, too slow to risk on a live page view.
        settle("pl")
        settle("ucl")
    except Exception:
        pass  # best-effort — a broken check must never break the page


def wipe_pending(comp=None):
    """Delete un-settled bets so the next place() can re-price them. Settled
    history is never touched."""
    where = "status=eq.pending" + (f"&comp=eq.{comp}" if comp else "")
    _sb("DELETE", f"coupon_paper_bets?{where}", prefer="return=minimal")
    return {"wiped_pending": where}


def run_all(wipe=False):
    out = {}
    if wipe:
        try:
            out["wipe"] = wipe_pending()
        except Exception as e:  # noqa: BLE001
            out["wipe"] = {"error": f"{type(e).__name__}: {e}"}
    steps = [(f"settle_{c}", settle, c) for c in BOOKS] + [(f"place_{c}", place, c) for c in BOOKS]
    for step, fn, arg in steps:
        try:
            out[step] = fn(arg)
        except Exception as e:  # noqa: BLE001
            out[step] = {"error": f"{type(e).__name__}: {e}"}
    return out


# --------------------------------------------------------------------------- #
#  dashboard
# --------------------------------------------------------------------------- #
_MARKET_LABEL = {
    "goals": "Goals O", "goals_u": "Goals U", "corners": "Corners O",
    "cards": "Cards O", "btts": "BTTS", "red": "Red card", "builder": "Builder",
}


def _book(bets):
    settled = [b for b in bets if b["status"] in ("won", "lost")]
    pnl = round(sum(float(b["pnl"] or 0) for b in bets if b["status"] != "pending"), 2)
    staked = round(sum(float(b["stake"]) for b in settled), 2)
    return {
        "bankroll": round(START_BANKROLL + pnl, 2),
        "pnl": pnl,
        "roi": round(100 * pnl / staked, 1) if staked else 0.0,
        "won": sum(1 for b in settled if b["status"] == "won"),
        "lost": sum(1 for b in settled if b["status"] == "lost"),
        "void": sum(1 for b in bets if b["status"] == "void"),
        "pending": sum(1 for b in bets if b["status"] == "pending"),
        "staked": staked,
    }


def _by_market(bets):
    rows = {}
    for b in bets:
        if b["status"] not in ("won", "lost"):
            continue
        k = b["market"]
        r = rows.setdefault(k, {"w": 0, "l": 0, "pnl": 0.0})
        r["w" if b["status"] == "won" else "l"] += 1
        r["pnl"] += float(b["pnl"] or 0)
    out = []
    for k, r in sorted(rows.items(), key=lambda kv: kv[1]["pnl"]):
        n = r["w"] + r["l"]
        out.append((_MARKET_LABEL.get(k, k), n, r["w"], r["l"],
                    round(100 * r["w"] / n) if n else 0, round(r["pnl"], 2)))
    return out


def _fmt(v, money=True):
    s = f"{abs(v):,.2f}" if money else f"{abs(v)}"
    sign = "-" if v < 0 else ("+" if money and v > 0 else "")
    return f"{sign}£{s}" if money else f"{sign}{s}"


# The model's goals section: Over/Under total goals + BTTS. Isolating its P&L
# answers "is the goals model itself profitable?" without corners/cards/builders.
_GOALS_MARKETS = {"goals", "goals_u", "btts"}

# Hit-rate bars (like the coupon's "model accuracy" bars, but for placed paper
# bets): singles only, both books combined, bucketed goals / corners / cards / BTTS / red card.
_METRIC_BUCKETS = [
    ("Goals", "goals", {"goals", "goals_u"}),
    ("Corners", "corners", {"corners"}),
    ("Cards", "cards", {"cards"}),
    ("BTTS", "btts", {"btts"}),
    ("Red card", "red", {"red"}),
]


def _metric_bars(bets):
    out = []
    for label, css, markets in _METRIC_BUCKETS:
        sub = [b for b in bets if b["bet_type"] == "single" and b["market"] in markets
               and b["status"] in ("won", "lost")]
        w = sum(1 for b in sub if b["status"] == "won")
        n = len(sub)
        out.append((label, css, round(100 * w / n) if n else 0, w, n))
    return out


def _no_red_bankroll(bets):
    """Bankroll as if red-card singles were never placed (their P&L excluded)."""
    pnl = sum(float(b["pnl"] or 0) for b in bets
              if b["status"] != "pending"
              and not (b["bet_type"] == "single" and b["market"] == "red"))
    return round(START_BANKROLL + pnl, 2)


def _slice(bets, markets):
    sub = [b for b in bets if b["bet_type"] == "single" and b["market"] in markets]
    settled = [b for b in sub if b["status"] in ("won", "lost")]
    pnl = round(sum(float(b["pnl"] or 0) for b in sub if b["status"] != "pending"), 2)
    staked = round(sum(float(b["stake"]) for b in settled), 2)
    return {
        "pnl": pnl,
        "roi": round(100 * pnl / staked, 1) if staked else 0.0,
        "won": sum(1 for b in settled if b["status"] == "won"),
        "lost": sum(1 for b in settled if b["status"] == "lost"),
        "void": sum(1 for b in sub if b["status"] == "void"),
        "pending": sum(1 for b in sub if b["status"] == "pending"),
    }


def generate_paper_html():
    _maybe_settle_on_view()
    try:
        bets = _read_bets()
    except Exception as e:  # noqa: BLE001
        bets = []
        err = f"{type(e).__name__}: {e}"
    else:
        err = ""

    books = {c: _book([b for b in bets if b["comp"] == c]) for c in BOOKS}
    combined_pnl = round(sum(books[c]["pnl"] for c in BOOKS), 2)

    goals = {c: _slice([b for b in bets if b["comp"] == c], _GOALS_MARKETS)
             for c in BOOKS}
    goals_all = _slice(bets, _GOALS_MARKETS)

    metric_bars = _metric_bars(bets)
    bars_total = sum(n for _, _, _, _, n in metric_bars)
    bars = "".join(
        f'<div class="bar-row"><span class="bar-lbl">{label.upper()}</span>'
        f'<div class="bar-track"><span class="bar-fill {css}" style="width:{pct}%"></span></div>'
        f'<span class="bar-pct">{pct}%</span></div>'
        for label, css, pct, w, n in metric_bars
    )

    def goals_row(label, g):
        rec = f"{g['won']}W-{g['lost']}L" + (f"-{g['void']}V" if g['void'] else "")
        return (f"<tr><td>{label}</td>"
                f"<td class='{'up' if g['pnl'] >= 0 else 'down'}'>{_fmt(g['pnl'])}</td>"
                f"<td>{g['roi']}%</td><td>{rec}</td><td>{g['pending']} open</td></tr>")

    def card(title, c):
        bk = books[c]
        cls = "up" if bk["pnl"] >= 0 else "down"
        book_bets = [b for b in bets if b["comp"] == c]
        no_red = _no_red_bankroll(book_bets)
        no_red_cls = "up" if no_red >= START_BANKROLL else "down"
        mk = "".join(
            f"<tr><td>{lbl}</td><td>{n}</td><td>{w}-{l}</td><td>{sr}%</td>"
            f"<td class='{'up' if p >= 0 else 'down'}'>{_fmt(p)}</td></tr>"
            for lbl, n, w, l, sr, p in _by_market(book_bets)
        ) or "<tr><td colspan=5 class='muted'>no settled bets yet</td></tr>"
        return f"""
      <section class="card">
        <h2>{title}</h2>
        <div class="bal-row">
          <div class="bal-col"><span class="bal-lbl">Without red cards</span>
            <p class="big {no_red_cls}">{_fmt(no_red)}</p></div>
          <div class="bal-col"><span class="bal-lbl">With red cards</span>
            <p class="big {cls}">{_fmt(bk['bankroll'])}</p></div>
        </div>
        <p class="sub">{_fmt(bk['pnl'])} &middot; ROI {bk['roi']}% &middot;
           {bk['won']}W-{bk['lost']}L{('-' + str(bk['void']) + 'V') if bk['void'] else ''}
           &middot; {bk['pending']} open</p>
        <table><thead><tr><th>Market</th><th>N</th><th>W-L</th><th>Strike</th><th>P&amp;L</th></tr></thead>
        <tbody>{mk}</tbody></table>
      </section>"""

    _COMP_LABEL = {"pl": "Premier League", "ucl": "Champions League", "rest": "Rest of Football"}

    def history_html():
        """Separate from Recent Bets: settled bets only, grouped by round
        (one placement batch), most recently settled round first - a clean
        week-by-week record instead of a flat filterable log."""
        settled = [b for b in bets if b["status"] in ("won", "lost", "void")]
        if not settled:
            return ""
        by_round = {}
        for b in settled:
            by_round.setdefault(b["round"], []).append(b)

        def round_time(items):
            return max((b.get("settled_at") or b.get("placed_at") or "") for b in items)

        rounds = sorted(by_round.items(), key=lambda kv: round_time(kv[1]), reverse=True)

        def round_label(r):
            comp, _, d = r.partition(":")
            return f"{_COMP_LABEL.get(comp, comp.upper())} — {d}"

        blocks = []
        for i, (r, items) in enumerate(rounds):
            won = sum(1 for b in items if b["status"] == "won")
            lost = sum(1 for b in items if b["status"] == "lost")
            void = sum(1 for b in items if b["status"] == "void")
            pnl = round(sum(float(b["pnl"] or 0) for b in items), 2)
            rec = f"{won}W-{lost}L" + (f"-{void}V" if void else "")
            rows = "".join(
                f"<tr><td>{b.get('league') or b['comp'].upper()}</td><td>{b['fixture']}</td>"
                f"<td>{b['leg_text']}{' <span class=bld>BUILDER</span>' if b['bet_type'] == 'builder' else ''}</td>"
                f"<td class='st-{b['status']}'>{b['status']}</td>"
                f"<td class='{'up' if (b['pnl'] or 0) >= 0 else 'down'}'>"
                f"{_fmt(float(b['pnl'])) if b['pnl'] is not None else '&ndash;'}</td></tr>"
                for b in sorted(items, key=lambda b: b["fixture"])
            )
            blocks.append(f"""
        <details class="hist-rd"{' open' if i == 0 else ''}>
          <summary><span>{round_label(r)}</span>
            <span class="{'up' if pnl >= 0 else 'down'}">{rec} &middot; {_fmt(pnl)}</span></summary>
          <table><thead><tr><th>League</th><th>Fixture</th><th>Bet</th><th>Status</th><th>P&amp;L</th></tr></thead>
          <tbody>{rows}</tbody></table>
        </details>""")

        return f"""
  <section class="card">
    <h2>History</h2>
    <p class="sub">Every settled round, most recently settled first.</p>
    {"".join(blocks)}
  </section>"""

    _MARKET_FILTER = {"goals": "Goals", "goals_u": "Goals", "corners": "Corners",
                       "cards": "Cards", "btts": "BTTS", "red": "Red card"}

    _rb_counter = {"n": 0}

    def bet_row(b):
        league = b.get("league") or b["comp"].upper()
        market = _MARKET_FILTER.get(b["market"], "")
        reason = b.get("reason")
        clickable = " bet-row" if reason else ""
        chev = '<span class="chev">&#8250;</span>' if reason else ""
        row = (
            f'<tr data-league="{league}" data-market="{market}" class="{clickable.strip()}"'
            + (f' data-target="rb{_rb_counter["n"]}" tabindex="0" role="button" aria-expanded="false"' if reason else "")
            + f'><td>{(b["placed_at"] or "")[:10]}</td><td>{league}</td>'
            f"<td>{b['fixture']}</td>"
            f"<td>{b['leg_text']}{' <span class=bld>BUILDER</span>' if b['bet_type'] == 'builder' else ''}</td>"
            f"<td>{float(b['price']):.2f}"
            f"{' <span class=rl>live</span>' if b.get('price_src') == 'real' else ''}</td>"
            f"<td class='st-{b['status']}'>{b['status']}</td>"
            f"<td class='{'up' if (b['pnl'] or 0) >= 0 else 'down'}'>"
            f"{_fmt(float(b['pnl'])) if b['pnl'] is not None else '&ndash;'}{chev}</td></tr>"
        )
        if not reason:
            return row
        rid = f'rb{_rb_counter["n"]}'
        _rb_counter["n"] += 1
        return row + f'<tr class="reason-row" id="{rid}" hidden><td colspan="7">{reason}</td></tr>'

    # Group by fixture so every bet on the same match sits together, rather
    # than interleaving with whatever else was placed around the same time.
    # Pending fixtures always sort above settled ones (so open bets are on
    # page 1), ordered by KICKOFF TIME (soonest first - whoever's playing
    # next sits at the top). Settled groups stay ordered by their most
    # recent bet (newest first), and bets within a group stay newest-first
    # too - so the very last page ends on the first bet ever placed.
    fixture_groups = {}
    for b in bets:
        fixture_groups.setdefault((b["comp"], b["fixture_id"]), []).append(b)
    group_order = sorted(
        fixture_groups, key=lambda k: max(b["placed_at"] or "" for b in fixture_groups[k]),
        reverse=True,
    )
    settled_keys = [k for k in group_order
                    if not any(b["status"] == "pending" for b in fixture_groups[k])]
    pending_keys = sorted(
        (k for k in group_order if any(b["status"] == "pending" for b in fixture_groups[k])),
        key=lambda k: fixture_groups[k][0]["kickoff"] or "",
    )
    recent_rows = [
        bet_row(b) for key in (pending_keys + settled_keys) for b in reversed(fixture_groups[key])
    ]
    recent = "".join(recent_rows) or "<tr><td colspan=7 class='muted'>No bets placed yet.</td></tr>"

    # League filter — lets a visitor pick just one league (e.g. Ligue 1) out
    # of "Rest of Football", or Premier League on its own, or leave it on
    # "All" to see everything together. Only offered for leagues that
    # actually have bets yet (no point showing an empty "FA Cup" filter).
    _LEAGUE_ORDER = ["Premier League", "Champions League", "La Liga", "Bundesliga",
                      "Serie A", "Ligue 1", "Championship", "FA Cup", "EFL Cup"]
    present = {b.get("league") or b["comp"].upper() for b in bets}
    ordered_leagues = [lg for lg in _LEAGUE_ORDER if lg in present]
    ordered_leagues += sorted(present - set(_LEAGUE_ORDER))
    filter_btns = ('<button type="button" class="lg-filter on" data-league="all">All</button>'
                   + "".join(f'<button type="button" class="lg-filter" data-league="{lg}">{lg}</button>'
                             for lg in ordered_leagues))

    # Market filter — Goals / Corners / Cards / BTTS / Red card, alongside
    # the league filter above. A builder bet spans more than one market so
    # it only shows under "All", not under any single market button.
    market_btns = ('<button type="button" class="mk-filter on" data-market="all">All</button>'
                   + "".join(f'<button type="button" class="mk-filter" data-market="{mk}">{mk}</button>'
                             for mk in ("Goals", "Corners", "Cards", "BTTS", "Red card")))

    # Pagination is entirely client-side (every row is already in the DOM;
    # JS just shows/hides them) so the page-size/filter choice needs no reload.
    rb_pager = "" if not recent_rows else f"""
    <div class="rb-filters">{filter_btns if len(ordered_leagues) > 1 else ""}</div>
    <div class="rb-filters">{market_btns}</div>
    <div class="rb-pager">
      <label class="rb-size">Rows per page
        <select id="rb-size" onchange="rbSetSize(this.value)">
          <option value="25" selected>25</option>
          <option value="50">50</option>
        </select>
      </label>
      <button type="button" class="rb-nav" id="rb-first" disabled onclick="rbFirst()">&#8676; First</button>
      <button type="button" class="rb-nav" id="rb-prev" disabled onclick="rbGo(-1)">&larr; Prev</button>
      <span id="rb-pos">Page 1 of 1</span>
      <button type="button" class="rb-nav" id="rb-next" onclick="rbGo(1)">Next &rarr;</button>
      <button type="button" class="rb-nav" id="rb-last" onclick="rbLast()">Last &#8677;</button>
    </div>
    <script>
    (function () {{
      var allRows = Array.prototype.slice.call(
        document.getElementById('rb-body').getElementsByTagName('tr'))
        .filter(function (r) {{ return !r.classList.contains('reason-row'); }});
      var size = 25, page = 1, leagueFilter = 'all', marketFilter = 'all';
      function visibleRows() {{
        return allRows.filter(function (r) {{
          return (leagueFilter === 'all' || r.dataset.league === leagueFilter)
            && (marketFilter === 'all' || r.dataset.market === marketFilter);
        }});
      }}
      function collapseReason(row) {{
        if (!row.dataset.target) return;
        var panel = document.getElementById(row.dataset.target);
        if (panel) {{ panel.hidden = true; row.setAttribute('aria-expanded', 'false'); }}
      }}
      function render() {{
        var vis = visibleRows();
        var t = Math.max(1, Math.ceil(vis.length / size));
        if (page > t) page = t;
        allRows.forEach(function (r) {{ r.hidden = true; collapseReason(r); }});
        vis.forEach(function (r, i) {{
          if (i >= (page - 1) * size && i < page * size) r.hidden = false;
        }});
        document.getElementById('rb-pos').textContent = 'Page ' + page + ' of ' + t
          + ' (' + vis.length + ' bet' + (vis.length === 1 ? '' : 's') + ')';
        document.getElementById('rb-first').disabled = page === 1;
        document.getElementById('rb-prev').disabled = page === 1;
        document.getElementById('rb-next').disabled = page === t;
        document.getElementById('rb-last').disabled = page === t;
      }}
      window.rbGo = function (d) {{
        var vis = visibleRows();
        var t = Math.max(1, Math.ceil(vis.length / size));
        var next = page + d;
        if (next < 1 || next > t) return;
        page = next; render();
      }};
      window.rbFirst = function () {{ page = 1; render(); }};
      window.rbLast = function () {{
        page = Math.max(1, Math.ceil(visibleRows().length / size)); render();
      }};
      window.rbSetSize = function (v) {{ size = parseInt(v, 10) || 25; page = 1; render(); }};
      document.querySelectorAll('.lg-filter').forEach(function (btn) {{
        btn.addEventListener('click', function () {{
          leagueFilter = btn.dataset.league;
          page = 1;
          document.querySelectorAll('.lg-filter').forEach(function (b) {{
            b.classList.toggle('on', b === btn);
          }});
          render();
        }});
      }});
      document.querySelectorAll('.mk-filter').forEach(function (btn) {{
        btn.addEventListener('click', function () {{
          marketFilter = btn.dataset.market;
          page = 1;
          document.querySelectorAll('.mk-filter').forEach(function (b) {{
            b.classList.toggle('on', b === btn);
          }});
          render();
        }});
      }});
      document.querySelectorAll('.bet-row').forEach(function (row) {{
        function toggleReason() {{
          var panel = document.getElementById(row.dataset.target);
          if (!panel) return;
          var open = !panel.hidden;
          panel.hidden = open;
          row.setAttribute('aria-expanded', String(!open));
        }}
        row.addEventListener('click', toggleReason);
        row.addEventListener('keydown', function (e) {{
          if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); toggleReason(); }}
        }});
      }});
      render();
    }})();
    </script>"""

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Account</title>
<style>
:root{{color-scheme:dark}}
*{{box-sizing:border-box}}
body{{margin:0;background:#161616;color:#eee;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}}
header{{padding:16px 20px;background:#111;border-bottom:1px solid #333}}
h1{{margin:0;font-size:18px}}h1 span{{color:#ffb80c}}
main{{max-width:1040px;margin:0 auto;padding:20px;display:grid;gap:16px}}
.combined{{background:#1f1f1f;border:1px solid #333;border-radius:10px;padding:14px 16px}}
.cards{{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}}
.card{{background:#242424;border:1px solid #333;border-radius:10px;padding:16px}}
h2{{margin:0 0 4px;font-size:14px;color:#9a9a9a;text-transform:uppercase;letter-spacing:.5px}}
.big{{margin:2px 0;font-size:26px;font-weight:700}}
.sub{{margin:0 0 10px;color:#9a9a9a;font-size:12px}}
.bal-row{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:4px}}
.bal-col .big{{font-size:19px}}
.bal-lbl{{display:block;color:#777;font-size:10px;text-transform:uppercase;letter-spacing:.4px}}
.up{{color:#3ecf8e}}.down{{color:#ff6b6b}}.muted{{color:#777}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th,td{{text-align:left;padding:5px 6px;border-bottom:1px solid #2e2e2e}}
th{{color:#888;font-weight:600}}
.bld{{background:#ffb80c;color:#000;border-radius:3px;padding:0 4px;font-size:9px;font-weight:700}}
.rl{{color:#3ecf8e;font-size:9px;border:1px solid #3ecf8e;border-radius:3px;padding:0 3px}}
.st-won{{color:#3ecf8e}}.st-lost{{color:#ff6b6b}}.st-void{{color:#888}}.st-pending{{color:#ffb80c}}
.note{{color:#777;font-size:11px}}
a{{color:#ffb80c}}
.bars{{display:flex;flex-direction:column;gap:10px;margin-top:8px}}
.bar-row{{display:grid;grid-template-columns:62px 1fr 38px;align-items:center;gap:10px;font-size:11px}}
.bar-lbl{{color:#9a9a9a;letter-spacing:.3px;font-weight:600;white-space:nowrap;font-size:9.5px}}
.bar-track{{height:8px;background:#2e2e2e;border-radius:4px;overflow:hidden}}
.bar-fill{{display:block;height:100%;border-radius:4px}}
.bar-fill.goals{{background:#3ecf8e}}
.bar-fill.corners{{background:#4a9fd8}}
.bar-fill.cards{{background:#ffb80c}}
.bar-fill.btts{{background:#ab47bc}}
.bar-fill.red{{background:#ff6b6b}}
.bar-pct{{text-align:right;font-weight:700}}
.rb-filters{{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}}
.lg-filter{{background:#2e2e2e;color:#bbb;border:1px solid #444;border-radius:999px;padding:5px 12px;cursor:pointer;font-size:11.5px}}
.lg-filter:hover{{background:#3a3a3a}}
.lg-filter.on{{background:#ffb80c;color:#000;border-color:#ffb80c;font-weight:700}}
.mk-filter{{background:#2e2e2e;color:#bbb;border:1px solid #444;border-radius:999px;padding:5px 12px;cursor:pointer;font-size:11.5px}}
.mk-filter:hover{{background:#3a3a3a}}
.mk-filter.on{{background:#4a9fd8;color:#000;border-color:#4a9fd8;font-weight:700}}
.hist-rd{{border:1px solid #333;border-radius:8px;margin-top:8px;padding:0 12px}}
.hist-rd summary{{display:flex;justify-content:space-between;align-items:center;gap:12px;
  padding:10px 0;cursor:pointer;font-size:13px;list-style:none}}
.hist-rd summary::-webkit-details-marker{{display:none}}
.hist-rd summary span:first-child{{font-weight:600}}
.hist-rd table{{margin:0 0 10px}}
.bet-row{{cursor:pointer}}
.bet-row:hover{{background:#2a2a2a}}
.chev{{color:#e0a72c;display:inline-block;margin-left:6px;transition:transform .15s ease}}
.bet-row[aria-expanded="true"] .chev{{transform:rotate(90deg)}}
.reason-row td{{border-bottom:1px solid #2e2e2e;padding:0}}
.key-stats{{background:#211a10;border-left:3px solid #e0a72c;margin:8px 6px 12px;padding:12px 14px}}
.ks-tag{{display:inline-block;font-family:"IBM Plex Mono",monospace;font-size:9.5px;font-weight:700;
  letter-spacing:.08em;color:#e0a72c;background:#2c2113;border:1px solid #4a3a1c;border-radius:3px;
  padding:2px 6px;margin-bottom:8px}}
.key-stats ul{{margin:6px 0 0;padding-left:18px;color:#d8cdb8}}
.key-stats li{{margin:6px 0;font-size:12.5px;line-height:1.55}}
.key-stats li::marker{{color:#e0a72c}}
.rb-pager{{display:flex;align-items:center;justify-content:center;gap:14px;margin-top:8px;font-size:12px;flex-wrap:wrap}}
.rb-size{{display:flex;align-items:center;gap:6px;color:#9a9a9a}}
.rb-size select{{background:#2e2e2e;color:#eee;border:1px solid #444;border-radius:6px;padding:4px 7px;font-size:12px}}
.rb-nav{{background:#2e2e2e;color:#eee;border:1px solid #444;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12px}}
.rb-nav:hover:not(:disabled){{background:#3a3a3a}}
.rb-nav:disabled{{opacity:.4;cursor:default}}
#rb-pos{{color:#9a9a9a}}
</style></head><body>
<header><h1><span>Paper Account</span></h1></header>
<main>
  {"<p class='down'>Could not load: " + err + "</p>" if err else ""}
  <div class="combined">Combined P&amp;L across all books:
    <b class="{'up' if combined_pnl >= 0 else 'down'}">{_fmt(combined_pnl)}</b>
    &nbsp;·&nbsp; started £{START_BANKROLL:,.0f} each &nbsp;·&nbsp;
    built {uk_now()}</div>
  <div class="cards">
    {card("Premier League", "pl")}
    {card("Champions League", "ucl")}
    {card("Rest of Football", "rest")}
  </div>
  <section class="card">
    <h2>Hit rate &mdash; singles, all books</h2>
    <p class="sub">{bars_total} settled single bets scored, bucketed by market.</p>
    <div class="bars">{bars}</div>
  </section>
  <section class="card">
    <h2>Goals model &mdash; singles only</h2>
    <p class="sub">Over/Under total goals + BTTS, stripped of corners, cards and builders.</p>
    <table><thead><tr><th></th><th>P&amp;L</th><th>ROI</th><th>Record</th><th>Open</th></tr></thead>
    <tbody>
      {goals_row("Premier League", goals["pl"])}
      {goals_row("Champions League", goals["ucl"])}
      {goals_row("Rest of Football", goals["rest"])}
      {goals_row("<b>Combined</b>", goals_all)}
    </tbody></table>
  </section>
  {history_html()}
  <section class="card">
    <h2>Recent bets</h2>
    <table><thead><tr><th>Placed</th><th>League</th><th>Fixture</th><th>Bet</th>
    <th>Price</th><th>Status</th><th>P&amp;L</th></tr></thead>
    <tbody id="rb-body">{recent}</tbody></table>
    {rb_pager}
  </section>
  <p class="note">Model forward test — the legs <code>suggest()</code> would put on,
  £{STAKES['pl']:.0f} flat per single plus one £{STAKES['pl']:.0f} combined
  builder on the top-rated fixture each round.
  Over-2.5-goals and BTTS legs are priced from real bookmaker odds at placement
  (marked <span class="rl">live</span>) for Premier League and Champions League;
  everything else, and all of Rest of Football, comes from a fixed table.
  Not real money.</p>
</main></body></html>"""
