"""
PL Bet Builder — weekend averages dashboard.

Pulls this weekend's Premier League fixtures + team stats from ESPN's public API,
blends last season (full) with the current season so far, estimates expected
goals / corners / cards per fixture, and writes a self-contained dashboard.html.

Usage:
    python build.py                 # auto: the upcoming Fri-Mon
    python build.py 20260829 20260901   # explicit date range (YYYYMMDD)
"""

import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sponsors import SPONSORS

HERE = Path(__file__).parent
UA = {"User-Agent": "curl/8.4.0", "Accept": "application/json"}

# 2025/26 corners taken per game, by ESPN team id (StatMuse). ESPN's API has no
# corner data. corners.json overrides this if present (edit it once a season).
_CORNERS = {
    "359": 5.68, "382": 6.42, "364": 6.11, "361": 6.05, "363": 5.95, "349": 5.58,
    "367": 5.50, "362": 5.24, "393": 5.21, "370": 4.92, "331": 4.87, "337": 4.79,
    "360": 4.76, "368": 4.42, "357": 4.42, "384": 4.18, "366": 3.63,
    "388": 4.30, "306": 4.10, "373": 3.90,
}
try:
    _CORNERS.update({k: v for k, v in json.loads((HERE / "corners.json").read_text()).items()
                     if not k.startswith("_")})
except OSError:
    pass
CORNERS = _CORNERS

CORE = "https://sports.core.api.espn.com/v2/sports/soccer/leagues/eng.1"
SITE = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1"

# Supabase — shared "football-fan-hub" project. Publishable key, safe in the page.
SUPA_URL = "https://hsccirgnwfwxccwjmgjf.supabase.co"
SUPA_KEY = "sb_publishable_TijbYc997ZRx6yGvP4qHOw_O34Qmrqn"

# SSB token (Solana mainnet) — leaderboard points are shown/paid as SSB.
SSB_MINT = "EFhEV39xNWtudvMszXHkk6aZVQzyXZPwefEDxpoXcSK2"
SSB_TICKER = "SSB"
SSB_PER_POINT = 100
SSB_PAYOUT_WALLET = "Dm3n3syehNvzQhM6CE85euYggFEdgvNQjmZoCQ6K8sir"
SSB_WEEKLY_CAP = 200_000   # keep in sync with api/payout.py PAYOUT_WEEKLY_CAP
# sha256("ssb-payout-lewis-7k2x9") — the payout sheet unlocks with that key.
ADMIN_HASH = "6e44a88b2d6e60b678a76d5ca9669439af90ba0140b5223c115a6803c94c789a"


def pick_line(metric, val):
    """The O/U line for the leaderboard game = the line the model suggests for that
    fixture (mirrors the thresholds in suggest()). Same for everyone on a game."""
    if metric == "goals":
        return 2.5 if val >= 3.15 else (1.5 if val >= 2.55 else 3.5)
    if metric == "corners":
        return 10.5 if val >= 11 else (9.5 if val >= 9.7 else 8.5)
    if metric == "cards":
        return 4.5 if val >= 4.4 else (3.5 if val >= 3.6 else 2.5)
    return 0


def pick_side(metric, val):
    """Which way the model leans at its own line: 'over' or 'under'."""
    return "over" if val > pick_line(metric, val) else "under"


def pick_landed(metric, model_val, actual):
    """Did the model's suggested Over/Under pick land? (lines are .5, no push)."""
    line = pick_line(metric, model_val)
    return actual > line if model_val > line else actual < line


def result_pick(h_goals, a_goals, margin=0.35):
    """Model's 1X2 lean: 'home' / 'draw' / 'away', from expected goals for each side."""
    diff = h_goals - a_goals
    if diff > margin:
        return "home"
    if diff < -margin:
        return "away"
    return "draw"


def _actual_result(h_score, a_score):
    diff = h_score - a_score
    return "home" if diff > 0 else "away" if diff < 0 else "draw"


def metric_landed(key, model, actual):
    """Did the model's pick for this category land, for any of the 5 scored
    categories (goals/corners/cards use an O/U line, btts/result don't)."""
    if key in ("goals", "corners", "cards"):
        return pick_landed(key, model[key], actual[key])
    if key == "btts":
        return model["btts"] == actual["btts"]
    if key == "result":
        return model["result"] == _actual_result(actual["h_goals"], actual["a_goals"])
    return False


SCORED_METRICS = ("goals", "corners", "cards", "btts", "result")

LAST_SEASON = 2025      # 2025-26 (completed)
THIS_SEASON = 2026      # 2026-27 (in progress)

# League baselines (2025/26) for the simple strength model
LEAGUE_AVG_GF = 1.42        # goals per team per game
LEAGUE_AVG_CARDS = 1.85     # yellows per team per game

PROMOTED = {"388", "306", "373"}   # Coventry, Hull, Ipswich — thin/no PL history


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def flat_stats(payload):
    out = {}
    for cat in payload.get("splits", {}).get("categories", []):
        for s in cat.get("stats", []):
            out[s["name"]] = s.get("value", 0.0) or 0.0
    return out


def _match_totals(comp, competitors, h_score, a_score):
    """goals / corners / cards for a finished match, from a scoreboard event."""
    details = comp.get("details", [])
    yc = sum(1 for x in details if x.get("yellowCard"))
    rc = sum(1 for x in details if x.get("redCard"))
    corners, has_corner = 0, False
    for c in competitors:
        for s in c.get("statistics", []):
            if s["name"] == "wonCorners":
                has_corner = True
                try:
                    corners += int(float(s["displayValue"]))
                except (TypeError, ValueError):
                    pass
    return {
        "goals": h_score + a_score, "h_goals": h_score, "a_goals": a_score,
        "corners": corners, "cards": yc + rc, "reds": rc,
        "btts": h_score > 0 and a_score > 0,
        "has_stats": has_corner or bool(details),
    }


def team_season(team_id, season):
    try:
        raw = get(f"{CORE}/seasons/{season}/types/1/teams/{team_id}/statistics")
    except Exception:
        return None
    s = flat_stats(raw)
    gp = s.get("appearances", 0)
    if not gp:
        return None
    return {
        "gp": gp,
        "gf_pg": s.get("totalGoals", 0) / gp,
        "ga_pg": s.get("goalsConceded", 0) / gp,
        "yc_pg": s.get("yellowCards", 0) / gp,
        "rc": s.get("redCards", 0),
        "fouls_pg": s.get("foulsCommitted", 0) / gp,
    }


def blended(team_id):
    """Blend last season (base) with this season (weight grows to 0.5 by GW10)."""
    last = team_season(team_id, LAST_SEASON)
    cur = team_season(team_id, THIS_SEASON)
    corners = CORNERS.get(str(team_id), 4.8)

    if last is None:                       # promoted / no history
        base = {
            "gf_pg": 1.05, "ga_pg": 1.70, "yc_pg": 2.05, "rc": 2, "fouls_pg": 11.0,
        }
    else:
        base = last

    if cur and cur["gp"] >= 3:
        w = min(cur["gp"] / 10.0, 0.5)
        merged = {
            k: base[k] * (1 - w) + cur[k] * w
            for k in ("gf_pg", "ga_pg", "yc_pg", "fouls_pg")
        }
        merged["rc"] = base["rc"]
        merged["cur_gp"] = cur["gp"]
    else:
        merged = {k: base[k] for k in ("gf_pg", "ga_pg", "yc_pg", "fouls_pg")}
        merged["rc"] = base["rc"]
        merged["cur_gp"] = cur["gp"] if cur else 0

    merged["corners_pg"] = corners
    merged["promoted"] = str(team_id) in PROMOTED or last is None
    return merged


def weekend_windows(today=None):
    """(prev_weekend, current_weekend) as (YYYYMMDD, YYYYMMDD) Fri-Mon pairs.

    The current weekend is held through Sun/Mon and only rolls forward on Tuesday,
    so the coupon still shows the games in progress over the weekend itself.
    """
    today = today or date.today()
    since_fri = (today.weekday() - 4) % 7     # Fri0 Sat1 Sun2 Mon3 Tue4 Wed5 Thu6
    last_fri = today - timedelta(days=since_fri)
    cur_fri = last_fri + timedelta(days=7) if since_fri >= 4 else last_fri
    prev_fri = cur_fri - timedelta(days=7)

    def win(fri):
        return fri.strftime("%Y%m%d"), (fri + timedelta(days=3)).strftime("%Y%m%d")

    return win(prev_fri), win(cur_fri)


def upcoming_weekend():
    return weekend_windows()[1]


def fixtures(d1, d2):
    data = get(f"{SITE}/scoreboard?dates={d1}-{d2}")
    out = []
    for ev in data.get("events", []):
        comp = ev["competitions"][0]
        cs = comp["competitors"]
        home = next(c for c in cs if c["homeAway"] == "home")
        away = next(c for c in cs if c["homeAway"] == "away")
        st = comp["status"]["type"]
        completed = bool(st.get("completed"))

        def score(c):
            try:
                return int(c.get("score"))
            except (TypeError, ValueError):
                return None

        row = {
            "id": ev["id"],
            "date": ev["date"],
            "home_id": home["team"]["id"],
            "away_id": away["team"]["id"],
            "home": home["team"]["displayName"],
            "away": away["team"]["displayName"],
            "home_abbr": home["team"].get("shortDisplayName", home["team"]["abbreviation"]),
            "away_abbr": away["team"].get("shortDisplayName", away["team"]["abbreviation"]),
            "home_score": score(home),
            "away_score": score(away),
            "completed": completed,
            "result": None,
        }

        if completed and row["home_score"] is not None:
            row["result"] = _match_totals(comp, cs, row["home_score"], row["away_score"])
        out.append(row)
    out.sort(key=lambda x: x["date"])
    return out


def weekend_fixtures(win, roll_ahead=3):
    """Fixtures for a weekend window; roll forward over empty (int'l break) weekends."""
    d1, d2 = win
    for _ in range(roll_ahead + 1):
        fx = fixtures(d1, d2)
        if fx:
            return (d1, d2), fx
        d1 = (datetime.strptime(d1, "%Y%m%d") + timedelta(days=7)).strftime("%Y%m%d")
        d2 = (datetime.strptime(d2, "%Y%m%d") + timedelta(days=7)).strftime("%Y%m%d")
    return (win[0], win[1]), []


def expected(h, a):
    # Goals: attack vs defence relative to league average, + small home tilt
    h_goals = (h["gf_pg"] / LEAGUE_AVG_GF) * (a["ga_pg"] / LEAGUE_AVG_GF) * LEAGUE_AVG_GF * 1.10
    a_goals = (a["gf_pg"] / LEAGUE_AVG_GF) * (h["ga_pg"] / LEAGUE_AVG_GF) * LEAGUE_AVG_GF * 0.92
    goals = h_goals + a_goals

    corners = h["corners_pg"] + a["corners_pg"]

    cards = h["yc_pg"] + a["yc_pg"]
    red_risk = (h["rc"] + a["rc"]) >= 6

    return {
        "goals": round(goals, 2),
        "h_goals": round(h_goals, 2),
        "a_goals": round(a_goals, 2),
        "corners": round(corners, 1),
        "cards": round(cards, 2),
        "red_risk": red_risk,
        "btts": h_goals >= 0.9 and a_goals >= 0.9,
        "result": result_pick(h_goals, a_goals),
    }


def suggest(x):
    """Structured legs: {cat, text, check:(metric, line)}. check grades a result."""
    legs = []
    g = x["goals"]
    if g >= 3.15:
        legs.append(_leg("goals", "Over 2.5 goals", ("goals", 2.5)))
    elif g >= 2.55:
        legs.append(_leg("goals", "Over 1.5 goals", ("goals", 1.5)))
    else:
        legs.append(_leg("goals", "Under 3.5 goals", ("goals_u", 3.5)))
    if x["btts"] and g >= 2.7:
        legs.append(_leg("goals", "Both teams to score", ("btts", 0)))

    c = x["corners"]
    cl = 10.5 if c >= 11 else 9.5 if c >= 9.7 else 8.5
    legs.append(_leg("corners", f"Over {cl:g} corners", ("corners", cl)))

    k = x["cards"]
    kl = 4.5 if k >= 4.4 else 3.5 if k >= 3.6 else 2.5 if k >= 2.9 else None
    if kl:
        legs.append(_leg("cards", f"Over {kl:g} cards", ("cards", kl)))
    if x["red_risk"]:
        legs.append(_leg("cards", "A red card", ("red", 1)))
    return legs


def _leg(cat, text, check):
    return {"cat": cat, "text": text, "check": check}


def grade(check, a):
    """Would this leg have landed? a = actual totals for a finished match."""
    metric, line = check
    return {
        "goals": a["goals"] > line,
        "goals_u": a["goals"] < line,
        "corners": a["corners"] > line,
        "cards": a["cards"] > line,
        "red": a["reds"] >= 1,
        "btts": a["btts"],
    }.get(metric, False)


def match_result(ev):
    """Actual goals/corners/cards for a finished fixture (read from the scoreboard)."""
    return ev.get("result")


def rating(x):
    """0-100 'juice' score for ranking fixtures by how bettable the overs look."""
    gs = max(0, min(1, (x["goals"] - 2.2) / 1.6))
    cs = max(0, min(1, (x["corners"] - 8.5) / 3.5))
    ks = max(0, min(1, (x["cards"] - 2.6) / 2.4))
    return round(100 * (0.4 * gs + 0.25 * cs + 0.35 * ks))


def _uk(dt_utc):
    """UTC datetime -> actual UK local time (BST Mar-Oct, GMT otherwise)."""
    y = dt_utc.year

    def last_sunday(month):
        d = date(y, month, 31)
        return d.day - (d.weekday() + 1) % 7

    bst_start = datetime(y, 3, last_sunday(3), 1, tzinfo=timezone.utc)
    bst_end = datetime(y, 10, last_sunday(10), 1, tzinfo=timezone.utc)
    offset = 1 if bst_start <= dt_utc < bst_end else 0
    return dt_utc + timedelta(hours=offset)


def uk_now():
    return _uk(datetime.now(timezone.utc)).strftime("%a %d %b %Y, %H:%M")


def team_blends():
    """Blend every current PL team once, so nothing else has to re-fetch."""
    ids = [t["team"]["id"] for t in
           get(f"{SITE}/teams")["sports"][0]["leagues"][0]["teams"]]
    with ThreadPoolExecutor(max_workers=12) as ex:
        return dict(zip(ids, ex.map(blended, ids)))


# --------------------------------------------------------------------------- #
#  Champions League — midweek add-on band
#  Rougher than the PL sheet: each club's DOMESTIC-league season stats, one
#  Europe-wide scoring average, corners from a small current-season sample.
# --------------------------------------------------------------------------- #

UCL_SITE = "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions"
CORE_SOCCER = "https://sports.core.api.espn.com/v2/sports/soccer/leagues"
UCL_POINTS_START = date(2026, 9, 8)   # UCL picks count from this date on
_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()

# Domestic leagues a UCL club is likely to come from.
_DOMESTIC_LEAGUES = [
    "eng.1", "esp.1", "ger.1", "ita.1", "fra.1", "por.1", "ned.1", "bel.1",
    "tur.1", "aut.1", "gre.1", "sco.1", "cze.1", "sui.1", "den.1", "nor.1",
    "ukr.1", "srb.1", "cro.1",
]

_UCL_CAVEATS = [
    ("Home / away goal splits can look skewed",
     "weaker-league defences read as elite, so the away side's goals get squeezed "
     "&mdash; trust the match <b>total</b>, not the split."),
    ("Corners are a rough guess",
     "there is no prior-season corner data, so they come from a small current-season "
     "sample and swing hard."),
    ("One scoring average across every league",
     "and UCL referees card more than domestic games, so the card picks lean a "
     "little high."),
]


def _league_team_ids(league):
    try:
        raw = get(f"{CORE_SOCCER}/{league}/seasons/{THIS_SEASON}/teams?limit=50")
    except Exception:
        return league, []
    ids = []
    for item in raw.get("items", []):
        m = re.search(r"/teams/(\d+)", item.get("$ref", ""))
        if m:
            ids.append(m.group(1))
    return league, ids


# 2026/27 UCL clubs -> domestic league. Static so the site isn't fetching 19
# league rosters on every build; _ucl_probe_league() fills any genuine gaps.
_UCL_LEAGUE_MAP = {
    "102": "esp.1", "104": "ita.1", "1068": "esp.1", "110": "ita.1", "114": "ita.1",
    "11420": "ger.1", "124": "ger.1", "132": "ger.1", "134": "ger.1", "142": "ned.1",
    "148": "ned.1", "160": "fra.1", "166": "fra.1", "175": "fra.1", "2250": "por.1",
    "244": "esp.1", "2572": "ita.1", "2980": "nor.1", "359": "eng.1", "360": "eng.1",
    "362": "eng.1", "364": "eng.1", "382": "eng.1", "432": "tur.1", "436": "tur.1",
    "437": "por.1", "4411": "aut.1", "510": "nor.1", "570": "bel.1", "83": "esp.1",
    "86": "esp.1", "887": "gre.1",
    "493": "ukr.1", "494": "cze.1", "521": "svk.1", "21922": "aze.1",
}
_ucl_probe_cache = {}


def _ucl_probe_league(team_id):
    if team_id in _ucl_probe_cache:
        return _ucl_probe_cache[team_id]
    found = None
    for lg in _DOMESTIC_LEAGUES:
        if _dom_season(team_id, lg, THIS_SEASON) or _dom_season(team_id, lg, LAST_SEASON):
            found = lg
            break
    _ucl_probe_cache[team_id] = found
    return found


def ucl_league_index(team_ids=None):
    """{team_id: domestic_league_slug}. Static map first, probe for unknowns."""
    idx = dict(_UCL_LEAGUE_MAP)
    for tid in team_ids or []:
        if tid not in idx:
            idx[tid] = _ucl_probe_league(tid)
    return idx


def _dom_season(team_id, league, season):
    try:
        raw = get(f"{CORE_SOCCER}/{league}/seasons/{season}/types/1/teams/{team_id}/statistics")
    except Exception:
        return None
    s = flat_stats(raw)
    gp = s.get("appearances", 0)
    if not gp:
        return None
    return {
        "gp": gp,
        "gf_pg": s.get("totalGoals", 0) / gp,
        "ga_pg": s.get("goalsConceded", 0) / gp,
        "yc_pg": s.get("yellowCards", 0) / gp,
        "rc": s.get("redCards", 0),
        "cnr_pg": s.get("wonCorners", 0) / gp,   # only tracked for the current season
    }


def ucl_blend(args):
    team_id, league = args
    if not league:
        return None
    last = _dom_season(team_id, league, LAST_SEASON)
    cur = _dom_season(team_id, league, THIS_SEASON)
    if not last and not cur:
        return None
    base = last or {"gf_pg": 1.25, "ga_pg": 1.35, "yc_pg": 2.0, "rc": 4, "cnr_pg": 5.0}
    if cur and cur["gp"] >= 2:
        w = min(cur["gp"] / 16.0, 0.35)      # tiny early samples -> lean on last season
        m = {k: base[k] * (1 - w) + cur[k] * w for k in ("gf_pg", "ga_pg", "yc_pg")}
    else:
        m = {k: base[k] for k in ("gf_pg", "ga_pg", "yc_pg")}
    m["rc"] = base["rc"]
    m["corners_pg"] = cur["cnr_pg"] if (cur and cur["gp"] >= 2 and cur["cnr_pg"] > 0) else 5.0
    m["promoted"] = False
    return m


def _ucl_scoreboard_events():
    """Every UCL event from the points-start date to a week ahead (windowed —
    one wide call caps at 100 events)."""
    end = date.today() + timedelta(days=8)
    windows, d = [], UCL_POINTS_START
    while d <= end:
        windows.append((d.strftime("%Y%m%d"), (d + timedelta(days=6)).strftime("%Y%m%d")))
        d += timedelta(days=7)
    seen, out = set(), []
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = ex.map(
            lambda w: get(f"{UCL_SITE}/scoreboard?dates={w[0]}-{w[1]}").get("events", []),
            windows,
        )
        for evs in results:
            for ev in evs:
                if ev["id"] not in seen:
                    seen.add(ev["id"])
                    out.append(ev)
    return out


_UCL_CACHE_TTL = 40 * 60   # seconds — how long a cached UCL snapshot is trusted


def _ucl_cache_get():
    """{'upcoming': {...row...}, 'results': {...row...}} from coupon_ucl_cache."""
    url = f"{SUPA_URL}/rest/v1/coupon_ucl_cache?select=key,payload,refreshed_at"
    req = urllib.request.Request(url, headers={
        "apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return {row["key"]: row for row in json.load(r)}
    except Exception:
        return {}


def _ucl_cache_put(key, payload):
    if not _SERVICE_KEY:
        return
    body = json.dumps([{
        "key": key, "payload": payload,
        "refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }]).encode("utf-8")
    req = urllib.request.Request(
        f"{SUPA_URL}/rest/v1/coupon_ucl_cache?on_conflict=key",
        data=body, method="POST",
        headers={"apikey": _SERVICE_KEY, "Authorization": f"Bearer {_SERVICE_KEY}",
                 "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates"},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def _ucl_rehydrate(u):
    fx = {"id": u["id"], "date": u["date"], "home": u["home"], "away": u["away"],
          "home_abbr": u["home"], "away_abbr": u["away"]}
    return {"fx": fx, "x": u["x"], "score": u["score"]}


def ucl_context():
    """(upcoming_rows, finished_map). Reads a Supabase snapshot; only one request
    per ~40 min pays the full ESPN + model recompute."""
    cache = _ucl_cache_get()
    up_row = cache.get("upcoming")
    fin = (cache.get("results") or {}).get("payload") or {}
    fresh = False
    if up_row:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(up_row["refreshed_at"])).total_seconds()
            fresh = age < _UCL_CACHE_TTL
        except Exception:
            fresh = False
    if fresh:
        return [_ucl_rehydrate(u) for u in (up_row["payload"] or [])], fin

    try:
        upcoming, new_fin = _ucl_compute(fin)
    except Exception:
        stale = [_ucl_rehydrate(u) for u in (up_row["payload"] or [])] if up_row else []
        return stale, fin

    _ucl_cache_put("upcoming", [
        {"id": u["fx"]["id"], "date": u["fx"]["date"], "home": u["fx"]["home"],
         "away": u["fx"]["away"], "x": u["x"], "score": u["score"]}
        for u in upcoming
    ])
    merged = {**fin, **new_fin}
    if new_fin or not cache.get("results"):
        _ucl_cache_put("results", merged)
    return upcoming, merged


def _ucl_compute(already_graded):
    """The expensive path: ESPN scoreboard + domestic-league model.
    Returns (upcoming_rows, newly_graded_finished_map)."""
    events = _ucl_scoreboard_events()
    cached = already_graded
    parsed, need = [], set()
    for ev in events:
        comp = ev["competitions"][0]
        cs = comp["competitors"]
        h = next(c for c in cs if c["homeAway"] == "home")
        a = next(c for c in cs if c["homeAway"] == "away")

        def _sc(c):
            try:
                return int(c.get("score"))
            except (TypeError, ValueError):
                return None

        done = bool(comp["status"]["type"].get("completed"))
        row = {
            "id": ev["id"], "date": ev["date"], "comp": comp, "cs": cs,
            "home_id": h["team"]["id"], "away_id": a["team"]["id"],
            "home": h["team"]["displayName"], "away": a["team"]["displayName"],
            "done": done, "h_score": _sc(h), "a_score": _sc(a),
        }
        parsed.append(row)
        if (done and row["id"] not in cached) or not done:
            need.add(row["home_id"])
            need.add(row["away_id"])

    blends = {}
    if need:
        idx = ucl_league_index(need)
        with ThreadPoolExecutor(max_workers=12) as ex:
            blends = dict(zip(need, ex.map(
                ucl_blend, [(tid, idx.get(tid)) for tid in need])))

    upcoming, new_fin = [], {}
    for row in parsed:
        if row["done"]:
            if row["id"] in cached or row["h_score"] is None:
                continue
            h, a = blends.get(row["home_id"]), blends.get(row["away_id"])
            if not h or not a:
                continue
            x = expected(h, a)
            tot = _match_totals(row["comp"], row["cs"], row["h_score"], row["a_score"])
            new_fin[row["id"]] = {
                **{k: tot[k] for k in ("goals", "corners", "cards",
                                       "h_goals", "a_goals", "btts")},
                "kickoff": row["date"],
                "lines": {k: pick_line(k, x[k]) for k in ("goals", "corners", "cards")},
            }
        else:
            h, a = blends.get(row["home_id"]), blends.get(row["away_id"])
            if not h or not a:
                continue
            x = expected(h, a)
            fx = {"id": row["id"], "date": row["date"],
                  "home": row["home"], "away": row["away"],
                  "home_abbr": row["home"], "away_abbr": row["away"]}
            upcoming.append({"fx": fx, "x": x, "score": rating(x)})

    upcoming.sort(key=lambda r: r["score"], reverse=True)
    return upcoming, new_fin


# --------------------------------------------------------------------------- #
#  "Rest of football" - paper-account-only book (not on the public picks/
#  leaderboard game, just the /paper forward test). La Liga, Bundesliga,
#  Serie A, Ligue 1, the Championship, and the two English domestic cups.
# --------------------------------------------------------------------------- #
REST_LEAGUES = ["esp.1", "ger.1", "ita.1", "fra.1", "eng.2"]
REST_CUPS = ["eng.fa", "eng.league_cup"]
_ENGLISH_TIERS = ["eng.1", "eng.2", "eng.3", "eng.4"]
_rest_tier_cache = {}


def _rest_english_tier(team_id):
    """Which English tier (PL/Championship/League One/League Two) a cup team's
    domestic form should be read from - a cup fixture pits clubs from
    different tiers against each other, so (unlike the 5 leagues above) the
    competition slug itself isn't a valid stats source (see ucl_blend note)."""
    if team_id in _rest_tier_cache:
        return _rest_tier_cache[team_id]
    found = None
    for lg in _ENGLISH_TIERS:
        if _dom_season(team_id, lg, THIS_SEASON) or _dom_season(team_id, lg, LAST_SEASON):
            found = lg
            break
    _rest_tier_cache[team_id] = found
    return found


def _rest_scoreboard_events(league, days_ahead=9):
    site = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}"
    d1 = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    d2 = (date.today() + timedelta(days=days_ahead)).strftime("%Y%m%d")
    try:
        return league, get(f"{site}/scoreboard?dates={d1}-{d2}").get("events", [])
    except Exception:
        return league, []


def _rest_parse_events(events):
    """[{id, date, home, away, home_id, away_id, completed, h_score, a_score,
    comp, cs}] - the bits both _rest_upcoming and _rest_actuals need."""
    out = []
    for ev in events:
        comp = ev["competitions"][0]
        cs = comp["competitors"]
        h = next((c for c in cs if c["homeAway"] == "home"), None)
        a = next((c for c in cs if c["homeAway"] == "away"), None)
        if not h or not a:
            continue

        def _sc(c):
            try:
                return int(c.get("score"))
            except (TypeError, ValueError):
                return None

        out.append({
            "id": ev["id"], "date": ev["date"], "comp": comp, "cs": cs,
            "home": h["team"].get("shortDisplayName") or h["team"]["displayName"],
            "away": a["team"].get("shortDisplayName") or a["team"]["displayName"],
            "home_id": h["team"]["id"], "away_id": a["team"]["id"],
            "completed": bool(comp["status"]["type"].get("completed")),
            "h_score": _sc(h), "a_score": _sc(a),
        })
    return out


def _rest_blend_for(team_id, league, is_cup):
    return ucl_blend((team_id, _rest_english_tier(team_id) if is_cup else league))


def _rest_compute():
    """The expensive path (~20s, 7 leagues x scoreboard + per-team stats):
    one pass over every rest-of-football competition producing BOTH the
    upcoming rows and the finished-fixture totals, exactly like
    _ucl_compute() does for UCL - only ever called on a cache miss."""
    leagues = REST_LEAGUES + REST_CUPS
    with ThreadPoolExecutor(max_workers=8) as ex:
        pairs = list(ex.map(_rest_scoreboard_events, leagues))

    upcoming, finished = [], {}
    for league, raw_events in pairs:
        events = _rest_parse_events(raw_events)
        is_cup = league in REST_CUPS
        team_ids = {tid for e in events for tid in (e["home_id"], e["away_id"])}
        if not team_ids:
            continue
        with ThreadPoolExecutor(max_workers=8) as ex:
            blends = dict(zip(
                team_ids,
                ex.map(lambda tid: _rest_blend_for(tid, league, is_cup), team_ids),
            ))
        for e in events:
            hb, ab = blends.get(e["home_id"]), blends.get(e["away_id"])
            if not hb or not ab:
                continue
            x = expected(hb, ab)
            if e["completed"] and e["h_score"] is not None and e["a_score"] is not None:
                # Full _match_totals() output (has_stats/reds included) -
                # this feeds paper.py's settle(), NOT the public display, so
                # it must match _ucl_actuals()'s shape, not _finished_map()'s
                # trimmed one.
                tot = _match_totals(e["comp"], e["cs"], e["h_score"], e["a_score"])
                finished[e["id"]] = {**tot, "kickoff": e["date"]}
            elif not e["completed"]:
                fx = {"id": e["id"], "date": e["date"], "home": e["home"], "away": e["away"],
                      "home_abbr": e["home"], "away_abbr": e["away"], "league": league}
                upcoming.append({"fx": fx, "x": x, "score": rating(x)})

    upcoming.sort(key=lambda r: r["fx"]["date"])
    return upcoming, finished


_REST_CACHE_TTL = 20 * 60   # seconds - shorter than UCL's since kickoffs cluster tighter


def rest_context():
    """(upcoming_rows, finished_map), cached in the same coupon_ucl_cache
    table UCL already uses (different keys) so settle()/place() don't pay
    the ~20s fetch cost on every cron tick."""
    cache = _ucl_cache_get()
    up_row = cache.get("rest_upcoming")
    fin = (cache.get("rest_results") or {}).get("payload") or {}
    fresh = False
    if up_row:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(up_row["refreshed_at"])).total_seconds()
            fresh = age < _REST_CACHE_TTL
        except Exception:
            fresh = False
    if fresh:
        return up_row["payload"] or [], fin

    try:
        upcoming, new_fin = _rest_compute()
    except Exception:
        return (up_row["payload"] or []) if up_row else [], fin

    _ucl_cache_put("rest_upcoming", upcoming)
    merged = {**fin, **new_fin}
    if new_fin or not cache.get("rest_results"):
        _ucl_cache_put("rest_results", merged)
    return upcoming, merged


def ucl_band(rows):
    if not rows:
        return ""
    cards = []
    for i, r in enumerate(rows):
        f, x = r["fx"], r["x"]
        legs = []
        for key, label in (("goals", "Goals"), ("corners", "Corners"), ("cards", "Cards")):
            line = pick_line(key, x[key])
            side = "Over" if x[key] > line else "Under"
            xtra = ""
            if key == "goals" and x["btts"]:
                xtra = ' <em class="ucl-x">+ BTTS</em>'
            if key == "cards" and x["red_risk"]:
                xtra = ' <em class="ucl-x ucl-x--red">red watch</em>'
            legs.append(
                f'<span class="ucl-l"><b>{label}</b> '
                f'<span class="ucl-p">{side} {line:g}</span> '
                f'<span class="ucl-m">model {half(x[key]):.1f}</span>{xtra}</span>'
            )
        top = " ucl-fx--top" if i == 0 else ""
        cards.append(
            f'<article class="ucl-fx{top}" id="fx-{f["id"]}" data-ko="{f["date"]}">'
            f'<div class="ucl-fh"><span class="ucl-name">{f["home"]} <i>v</i> {f["away"]}</span>'
            f'<span class="ucl-meta">{kickoff(f["date"])} &middot; juice {r["score"]}/100</span></div>'
            f'<div class="ucl-legs">{"".join(legs)}</div>'
            f'<div class="body"></div></article>'
        )
    caveats = "".join(f"<li><b>{h}</b> &mdash; {b}</li>" for h, b in _UCL_CAVEATS)
    n = len(rows)
    return f"""
  <section class="ucl">
    <div class="ucl-head">
      <h2>Champions League &mdash; midweek</h2>
      <span class="ucl-rate">{n} game{'' if n == 1 else 's'} &middot; strongest first</span>
    </div>
    <p class="ucl-key">Model prediction for each metric. Make your predictions
    below &mdash; they score for the <b>same leaderboard and {SSB_TICKER} rewards</b>
    as the weekend sheet. Built from cross-league data, so treat it as a
    <b>steer, not a shout</b>.</p>
    {"".join(cards)}
    <details class="ucl-note">
      <summary>Why to trust this less than the Premier League sheet</summary>
      <ul>{caveats}</ul>
    </details>
  </section>"""


def compute_rows(fx, blends):
    if not fx:
        return []
    rows = []
    for f in fx:
        h, a = blends[f["home_id"]], blends[f["away_id"]]
        x = expected(h, a)
        rows.append({
            "fx": f, "h": h, "a": a, "x": x,
            "legs": suggest(x), "score": rating(x),
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


# How close the model has to be to count as a good "call", per metric.
_TOL = {"goals": 1.0, "corners": 2.5, "cards": 1.5}


def _model_close(key, model, actual):
    return abs(half(model) - actual) <= _TOL[key]


def season_matches(blends):
    """Every finished PL match this season, graded model-vs-actual. One scoreboard
    call per week — the scoreboard carries goals, corners and card events."""
    start = date(THIS_SEASON, 8, 1)
    end = date.today() + timedelta(days=4)
    windows, d = [], start
    while d <= end:
        windows.append((d.strftime("%Y%m%d"), (d + timedelta(days=6)).strftime("%Y%m%d")))
        d += timedelta(days=7)

    seen, graded = set(), []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for fx in ex.map(lambda w: fixtures(*w), windows):
            for f in fx:
                if f["id"] in seen or not f["result"]:
                    continue
                seen.add(f["id"])
                a = f["result"]
                x = expected(blends[f["home_id"]], blends[f["away_id"]])
                model = {k: x[k] for k in SCORED_METRICS}
                within = {k: metric_landed(k, model, a) for k in SCORED_METRICS}
                graded.append({"fx": f, "actual": a, "model": model, "within": within})
    graded.sort(key=lambda g: g["fx"]["date"])
    return graded


def last_weekend_results(season, win):
    """Slice the graded season list down to one weekend window (YYYYMMDD pair)."""
    d1, d2 = win
    items = [g for g in season if d1 <= g["fx"]["date"][:10].replace("-", "") <= d2]
    return d1, d2, items


def season_report(season):
    """Aggregate: overall + per-metric hit rate, and a week-by-week list."""
    if not season:
        return None
    keys = SCORED_METRICS
    by_metric = {k: [0, 0] for k in keys}
    weeks = {}
    for g in season:
        dt = datetime.fromisoformat(g["fx"]["date"].replace("Z", "+00:00"))
        fri = (dt - timedelta(days=(dt.weekday() - 4) % 7)).date()  # football-week Friday
        wk = weeks.setdefault(fri, {"date": g["fx"]["date"], "c": 0, "t": 0})
        for k in keys:
            by_metric[k][1] += 1
            wk["t"] += 1
            if g["within"][k]:
                by_metric[k][0] += 1
                wk["c"] += 1
    close = sum(v[0] for v in by_metric.values())
    total = sum(v[1] for v in by_metric.values())
    week_list = [w for _, w in sorted(weeks.items())]
    for w in week_list:
        w["label"] = match_day(w["date"]).split(" ", 1)[1]  # "29 AUG"
        w["pct"] = round(100 * w["c"] / w["t"]) if w["t"] else 0
    return {
        "close": close, "total": total,
        "pct": round(100 * close / total) if total else 0,
        "by_metric": {k: tuple(v) for k, v in by_metric.items()},
        "weeks": week_list,
    }


# --------------------------------------------------------------------------- #
#  Picks + leaderboard
# --------------------------------------------------------------------------- #

def fetch_picks():
    """Every pick from Supabase (append-only table, stays small enough to pull)."""
    url = (f"{SUPA_URL}/rest/v1/coupon_picks"
           f"?select=player,fixture_id,market,selection,created_at&order=created_at.asc")
    req = urllib.request.Request(url, headers={
        "apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except Exception:
        return []


def _pick_correct(market, selection, res):
    """Did this pick land? res = a finished fixture's result dict (carries lines)."""
    lines = res["lines"]
    if market == "goals_ou":
        return res["goals"] > lines["goals"] if selection == "over" else res["goals"] < lines["goals"]
    if market == "corners_ou":
        return res["corners"] > lines["corners"] if selection == "over" else res["corners"] < lines["corners"]
    if market == "cards_ou":
        return res["cards"] > lines["cards"] if selection == "over" else res["cards"] < lines["cards"]
    if market == "btts":
        return res["btts"] if selection == "yes" else not res["btts"]
    if market == "result":
        d = res["h_goals"] - res["a_goals"]
        return {"home": d > 0, "draw": d == 0, "away": d < 0}[selection]
    return False


def standings(picks, finished, weekend_ids):
    """finished = {fixture_id: {result..., kickoff}}; weekend_ids = this weekend.
    A pick scores only if it was made before the real kickoff. 1 pt per hit."""
    latest = {}  # (player, fixture, market) -> row  (last pick wins)
    for p in picks:
        latest[(p["player"], p["fixture_id"], p["market"])] = p

    season, week = {}, {}
    for (player, fid, market), p in latest.items():
        info = finished.get(fid)
        if not info:
            continue
        if p["created_at"] >= info["kickoff"]:
            continue
        hit = 1 if _pick_correct(market, p["selection"], info) else 0
        s = season.setdefault(player, [0, 0])
        s[0] += hit
        s[1] += 1
        if fid in weekend_ids:
            w = week.setdefault(player, [0, 0])
            w[0] += hit
            w[1] += 1

    def table(d):
        rows = [{"name": n, "pts": v[0], "graded": v[1]} for n, v in d.items()]
        rows.sort(key=lambda r: (-r["pts"], -r["graded"], r["name"].lower()))
        return rows[:50]

    return {"weekend": table(week), "season": table(season)}


BOT_NAME = "Weekend Coupon"


def _model_tally(graded, ids=None):
    """Same 1-pt-per-correct-prediction scoring as standings(), but for the
    model's own suggested picks across every graded match (or just `ids`)."""
    hits = total = 0
    for g in graded:
        if ids is not None and g["fx"]["id"] not in ids:
            continue
        for k in SCORED_METRICS:
            if k in ("corners", "cards") and not g["actual"]["has_stats"]:
                continue
            total += 1
            if metric_landed(k, g["model"], g["actual"]):
                hits += 1
    return hits, total


def add_model_entry(stand, season_graded, weekend_ids):
    """Add the model's own season-long track record as a 'Weekend Coupon' row
    on the DISPLAY leaderboard only. Never touches payout_round() - the bot
    has no wallet and must never receive a share of the SSB pool."""
    def insert(rows, hits, total):
        if total == 0:
            return rows
        rows = [r for r in rows if r["name"] != BOT_NAME]
        rows.append({"name": BOT_NAME, "pts": hits, "graded": total})
        rows.sort(key=lambda r: (-r["pts"], -r["graded"], r["name"].lower()))
        return rows[:50]

    s_hits, s_total = _model_tally(season_graded)
    w_hits, w_total = _model_tally(season_graded, weekend_ids)
    stand["season"] = insert(stand["season"], s_hits, s_total)
    stand["weekend"] = insert(stand["weekend"], w_hits, w_total)
    return stand


def fetch_wallets():
    """Latest linked wallet per player, {player: wallet}."""
    url = (f"{SUPA_URL}/rest/v1/coupon_wallets"
           f"?select=player,wallet,created_at&order=created_at.desc")
    req = urllib.request.Request(url, headers={
        "apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
    })
    out = {}
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            for w in json.load(r):
                out.setdefault(w["player"], w["wallet"])
    except Exception:
        pass
    return out


def _finished_map(season):
    return {
        g["fx"]["id"]: {
            **{k: g["actual"][k] for k in ("goals", "corners", "cards", "h_goals",
                                           "a_goals", "btts")},
            "kickoff": g["fx"]["date"],
            "lines": {k: pick_line(k, g["model"][k]) for k in ("goals", "corners", "cards")},
        }
        for g in season
    }


def _fixture_info_map(season, rows, ucl_up):
    """id -> {home, away, date, lines} for every fixture we know about this
    season (finished PL, this week's PL, or upcoming UCL) - lets the client
    render + grade a player's OWN pick history for any past week, not just
    the current one (name/date come from here, the actual result from the
    `results`/finished map when one exists)."""
    info = {}
    for g in season:
        f, x = g["fx"], g["model"]
        info[f["id"]] = {
            "home": f["home_abbr"], "away": f["away_abbr"], "date": f["date"],
            "lines": {k: pick_line(k, x[k]) for k in ("goals", "corners", "cards")},
        }
    for r in rows:
        f, x = r["fx"], r["x"]
        info[f["id"]] = {
            "home": f["home_abbr"], "away": f["away_abbr"], "date": f["date"],
            "lines": {k: pick_line(k, x[k]) for k in ("goals", "corners", "cards")},
        }
    for u in ucl_up:
        f, x = u["fx"], u["x"]
        info[f["id"]] = {
            "home": f.get("home_abbr", f.get("home")), "away": f.get("away_abbr", f.get("away")),
            "date": f["date"],
            "lines": {k: pick_line(k, x[k]) for k in ("goals", "corners", "cards")},
        }
    return info


SEASON_PRIZES = [1_000_000, 500_000, 250_000]   # 1st / 2nd / 3rd
SEASON_ROUND = f"season:{THIS_SEASON}-{(THIS_SEASON + 1) % 100:02d}"


def _weekend_complete(win):
    (_, _), fx = weekend_fixtures(win, roll_ahead=0)
    return bool(fx) and all(f["completed"] for f in fx)


def payout_round(mode="weekly"):
    """Returns (round_id, description, [ {player, wallet, points, ssb} ]).
    weekly  -> every PL + UCL pick that settled in the last 7 days, x SSB_PER_POINT
    season  -> top 3 on the season board get SEASON_PRIZES
    """
    prev_win, cur_win = weekend_windows()
    blends = team_blends()
    season = season_matches(blends)
    finished = _finished_map(season)
    _, ucl_fin = _safe_ucl_context()
    finished.update(ucl_fin)
    picks = fetch_picks()
    wallets = fetch_wallets()

    if mode == "season":
        stand = standings(picks, finished, set())
        rows = stand["season"][:3]
        out = [
            {"player": r["name"], "wallet": wallets.get(r["name"]),
             "points": r["pts"], "ssb": SEASON_PRIZES[i]}
            for i, r in enumerate(rows) if r["pts"] > 0
        ]
        return SEASON_ROUND, f"Season finale — top {len(out)}", out

    # weekly: everything (PL weekend + UCL midweek) that settled in the last 7 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    week_ids = {fid for fid, info in finished.items() if info["kickoff"] >= cutoff}
    stand = standings(picks, finished, week_ids)
    pd1 = (date.today() - timedelta(days=7)).strftime("%Y%m%d")

    rows = [r for r in stand["weekend"] if r["pts"] > 0]
    out = [
        {"player": r["name"], "wallet": wallets.get(r["name"]),
         "points": r["pts"], "ssb": r["pts"] * SSB_PER_POINT}
        for r in rows
    ]
    return f"weekly:{pd1}", f"Week to {date.today():%d.%m} settled", out


def generate_html(d1=None, d2=None):
    if d1 and d2:
        prev_win, cur_win = None, (d1, d2)
    else:
        prev_win, cur_win = weekend_windows()

    blends = team_blends()
    (cd1, cd2), fx = weekend_fixtures(cur_win)
    rows = compute_rows(fx, blends)
    season = season_matches(blends)
    results = last_weekend_results(season, prev_win) if prev_win else None
    report = season_report(season)

    if not rows and not (results and results[2]):
        return ('<meta charset="utf-8"><title>Weekend Coupon</title>'
                '<div style="font-family:sans-serif;max-width:600px;margin:48px auto;'
                'padding:0 20px"><h1>Nothing to show yet</h1><p>No Premier League '
                'fixtures in range and no results from last weekend &mdash; likely an '
                'international break. Check back Friday.</p></div>')

    pl_finished = _finished_map(season)
    ucl_up, ucl_fin = _safe_ucl_context()
    finished = {**pl_finished, **ucl_fin}
    weekend_ids = [r["fx"]["id"] for r in rows] + [u["fx"]["id"] for u in ucl_up]
    stand = standings(fetch_picks(), finished, set(weekend_ids))
    stand = add_model_entry(stand, season, set(weekend_ids))

    cfg = {
        "supaUrl": SUPA_URL, "supaKey": SUPA_KEY,
        "ssb": {"ticker": SSB_TICKER, "perPoint": SSB_PER_POINT, "mint": SSB_MINT,
                "payout": SSB_PAYOUT_WALLET, "weeklyCap": SSB_WEEKLY_CAP},
        "adminHash": ADMIN_HASH,
        "fixtures": [
            {"id": r["fx"]["id"], "home": r["fx"]["home_abbr"],
             "away": r["fx"]["away_abbr"], "kickoff": r["fx"]["date"],
             "lines": {k: pick_line(k, r["x"][k]) for k in ("goals", "corners", "cards")}}
            for r in sorted(rows, key=lambda r: r["fx"]["date"])
        ],
        "results": finished,
        "fixtureInfo": _fixture_info_map(season, rows, ucl_up),
        "standings": stand,
    }

    built_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return render(rows, cd1, cd2, uk_now(), results, built_iso, report, cfg,
                  None, "pl", season)


def _safe_ucl_context():
    try:
        return ucl_context()
    except Exception:
        return [], {}


def generate_ucl_html():
    """Page two — Champions League. Its own pick game, shared leaderboard."""
    blends = team_blends()
    season = season_matches(blends)
    _, cur_win = weekend_windows()
    (_, _), pl_fx = weekend_fixtures(cur_win)
    pl_rows = compute_rows(pl_fx, blends)

    ucl_up, ucl_fin = _safe_ucl_context()
    finished = {**_finished_map(season), **ucl_fin}
    week_ids = [r["fx"]["id"] for r in pl_rows] + [u["fx"]["id"] for u in ucl_up]
    stand = standings(fetch_picks(), finished, set(week_ids))
    stand = add_model_entry(stand, season, set(week_ids))

    cfg = {
        "supaUrl": SUPA_URL, "supaKey": SUPA_KEY,
        "ssb": {"ticker": SSB_TICKER, "perPoint": SSB_PER_POINT, "mint": SSB_MINT,
                "payout": SSB_PAYOUT_WALLET, "weeklyCap": SSB_WEEKLY_CAP},
        "adminHash": ADMIN_HASH,
        "fixtures": [
            {"id": u["fx"]["id"], "home": u["fx"]["home"], "away": u["fx"]["away"],
             "kickoff": u["fx"]["date"],
             "lines": {k: pick_line(k, u["x"][k]) for k in ("goals", "corners", "cards")}}
            for u in sorted(ucl_up, key=lambda u: u["fx"]["date"])
        ],
        "results": finished,
        "fixtureInfo": _fixture_info_map(season, pl_rows, ucl_up),
        "standings": stand,
    }

    built_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return render([], "", "", uk_now(), None, built_iso, None, cfg, ucl_up, "ucl")


def build():
    if len(sys.argv) == 3:
        html = generate_html(sys.argv[1], sys.argv[2])
    else:
        html = generate_html()
    (HERE / "dashboard.html").write_text(html, encoding="utf-8")
    (HERE / "champions-league.html").write_text(generate_ucl_html(), encoding="utf-8")
    print(f"Wrote {HERE / 'dashboard.html'}  ({len(html)} bytes)")


# --------------------------------------------------------------------------- #
#  HTML rendering
# --------------------------------------------------------------------------- #

def half(x):
    """Snap to the nearest 0.5 — betting lines only come in .0 / .5 steps."""
    return round(x * 2) / 2


def meter(kind, label, value, lo, hi, ticks):
    value = half(value)
    pct = max(3, min(100, (value - lo) / (hi - lo) * 100))
    tick_html = "".join(
        f'<span class="tick" style="left:{(t - lo) / (hi - lo) * 100:.1f}%">'
        f'<i></i><em>{t:g}</em></span>'
        for t in ticks
    )
    return f"""
        <div class="meter meter--{kind}">
          <div class="meter-head"><span>{label}</span><b>{value:.1f}</b></div>
          <div class="track">
            <div class="fill" style="--w:{pct:.1f}%"></div>
            {tick_html}
          </div>
        </div>"""


def kickoff(iso):
    dt = _uk(datetime.fromisoformat(iso.replace("Z", "+00:00")))
    return dt.strftime("%a %d %b · %H:%M").upper()


def match_day(iso):
    return _uk(datetime.fromisoformat(iso.replace("Z", "+00:00"))).strftime("%a %d %b").upper()


def season_band(report):
    if not report or report["total"] < 6:
        return ""
    labels = {"goals": "Goals", "corners": "Corners", "cards": "Cards", "btts": "BTTS", "result": "Result"}
    metric_rows = "".join(
        f"""
      <div class="smetric">
        <b>{labels[k]}</b>
        <div class="sbar"><span style="--w:{round(100 * c / t) if t else 0}%"></span></div>
        <i>{round(100 * c / t) if t else 0}%</i>
      </div>"""
        for k, (c, t) in report["by_metric"].items()
    )
    weeks = "".join(
        f'<span class="swk" title="{w["label"]} &middot; {w["pct"]}% ({w["c"]}/{w["t"]})">'
        f'<span class="swk-bar" style="--h:{max(6, w["pct"])}%"></span>'
        f'<em>{w["label"].split(" ")[0]}</em></span>'
        for w in report["weeks"]
    )
    return f"""
  <section class="season">
    <div class="season-head">
      <h2>Model accuracy &mdash; season to date</h2>
      <span class="season-pct">{report['pct']}&#37;</span>
    </div>
    <div class="sbar sbar--all"><span style="--w:{report['pct']}%"></span></div>
    <p class="season-sub">{report['close']} of {report['total']} suggested goal / corner /
    card / BTTS / result predictions landed. Bars below: each metric, then week by
    week. Each prediction is scored on its own &mdash; getting some right on a
    match and others wrong still counts each one individually, not as a miss.</p>
    <div class="season-metrics">{metric_rows}</div>
    <div class="season-weeks">{weeks}</div>
  </section>"""


def result_band(results):
    if not results or not results[2]:
        return ""
    d1, d2, items = results
    close = total = 0
    for it in items:
        for key in SCORED_METRICS:
            if key in ("corners", "cards") and not it["actual"]["has_stats"]:
                continue
            total += 1
            if metric_landed(key, it["model"], it["actual"]):
                close += 1
    cards_html = "".join(_result_card(it) for it in items)
    return f"""
  <section class="results">
    <div class="results-head">
      <h2>Last weekend &mdash; how the predictions did</h2>
      <span class="results-rate">{close}/{total} landed</span>
    </div>
    <p class="results-key">Each metric shows the <b>prediction</b> &rarr; the
    <b>actual</b> number in the match. &#10003; = it landed.</p>
    {cards_html}
  </section>"""


def _result_card(it):
    f, a, m = it["fx"], it["actual"], it["model"]
    lines = []
    for key, label in (("goals", "Goals"), ("corners", "Corners"), ("cards", "Cards")):
        line = pick_line(key, m[key])
        side = "Over" if m[key] > line else "Under"
        if key in ("corners", "cards") and not a["has_stats"]:
            lines.append(f'<span class="rzl rzl--na"><b>{label}</b> '
                         f'{side} {line:g} &rarr; &mdash;</span>')
            continue
        actual = a[key]
        cls = "hit" if pick_landed(key, m[key], actual) else "miss"
        lines.append(f'<span class="rzl rzl--{cls}"><b>{label}</b> '
                     f'<span class="rzl-p">{side} {line:g}</span> &rarr; {actual}</span>')

    btts_cls = "hit" if m["btts"] == a["btts"] else "miss"
    lines.append(f'<span class="rzl rzl--{btts_cls}"><b>BTTS</b> '
                 f'<span class="rzl-p">{"Yes" if m["btts"] else "No"}</span> '
                 f'&rarr; {"Yes" if a["btts"] else "No"}</span>')

    real_result = _actual_result(a["h_goals"], a["a_goals"])
    result_label = {"home": f["home_abbr"], "away": f["away_abbr"], "draw": "Draw"}
    result_cls = "hit" if m["result"] == real_result else "miss"
    lines.append(f'<span class="rzl rzl--{result_cls}"><b>Result</b> '
                 f'<span class="rzl-p">{result_label[m["result"]]}</span> '
                 f'&rarr; {result_label[real_result]}</span>')
    return f"""
      <div class="rz">
        <div class="rz-score">
          <span class="rz-day">{match_day(f['date'])}</span>
          <b>{f['home_abbr']}</b> {a['h_goals']}&#8211;{a['a_goals']} <b>{f['away_abbr']}</b>
        </div>
        <div class="rz-lines">{''.join(lines)}</div>
      </div>"""


def _week_of(iso_date):
    dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    return (dt - timedelta(days=(dt.weekday() - 4) % 7)).date()   # football-week Friday


def history_band(season):
    """Every past week this season, collapsed behind a History button — each
    week expandable, each match graded the same way as the last-weekend band."""
    if not season:
        return ""
    by_week = {}
    for g in season:
        by_week.setdefault(_week_of(g["fx"]["date"]), []).append(g)
    weeks = sorted(by_week.items(), key=lambda kv: kv[0], reverse=True)
    if not weeks:
        return ""

    week_html = []
    for i, (fri, games) in enumerate(weeks):
        won = total = 0
        for g in games:
            for key in SCORED_METRICS:
                if key in ("corners", "cards") and not g["actual"]["has_stats"]:
                    continue
                total += 1
                if metric_landed(key, g["model"], g["actual"]):
                    won += 1
        span = match_day(games[0]["fx"]["date"]) if len(games) == 1 else \
            f"w/o {fri.strftime('%d %b').upper()}"
        cards = "".join(_result_card(g) for g in sorted(games, key=lambda g: g["fx"]["date"]))
        week_html.append(f"""
      <details class="wk"{' open' if i == 0 else ''}>
        <summary><span class="wk-span">{span}</span>
          <span class="wk-rate">{won}/{total} landed</span></summary>
        <div class="wk-body">{cards}</div>
      </details>""")

    return f"""
  <section class="history">
    <button type="button" class="hist-btn" id="hist-btn"
      onclick="var p=document.getElementById('hist-panel');p.hidden=!p.hidden;
               this.textContent=p.hidden?'History':'Hide history';">History</button>
    <div id="hist-panel" class="hist-panel" hidden>
      <h2>Result history</h2>
      {"".join(week_html)}
    </div>
  </section>"""


def sponsors_html():
    return "".join(
        f"""
    <a class="sponsor" href="{url}" target="_blank" rel="noopener sponsored">
      <img src="{logo}" alt="{name} logo" width="40" height="40" loading="lazy">
      <span>
        <em>Sponsored by</em>
        <b>{name}</b>
        <i>{tagline}</i>
      </span>
    </a>"""
        for name, tagline, url, logo in SPONSORS
    )


PICKS_JS = r"""
(function () {
  var cfgEl = document.getElementById('coupon-cfg');
  var cfg; try { cfg = JSON.parse(cfgEl.textContent); } catch (e) { return; }
  var panel = document.getElementById('mypicks');
  var sb = (window.supabase && cfg.supaUrl)
    ? window.supabase.createClient(cfg.supaUrl, cfg.supaKey) : null;
  if (!cfg.fixtures || !cfg.fixtures.length) { return; }

  var MARKETS = [
    { k: 'goals_ou',   metric: 'goals',   label: 'Goals',   opts: [['over','Over'],['under','Under']] },
    { k: 'corners_ou', metric: 'corners', label: 'Corners', opts: [['over','Over'],['under','Under']] },
    { k: 'cards_ou',   metric: 'cards',   label: 'Cards',   opts: [['over','Over'],['under','Under']] },
    { k: 'btts',       label: 'BTTS',    opts: [['yes','Yes'],['no','No']] },
    { k: 'result',     label: 'Result',  opts: [['home','Home'],['draw','Draw'],['away','Away']] }
  ];
  function lineFor(fx, m) { return m.metric ? fx.lines[m.metric] : null; }
  var LS_NAME = 'coupon_player';
  var player = null;
  try { player = localStorage.getItem(LS_NAME) || null; } catch (e) {}
  var myPicks = {};

  function fxById(id) { for (var i=0;i<cfg.fixtures.length;i++) if (cfg.fixtures[i].id===id) return cfg.fixtures[i]; }
  function locked(fx) { return Date.now() >= Date.parse(fx.kickoff); }
  function optLabel(m, sel) { for (var i=0;i<m.opts.length;i++) if (m.opts[i][0]===sel) return m.opts[i][1]; return sel; }
  function marketBy(k) { for (var i=0;i<MARKETS.length;i++) if (MARKETS[i].k===k) return MARKETS[i]; }

  function correct(fx, k, sel, r) {
    if (k === 'goals_ou')   return sel === 'over' ? r.goals   > fx.lines.goals   : r.goals   < fx.lines.goals;
    if (k === 'corners_ou') return sel === 'over' ? r.corners > fx.lines.corners : r.corners < fx.lines.corners;
    if (k === 'cards_ou')   return sel === 'over' ? r.cards   > fx.lines.cards   : r.cards   < fx.lines.cards;
    if (k === 'btts')       return sel === 'yes'  ? r.btts : !r.btts;
    if (k === 'result')     { var d = r.h_goals - r.a_goals; return sel==='home'?d>0:sel==='draw'?d===0:d<0; }
    return false;
  }

  function wkKey() { return 'coupon_picks_' + cfg.fixtures[0].kickoff.slice(0, 10); }
  function saveLocal() { try { localStorage.setItem(wkKey(), JSON.stringify(myPicks)); } catch (e) {} }
  function loadLocal() { try { myPicks = JSON.parse(localStorage.getItem(wkKey()) || '{}') || {}; } catch (e) { myPicks = {}; } }

  function renderPicks() {
    document.querySelectorAll('.picks').forEach(function (x) { x.remove(); });
    cfg.fixtures.forEach(function (fx) {
      var card = document.getElementById('fx-' + fx.id);
      if (!card) return;
      var host = card.querySelector('.body') || card;
      var lk = locked(fx);
      var mine = myPicks[fx.id] || {};
      var rows = MARKETS.map(function (m) {
        var ln = lineFor(fx, m);
        var lt = ln ? ' ' + ln : '';
        var btns = m.opts.map(function (o) {
          var on = mine[m.k] === o[0] ? ' on' : '';
          return '<button class="pk' + on + '" data-fx="' + fx.id + '" data-mk="' + m.k +
                 '" data-sel="' + o[0] + '"' + (lk ? ' disabled' : '') + '>' + o[1] + '</button>';
        }).join('');
        return '<div class="pkrow"><b>' + m.label + lt + '</b><div class="pkbtns">' + btns + '</div></div>';
      }).join('');
      var hasPicks = Object.keys(mine).length > 0;
      var w = document.createElement('div');
      w.className = 'picks' + (lk ? ' picks--locked' : '');
      if (lk && !hasPicks) {
        w.innerHTML = '<div class="picks-h">Your predictions <span>&middot; locked</span></div>' +
                      '<p class="pk-note">Kicked off &mdash; no prediction made on this one.</p>';
      } else {
        w.innerHTML = '<div class="picks-h">Your predictions' +
          (lk ? ' <span>&middot; locked</span>' : '') + '</div>' +
          '<p class="pk-note"><b>Once tapped, pick is live.</b> You can change ' +
          'your mind any time before kickoff &mdash; the last one you tap is ' +
          'the one that counts. Once the match kicks off, your picks lock in.</p>' +
          rows;
      }
      host.appendChild(w);
    });
    document.querySelectorAll('.pk').forEach(function (b) {
      b.addEventListener('click', function () { onPick(b); });
    });
  }

  function onPick(btn) {
    var fid = btn.dataset.fx, mk = btn.dataset.mk, sel = btn.dataset.sel;
    var fx = fxById(fid);
    if (!fx || locked(fx)) return;
    if (!player) { openName(function () { onPick(btn); }); return; }
    btn.parentNode.querySelectorAll('.pk').forEach(function (x) { x.classList.remove('on'); });
    btn.classList.add('on');
    (myPicks[fid] = myPicks[fid] || {})[mk] = sel;
    saveLocal();
    renderMine();
    if (sb) sb.from('coupon_picks').insert({
      player: player, fixture_id: fid, market: mk, selection: sel, kickoff: fx.kickoff
    }).then(function (r) { if (r && r.error) console.warn('pick save failed', r.error); });
  }

  function renderMine() {
    if (!panel) return;
    var items = [], pts = 0, settled = 0, n = 0;
    cfg.fixtures.forEach(function (fx) {
      var mine = myPicks[fx.id]; if (!mine) return;
      var res = cfg.results[fx.id];
      Object.keys(mine).forEach(function (mk) {
        n++;
        var m = marketBy(mk), sel = mine[mk], ln = lineFor(fx, m);
        var lab = m.label + (ln ? ' ' + ln : '') + ' · ' + optLabel(m, sel);
        var mark = '<i class="pk-pend">&middot;</i>';
        if (res) {
          settled++;
          var ok = correct(fx, mk, sel, res);
          if (ok) pts++;
          mark = ok ? '<i class="pk-hit">&#10003;</i>' : '<i class="pk-miss">&#10007;</i>';
        }
        items.push('<li><span class="mp-fx">' + fx.home + ' v ' + fx.away +
                   '</span><span class="mp-sel">' + lab + '</span>' + mark + '</li>');
      });
    });
    var tk = cfg.ssb ? cfg.ssb.ticker : 'pts';
    var per = cfg.ssb ? cfg.ssb.perPoint : 1;
    var WEEKLY_CAP = (cfg.ssb && cfg.ssb.weeklyCap) || 200000;
    var body = items.length
      ? '<ul class="mp-list">' + items.join('') + '</ul>' +
        '<div class="mp-foot"><b>' + pts + ' pt' + (pts === 1 ? '' : 's') + '</b> = ' +
        (pts * per).toLocaleString() + ' ' + tk + ' &middot; ' + settled + ' of ' + n + ' settled</div>'
      : '<p class="mp-empty">Tap the prediction buttons on any fixture below to start.</p>';
    panel.hidden = false;
    panel.innerHTML =
      '<div class="mp-h"><b>My predictions</b><span class="mp-h-btns">' +
      '<button type="button" id="lb-open">Leaderboard</button>' +
      '<button type="button" id="hist-open">Player History</button>' +
      '</span></div>' +
      (player ? '<div class="mp-who">' + player +
        ' <button type="button" id="wl-signout">sign out</button>' +
        ' <button type="button" id="wl-pin">change PIN</button>' +
        ' <button type="button" id="wl-recovery">recovery code</button></div>' : '') +
      walletRow() +
      (cfg.ssb && cfg.ssb.mint ?
        '<div class="mp-ca"><span>$' + cfg.ssb.ticker + ' contract</span>' +
        '<a href="https://solscan.io/token/' + cfg.ssb.mint + '" target="_blank" rel="noopener">' +
        cfg.ssb.mint + '</a>' +
        '<button type="button" class="mp-copy" data-ca="' + cfg.ssb.mint + '">copy</button></div>' : '') +
      (cfg.ssb && cfg.ssb.payout ?
        '<div class="mp-ca"><span>Official payout wallet</span>' +
        '<a href="https://solscan.io/account/' + cfg.ssb.payout + '" target="_blank" rel="noopener">' +
        cfg.ssb.payout + '</a>' +
        '<button type="button" class="mp-copy" data-ca="' + cfg.ssb.payout + '">copy</button></div>' : '') +
      '<div class="mp-note">' + (cfg.ssb ? cfg.ssb.ticker : 'Token') +
      ' rewards are only ever sent <b>from</b> that address, straight <b>to</b> the wallet ' +
      'you linked. We will never DM you, ask for your seed phrase or private key, or ask ' +
      'you to send SOL/SSB to unlock a payout &mdash; anyone who does is scamming you.<br>' +
      'Weekly payouts are capped at <b>' + WEEKLY_CAP.toLocaleString() + ' ' +
      (cfg.ssb ? cfg.ssb.ticker : 'SSB') + '</b> total. If a week goes over that, ' +
      "that week's payout is held for a manual check before anything is sent &mdash; " +
      'you have not lost your points.<br>' +
      'The ' + (cfg.ssb ? cfg.ssb.ticker : 'SSB') + ' amount paid per point may be ' +
      '<b>lowered</b> as the game grows &mdash; the weekly pool is fixed, so the more ' +
      'players share it, the less each point is worth.<br>' +
      '<b class="mp-imp">Important:</b> no tokens are being sent yet. We are taking proper advice ' +
      'before any SSB goes out, so payouts are paused. Your points and leaderboard ' +
      'place are safe and carry over.</div>' +
      body +
      '<button type="button" class="mp-pay" id="pay-open">Payout sheet</button>';
    var lb = document.getElementById('lb-open');
    if (lb) lb.addEventListener('click', openLB);
    var ho = document.getElementById('hist-open');
    if (ho) ho.addEventListener('click', function () { openHistory(); });
    var wc = document.getElementById('wl-connect');
    if (wc) wc.addEventListener('click', linkWallet);
    var wd = document.getElementById('wl-disconnect');
    if (wd) wd.addEventListener('click', disconnectWallet);
    var si = document.getElementById('wl-signin');
    if (si) si.addEventListener('click', signInWithWallet);
    var ac = document.getElementById('wl-account');
    if (ac) ac.addEventListener('click', function () { openName(); });
    var so = document.getElementById('wl-signout');
    if (so) so.addEventListener('click', signOut);
    var pn = document.getElementById('wl-pin');
    if (pn) pn.addEventListener('click', changePin);
    var rv = document.getElementById('wl-recovery');
    if (rv) rv.addEventListener('click', newRecoveryCode);
    var po = document.getElementById('pay-open');
    if (po) po.addEventListener('click', openPayout);
    panel.querySelectorAll('.mp-copy').forEach(function (cp) {
      cp.addEventListener('click', function () {
        (navigator.clipboard ? navigator.clipboard.writeText(cp.dataset.ca) : Promise.reject())
          .then(function () {
            cp.textContent = 'copied';
            setTimeout(function () { cp.textContent = 'copy'; }, 1500);
          }).catch(function () {});
      });
    });
  }

  /* ---- Phantom wallet ---- */
  var LS_WALLET = 'coupon_wallet';
  var wallet = null;
  try { wallet = localStorage.getItem(LS_WALLET) || null; } catch (e) {}

  function shortW(w) { return w.slice(0, 4) + '…' + w.slice(-4); }
  function walletRow() {
    var tk = cfg.ssb ? cfg.ssb.ticker : 'the token';
    if (!player) {
      return '<div class="mp-wallet">' +
             '<button type="button" id="wl-account">Create account / sign in with PIN</button>' +
             '<button type="button" id="wl-signin">Sign in with Phantom</button>' +
             '<span>new here? tap Create account. Coming back? use your PIN, or your linked wallet.</span></div>';
    }
    if (wallet) {
      return '<div class="mp-wallet linked">Wallet <b>' + shortW(wallet) +
             '</b> linked <button type="button" id="wl-disconnect" class="wl-change">disconnect wallet</button></div>';
    }
    return '<div class="mp-wallet"><button type="button" id="wl-connect">Connect Phantom</button>' +
           '<span>link a wallet to get ' + tk + ' payouts</span></div>';
  }

  function signOut() {
    try {
      localStorage.removeItem(LS_NAME);
      localStorage.removeItem(LS_WALLET);
      localStorage.removeItem(wkKey());
    } catch (e) {}
    if (window.solana && window.solana.disconnect) {
      try { window.solana.disconnect(); } catch (e) {}
    }
    player = null;
    wallet = null;
    sessionPin = null;
    myPicks = {};
    renderPicks();
    renderMine();
  }

  function disconnectWallet() {
    wallet = null;
    try { localStorage.removeItem(LS_WALLET); } catch (e) {}
    try {
      localStorage.removeItem('phantom_dl_sk');
      localStorage.removeItem('phantom_dl_intent');
    } catch (e) {}
    if (window.solana && window.solana.disconnect) {
      try { window.solana.disconnect(); } catch (e) {}
    }
    renderMine();
  }

  function hasInjectedPhantom() { return !!(window.solana && window.solana.isPhantom); }

  /* ---- base58 (for the Phantom mobile deep-link handshake) ---- */
  var B58A = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  function b58enc(bytes) {
    var d = [], s = '', i, j, c;
    for (i = 0; i < bytes.length; i++) {
      c = bytes[i];
      for (j = 0; j < d.length; j++) { c += d[j] << 8; d[j] = c % 58; c = (c / 58) | 0; }
      while (c) { d.push(c % 58); c = (c / 58) | 0; }
    }
    for (i = 0; i < bytes.length && bytes[i] === 0; i++) s += '1';
    for (i = d.length - 1; i >= 0; i--) s += B58A[d[i]];
    return s;
  }
  function b58dec(str) {
    var d = [], b = [], i, j, c;
    for (i = 0; i < str.length; i++) {
      c = B58A.indexOf(str[i]); if (c < 0) return null;
      for (j = 0; j < d.length; j++) { c += d[j] * 58; d[j] = c & 255; c >>= 8; }
      while (c) { d.push(c & 255); c >>= 8; }
    }
    for (i = 0; i < str.length && str[i] === '1'; i++) b.push(0);
    for (i = d.length - 1; i >= 0; i--) b.push(d[i]);
    return new Uint8Array(b);
  }

  /* ---- Phantom mobile deep-link ---- */
  var DL_SK = 'phantom_dl_sk', DL_INTENT = 'phantom_dl_intent';
  var pendingLinkAddr = null;

  function dlConnect(intent) {
    if (!window.nacl) { alert('Could not load the wallet library. Check your connection and try again.'); return; }
    var kp = nacl.box.keyPair();
    try {
      localStorage.setItem(DL_SK, b58enc(kp.secretKey));
      localStorage.setItem(DL_INTENT, intent);
    } catch (e) {}
    var redirect = location.origin + location.pathname + '?phantom=connect';
    var qs = 'dapp_encryption_public_key=' + b58enc(kp.publicKey) +
             '&cluster=mainnet-beta&app_url=' + encodeURIComponent(location.origin) +
             '&redirect_link=' + encodeURIComponent(redirect);
    if (/android/i.test(navigator.userAgent)) {
      // Android intent: force-open the Phantom app, else Play Store.
      // (Samsung Internet often won't honour a plain universal link.)
      var fb = encodeURIComponent('https://play.google.com/store/apps/details?id=app.phantom');
      location.href = 'intent://phantom.app/ul/v1/connect?' + qs +
        '#Intent;scheme=https;package=app.phantom;S.browser_fallback_url=' + fb + ';end';
    } else {
      location.href = 'https://phantom.app/ul/v1/connect?' + qs;
    }
  }

  function handlePhantomReturn() {
    var q = new URLSearchParams(location.search);
    if (q.get('phantom') !== 'connect') return;
    history.replaceState(null, '', location.origin + location.pathname);
    var skStr = '', intent = 'signin';
    try {
      skStr = localStorage.getItem(DL_SK) || '';
      intent = localStorage.getItem(DL_INTENT) || 'signin';
      localStorage.removeItem(DL_SK);
      localStorage.removeItem(DL_INTENT);
    } catch (e) {}
    if (q.get('errorCode')) return;
    var pk = q.get('phantom_encryption_public_key'),
        nonce = q.get('nonce'), data = q.get('data');
    if (!pk || !nonce || !data || !window.nacl) return;
    var sk = skStr ? b58dec(skStr) : null;
    if (!sk) {
      alert('Almost there — finish signing in from the same browser you started in.');
      return;
    }
    var dec;
    try {
      dec = nacl.box.open.after(b58dec(data), b58dec(nonce), nacl.box.before(b58dec(pk), sk));
    } catch (e) { return; }
    if (!dec) return;
    var addr;
    try { addr = JSON.parse(new TextDecoder().decode(dec)).public_key; } catch (e) { return; }
    if (!addr) return;
    if (intent === 'link') dlLink(addr); else dlSignIn(addr);
  }

  function dlSignIn(addr) {
    if (!sb) return;
    sb.from('coupon_wallets').select('player').eq('wallet', addr)
      .order('created_at', { ascending: false }).limit(1).then(function (r) {
        if (r && r.data && r.data[0]) {
          wallet = addr;
          try { localStorage.setItem(LS_WALLET, addr); } catch (e) {}
          setPlayer(r.data[0].player);
        } else {
          alert('This wallet is not linked to an account yet.\nPick a name to start — it will be linked to this wallet.');
          pendingLinkAddr = addr;
          openName(function () { if (pendingLinkAddr) { dlLink(pendingLinkAddr); pendingLinkAddr = null; } });
        }
      });
  }

  function linkDone(r, addr) {
    if (!r.error && r.data && r.data.ok) {
      wallet = addr;
      try { localStorage.setItem(LS_WALLET, addr); } catch (e) {}
      renderMine();
      return;
    }
    var reason = (r.data && r.data.reason) || 'err';
    if (reason === 'bad') sessionPin = null;    // wrong PIN — prompt fresh next time
    wallet = null;
    try { localStorage.removeItem(LS_WALLET); } catch (e) {}
    renderMine();
    alert(
      reason === 'wallet_taken'
        ? 'That wallet is already linked to another account. Use "Sign in with Phantom" to get into that one.'
      : reason === 'locked' ? 'Too many wrong PIN tries. Wait 15 minutes and try again.'
      : reason === 'bad' ? 'That PIN is not right for "' + player + '".'
      : reason === 'bad_wallet' ? 'That does not look like a valid wallet address.'
      : 'Could not link the wallet. Try again in a moment.'
    );
  }

  function dlLink(addr) {
    if (!player) { pendingLinkAddr = addr; openName(function () { dlLink(addr); }); return; }
    if (!sb) return;
    var pin = askPin();
    if (!pin) return;
    sb.rpc('coupon_link_wallet', { p_name: player, p_pin: pin, p_wallet: addr })
      .then(function (r) { linkDone(r, addr); });
  }

  function signInWithWallet() {
    if (!hasInjectedPhantom()) { dlConnect('signin'); return; }
    if (!sb) { alert('Connection lost — try again in a moment.'); return; }
    window.solana.connect().then(function (res) {
      var addr = res.publicKey.toString();
      return sb.from('coupon_wallets').select('player')
        .eq('wallet', addr).order('created_at', { ascending: false }).limit(1)
        .then(function (r) {
          if (r && r.data && r.data[0]) {
            wallet = addr;
            try { localStorage.setItem(LS_WALLET, addr); } catch (e) {}
            setPlayer(r.data[0].player);
          } else {
            alert('This wallet is not linked to an account yet.\nPick a name to start — it will be linked to this wallet.');
            openName(linkWallet);
          }
        });
    }).catch(function (e) { console.warn('sign-in cancelled', e); });
  }

  function b64(bytes) {
    var s = ''; for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }

  function linkWallet() {
    var prov = window.solana;
    if (!prov || !prov.isPhantom) {
      dlConnect('link');
      return;
    }
    if (!player) { openName(linkWallet); return; }
    var pin = askPin();
    if (!pin) return;
    prov.connect().then(function (res) {
      var addr = res.publicKey.toString();
      var msg = 'Weekend Coupon\nLink this wallet to player: ' + player + '\n' +
                addr + '\n' + new Date().toISOString();
      return prov.signMessage(new TextEncoder().encode(msg), 'utf8').then(function (sig) {
        if (!sb) return;
        sb.rpc('coupon_link_wallet', {
          p_name: player, p_pin: pin, p_wallet: addr,
          p_sig: b64(sig.signature), p_msg: msg
        }).then(function (r) { linkDone(r, addr); });
      });
    }).catch(function (e) { console.warn('wallet connect cancelled', e); });
  }

  /* ---- payout sheet (key-gated) ---- */
  function sha256Hex(str) {
    return crypto.subtle.digest('SHA-256', new TextEncoder().encode(str)).then(function (buf) {
      return Array.prototype.map.call(new Uint8Array(buf),
        function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
    });
  }
  function openPayout() {
    var key;
    try { key = localStorage.getItem('coupon_admin_key'); } catch (e) {}
    if (!key) key = prompt('Payout key');
    if (!key) return;
    sha256Hex(key).then(function (h) {
      if (h !== cfg.adminHash) { alert('Wrong key.'); return; }
      try { localStorage.setItem('coupon_admin_key', key); } catch (e) {}
      buildPayout();
    });
  }
  function buildPayout() {
    var modal = document.getElementById('pay-modal');
    modal.hidden = false;
    var tbody = document.getElementById('pay-body');
    tbody.innerHTML = '<tr><td colspan="5">Loading&hellip;</td></tr>';
    var per = cfg.ssb ? cfg.ssb.perPoint : 1;
    var season = (cfg.standings && cfg.standings.season) || [];
    var q = sb ? sb.from('coupon_wallets').select('player,wallet,created_at')
                   .order('created_at', { ascending: false })
               : Promise.resolve({ data: [] });
    Promise.resolve(q).then(function (r) {
      var wmap = {};
      ((r && r.data) || []).forEach(function (w) { if (!wmap[w.player]) wmap[w.player] = w.wallet; });
      var out = season.map(function (p) {
        return { name: p.name, wallet: wmap[p.name] || '', season: p.pts,
                 ssb: p.pts * per };
      });
      var head = '<tr><th>Player</th><th>Wallet</th><th>Season pts</th><th>' +
                 (cfg.ssb ? cfg.ssb.ticker : 'Tokens') + '</th></tr>';
      tbody.innerHTML = head + (out.length ? out.map(function (o) {
        return '<tr><td>' + o.name + '</td><td class="pay-w">' +
               (o.wallet || '<em>no wallet</em>') + '</td><td>' + o.season +
               '</td><td>' + o.ssb.toLocaleString() + '</td></tr>';
      }).join('') : '<tr><td colspan="4">No settled predictions yet.</td></tr>');
      document.getElementById('pay-json').value = JSON.stringify(out, null, 2);
    });
  }

  /* username modal — create account / sign in / recover, all PIN-based */
  var nameModal = document.getElementById('name-modal');
  var sessionPin = null;                 // the PIN for this session (never stored)
  var pendingSignup = null;

  function norm(s) { return (s || '').trim().replace(/\s+/g, ' ').slice(0, 20); }
  function isPin(s) { return /^[0-9]{4,6}$/.test((s || '').trim()); }
  function nv(view) {
    ['new', 'signin', 'recover', 'code', 'changepin', 'getcode'].forEach(function (k) {
      var el = nameModal.querySelector('.nv-' + k);
      if (el) el.hidden = (k !== view);
    });
    nameModal.querySelectorAll('.pin-eye').forEach(function (b) {
      var inp = document.getElementById(b.dataset.for);
      if (inp) inp.type = 'password';
      b.textContent = 'show';
    });
  }
  function openNameView(view, focusId) {
    nameModal.hidden = false;
    nameModal._after = null;
    nv(view);
    ['cp-old', 'cp-new', 'gc-pin'].forEach(function (id) {
      var el = document.getElementById(id); if (el) el.value = '';
    });
    ['cp-err', 'gc-err'].forEach(function (id) {
      var el = document.getElementById(id); if (el) el.textContent = '';
    });
    var f = document.getElementById(focusId);
    setTimeout(function () { if (f) f.focus(); }, 30);
  }
  function openName(after) {
    nameModal.hidden = false;
    nameModal._after = after || null;
    nv('new');
    ['name-input', 'name-pin', 'si-name', 'si-pin', 'rc-name', 'rc-code', 'rc-pin'].forEach(function (id) {
      var el = document.getElementById(id); if (el) el.value = '';
    });
    ['name-err', 'si-err', 'rc-err'].forEach(function (id) {
      var el = document.getElementById(id); if (el) el.textContent = '';
    });
    var i = document.getElementById('name-input');
    setTimeout(function () { if (i) i.focus(); }, 30);
  }

  function askPin(msg) {
    if (sessionPin) return sessionPin;
    var p = prompt(msg || ('Enter the PIN for "' + player + '" to link this wallet:'));
    if (p == null) return null;
    sessionPin = p.trim();
    return sessionPin;
  }

  function createAccount() {
    var raw = norm(document.getElementById('name-input').value);
    var pin = (document.getElementById('name-pin').value || '').trim();
    var errEl = document.getElementById('name-err');
    errEl.textContent = '';
    if (raw.length < 2) { errEl.textContent = 'Pick a name of at least 2 characters.'; return; }
    if (!isPin(pin)) { errEl.textContent = 'PIN must be 4 to 6 digits.'; return; }
    if (!sb) { sessionPin = pin; setPlayer(raw); return; }
    sb.rpc('coupon_signup', { p_name: raw, p_pin: pin }).then(function (r) {
      if (r.error) {
        var m = String((r.error && r.error.message) || '');
        errEl.textContent = m.indexOf('taken') > -1
          ? '"' + raw + '" is taken. If it\'s yours, use "Sign in with username + PIN".'
          : m.indexOf('bad_pin') > -1 ? 'PIN must be 4 to 6 digits.'
          : m.indexOf('bad_name') > -1 ? 'That name will not work — try another.'
          : 'Could not create the account. Try again in a moment.';
        return;
      }
      sessionPin = pin;
      pendingSignup = raw;
      var cv = document.getElementById('rc-value');
      if (cv) cv.textContent = r.data;
      nv('code');
    });
  }

  function signInWithPin() {
    var raw = norm(document.getElementById('si-name').value);
    var pin = (document.getElementById('si-pin').value || '').trim();
    var errEl = document.getElementById('si-err');
    errEl.textContent = '';
    if (raw.length < 2 || !pin) { errEl.textContent = 'Enter your username and PIN.'; return; }
    if (!sb) { sessionPin = pin; setPlayer(raw); return; }
    sb.rpc('coupon_signin', { p_name: raw, p_pin: pin }).then(function (r) {
      if (r.error || !r.data || !r.data.ok) {
        errEl.textContent = (r.data && r.data.reason === 'locked')
          ? 'Too many wrong tries. Wait 15 minutes, then try again.'
          : 'That username or PIN is not right.';
        return;
      }
      sessionPin = pin;
      setPlayer(raw);
    });
  }

  function recoverPin() {
    var raw = norm(document.getElementById('rc-name').value);
    var code = (document.getElementById('rc-code').value || '').trim();
    var pin = (document.getElementById('rc-pin').value || '').trim();
    var errEl = document.getElementById('rc-err');
    errEl.textContent = '';
    if (raw.length < 2 || !code || !isPin(pin)) {
      errEl.textContent = 'Fill all three in — the new PIN must be 4 to 6 digits.'; return;
    }
    if (!sb) return;
    sb.rpc('coupon_recover', { p_name: raw, p_code: code, p_new_pin: pin }).then(function (r) {
      if (r.error || !r.data || !r.data.ok) {
        errEl.textContent = (r.data && r.data.reason === 'locked')
          ? 'Too many wrong tries. Wait 15 minutes.'
          : 'That recovery code does not match that username.';
        return;
      }
      sessionPin = pin;
      setPlayer(raw);
    });
  }

  function changePin() {
    if (!player || !sb) return;
    openNameView('changepin', 'cp-old');
  }
  function doChangePin() {
    var oldp = (document.getElementById('cp-old').value || '').trim();
    var newp = (document.getElementById('cp-new').value || '').trim();
    var errEl = document.getElementById('cp-err');
    errEl.textContent = '';
    if (!oldp || !isPin(newp)) {
      errEl.textContent = 'Enter your current PIN and a new 4 to 6 digit PIN.'; return;
    }
    sb.rpc('coupon_set_pin', { p_name: player, p_old_pin: oldp, p_new_pin: newp })
      .then(function (r) {
        if (!r.error && r.data && r.data.ok) {
          sessionPin = newp;
          nameModal.hidden = true;
          alert('PIN changed.');
        } else {
          errEl.textContent = (r.data && r.data.reason === 'locked')
            ? 'Locked — too many wrong tries. Wait 15 minutes.'
            : 'That current PIN is not right.';
        }
      });
  }

  function newRecoveryCode() {
    if (!player || !sb) return;
    openNameView('getcode', 'gc-pin');
  }
  function doGetCode() {
    var pin = (document.getElementById('gc-pin').value || '').trim();
    var errEl = document.getElementById('gc-err');
    errEl.textContent = '';
    if (!isPin(pin)) { errEl.textContent = 'Enter your PIN (4 to 6 digits).'; return; }
    sb.rpc('coupon_new_recovery', { p_name: player, p_pin: pin }).then(function (r) {
      if (r.error) {
        var m = String((r.error && r.error.message) || '');
        errEl.textContent = m.indexOf('locked') > -1 ? 'Too many wrong tries. Wait 15 minutes.'
          : m.indexOf('bad') > -1 ? 'That PIN is not right.'
          : 'Could not make a code. Try again in a moment.';
        return;
      }
      sessionPin = pin;
      pendingSignup = null;
      var cv = document.getElementById('rc-value');
      if (cv) cv.textContent = r.data;
      nv('code');
    });
  }

  function setPlayer(name) {
    player = name;
    try { localStorage.setItem(LS_NAME, name); } catch (e) {}
    nameModal.hidden = true;
    pendingSignup = null;
    myPicks = {};
    saveLocal();
    renderPicks();
    renderMine();
    var f = nameModal._after; nameModal._after = null;
    loadRemote().then(function () { renderPicks(); renderMine(); });
    if (f) f();
  }

  function wire(id, ev, fn) {
    var el = document.getElementById(id);
    if (el) el.addEventListener(ev, fn);
  }
  function enterKey(id, fn) {
    wire(id, 'keydown', function (e) { if (e.key === 'Enter') fn(); });
  }
  wire('name-go', 'click', createAccount);
  enterKey('name-input', createAccount); enterKey('name-pin', createAccount);
  wire('si-go', 'click', signInWithPin);
  enterKey('si-name', signInWithPin); enterKey('si-pin', signInWithPin);
  wire('rc-go', 'click', recoverPin);
  enterKey('rc-pin', recoverPin);
  wire('cp-go', 'click', doChangePin);
  enterKey('cp-old', doChangePin); enterKey('cp-new', doChangePin);
  wire('gc-go', 'click', doGetCode);
  enterKey('gc-pin', doGetCode);
  wire('cp-cancel', 'click', function () { nameModal.hidden = true; });
  wire('gc-cancel', 'click', function () { nameModal.hidden = true; });
  wire('to-signin', 'click', function () { nv('signin'); var e = document.getElementById('si-name'); if (e) e.focus(); });
  wire('to-new', 'click', function () { nv('new'); });
  wire('to-recover', 'click', function () { nv('recover'); });
  wire('rc-back', 'click', function () { nv('signin'); });
  nameModal.querySelectorAll('.pin-eye').forEach(function (b) {
    b.addEventListener('click', function () {
      var inp = document.getElementById(b.dataset.for);
      if (!inp) return;
      var hidden = inp.type === 'password';
      inp.type = hidden ? 'text' : 'password';
      b.textContent = hidden ? 'hide' : 'show';
    });
  });
  wire('rc-done', 'click', function () {
    if (pendingSignup) setPlayer(pendingSignup);
    else nameModal.hidden = true;
  });
  wire('rc-copy', 'click', function () {
    var v = (document.getElementById('rc-value') || {}).textContent || '';
    var b = document.getElementById('rc-copy');
    (navigator.clipboard ? navigator.clipboard.writeText(v) : Promise.reject())
      .then(function () { b.textContent = 'copied'; setTimeout(function () { b.textContent = 'copy'; }, 1500); })
      .catch(function () {});
  });
  var nameSignin = document.getElementById('name-signin');
  if (nameSignin) nameSignin.addEventListener('click', signInWithWallet);

  /* leaderboard modal */
  function openLB() { document.getElementById('lb-modal').hidden = false; showLB('weekend'); }
  function showLB(which) {
    var s = (cfg.standings && cfg.standings[which]) || [];
    var per = cfg.ssb ? cfg.ssb.perPoint : 1;
    var tk = cfg.ssb ? cfg.ssb.ticker : 'Tokens';
    var head = '<tr><th>#</th><th>Player</th><th>Pts</th><th>' + tk + '</th></tr>';
    var body = s.length ? s.map(function (r, i) {
      return '<tr class="' + (r.name === player ? 'me' : '') + '"><td>' + (i + 1) +
             '</td><td><a href="#" class="lb-name" data-name="' + r.name + '">' + r.name +
             '</a></td><td>' + r.pts + '</td><td>' +
             (r.pts * per).toLocaleString() + '</td></tr>';
    }).join('') : '<tr><td colspan="4" class="lb-empty">No settled predictions yet.</td></tr>';
    document.getElementById('lb-body').innerHTML = head + body;
    document.querySelectorAll('.lb-tab').forEach(function (t) {
      t.classList.toggle('on', t.dataset.tab === which);
    });
    document.querySelectorAll('.lb-name').forEach(function (a) {
      a.addEventListener('click', function (e) {
        e.preventDefault();
        document.getElementById('lb-modal').hidden = true;
        openHistory(a.dataset.name);
      });
    });
  }
  document.querySelectorAll('.lb-tab').forEach(function (t) {
    t.addEventListener('click', function () { showLB(t.dataset.tab); });
  });

  function pickCorrect(mk, sel, res) {
    if (mk === 'goals_ou')   return sel === 'over' ? res.goals   > res.lines.goals   : res.goals   < res.lines.goals;
    if (mk === 'corners_ou') return sel === 'over' ? res.corners > res.lines.corners : res.corners < res.lines.corners;
    if (mk === 'cards_ou')   return sel === 'over' ? res.cards   > res.lines.cards   : res.cards   < res.lines.cards;
    if (mk === 'btts')       return sel === 'yes'  ? res.btts : !res.btts;
    if (mk === 'result')     { var d = res.h_goals - res.a_goals; return sel==='home'?d>0:sel==='draw'?d===0:d<0; }
    return false;
  }
  function fridayOf(iso) {
    var d = new Date(iso);
    var diff = (d.getUTCDay() - 5 + 7) % 7;   // days since the most recent Friday
    var fri = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() - diff));
    return fri.toISOString().slice(0, 10);
  }
  function openHistory(name) {
    document.getElementById('history-modal').hidden = false;
    var input = document.getElementById('hist-name');
    var target = (name || player || '').trim();
    if (input) input.value = target;
    loadHistoryFor(target);
  }
  function loadHistoryFor(name) {
    var body = document.getElementById('history-body');
    if (!name) { body.innerHTML = '<p class="hint-text">Type a player name and hit Search.</p>'; return; }
    if (!sb) { body.innerHTML = '<p class="hint-text">Could not connect.</p>'; return; }
    body.innerHTML = '<p class="hint-text">Loading…</p>';
    sb.from('coupon_picks').select('fixture_id,market,selection,created_at')
      .eq('player', name).order('created_at', { ascending: true })
      .then(function (r) {
        if (!r || r.error || !r.data) { body.innerHTML = '<p class="hint-text">Could not load history.</p>'; return; }
        if (!r.data.length) { body.innerHTML = '<p class="hint-text">No predictions found for that name.</p>'; return; }
        renderHistory(r.data);
      });
  }
  function renderHistory(picks) {
    var body = document.getElementById('history-body');
    var latest = {};
    picks.forEach(function (p) { latest[p.fixture_id + '|' + p.market] = p; });
    var byWeek = {};
    Object.keys(latest).forEach(function (key) {
      var p = latest[key];
      var info = cfg.fixtureInfo && cfg.fixtureInfo[p.fixture_id];
      if (!info) return;
      var wk = fridayOf(info.date);
      var wkList = byWeek[wk] = byWeek[wk] || {};
      var fx = wkList[p.fixture_id] = wkList[p.fixture_id] || { info: info, picks: [] };
      fx.picks.push(p);
    });
    var weekKeys = Object.keys(byWeek).sort().reverse();
    if (!weekKeys.length) {
      body.innerHTML = '<p class="hint-text">No predictions made yet.</p>';
      return;
    }
    body.innerHTML = weekKeys.map(function (wk, i) {
      var fxMap = byWeek[wk];
      var won = 0, total = 0;
      var cards = Object.keys(fxMap).map(function (fid) {
        var g = fxMap[fid], info = g.info;
        var res = cfg.results && cfg.results[fid];
        var lines = g.picks.map(function (p) {
          var m = marketBy(p.market);
          if (!m) return '';
          var ln = m.metric && info.lines ? ' ' + info.lines[m.metric] : '';
          var label = '<b>' + m.label + '</b> <span class="rzl-p">' + optLabel(m, p.selection) + ln + '</span>';
          if (!res) return '<span class="rzl rzl--na">' + label + ' &rarr; pending</span>';
          total++;
          var ok = pickCorrect(p.market, p.selection, res);
          if (ok) won++;
          return '<span class="rzl rzl--' + (ok ? 'hit' : 'miss') + '">' + label + '</span>';
        }).join('');
        var score = res ? (res.h_goals + '&#8211;' + res.a_goals) : 'vs';
        return '<div class="rz"><div class="rz-score"><b>' + info.home + '</b> ' + score +
               ' <b>' + info.away + '</b></div><div class="rz-lines">' + lines + '</div></div>';
      }).join('');
      return '<details class="wk"' + (i === 0 ? ' open' : '') + '>' +
        '<summary><span class="wk-span">w/o ' + wk + '</span>' +
        '<span class="wk-rate">' + won + '/' + total + ' landed</span></summary>' +
        '<div class="wk-body">' + cards + '</div></details>';
    }).join('');
  }
  var histGo = document.getElementById('hist-go');
  var histNameInput = document.getElementById('hist-name');
  if (histGo) histGo.addEventListener('click', function () {
    loadHistoryFor(histNameInput.value.trim());
  });
  if (histNameInput) histNameInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); loadHistoryFor(histNameInput.value.trim()); }
  });

  function closeModal(m) {
    m.hidden = true;
    if (m === nameModal && pendingSignup && !player) setPlayer(pendingSignup);
  }
  document.querySelectorAll('[data-close]').forEach(function (x) {
    x.addEventListener('click', function () { closeModal(x.closest('.modal')); });
  });
  document.querySelectorAll('.modal').forEach(function (m) {
    m.addEventListener('click', function (e) { if (e.target === m) closeModal(m); });
  });

  function loadRemote() {
    if (!sb || !player) return Promise.resolve();
    var ids = cfg.fixtures.map(function (f) { return f.id; });
    var p1 = sb.from('coupon_picks').select('fixture_id,market,selection,created_at')
      .eq('player', player).in('fixture_id', ids)
      .order('created_at', { ascending: true })
      .then(function (r) {
        if (!r || r.error || !r.data) return;
        r.data.forEach(function (p) {
          (myPicks[p.fixture_id] = myPicks[p.fixture_id] || {})[p.market] = p.selection;
        });
        saveLocal();
      });
    var p2 = sb.from('coupon_wallets').select('wallet,created_at')
      .eq('player', player).order('created_at', { ascending: false }).limit(1)
      .then(function (r) {
        if (r && r.data && r.data[0]) {
          wallet = r.data[0].wallet;
          try { localStorage.setItem(LS_WALLET, wallet); } catch (e) {}
        }
      });
    return Promise.all([p1, p2]);
  }

  function verifyPlayer() {
    if (!sb || !player) return Promise.resolve();
    return sb.from('coupon_players').select('name').eq('name', player).limit(1)
      .then(function (r) {
        if (r && !r.error && r.data && r.data.length === 0) {
          // stored account no longer exists (e.g. a deleted test acct) — sign out
          try {
            localStorage.removeItem(LS_NAME);
            localStorage.removeItem(LS_WALLET);
            localStorage.removeItem(wkKey());
          } catch (e) {}
          player = null; wallet = null; myPicks = {};
        }
      });
  }

  loadLocal();
  renderPicks();
  renderMine();
  handlePhantomReturn();
  verifyPlayer().then(function () {
    renderPicks();
    renderMine();
    return loadRemote();
  }).then(function () { renderPicks(); renderMine(); });
})();
"""


def render(rows, d1, d2, generated, results=None, built_iso="", report=None, cfg=None,
           ucl=None, page="pl", history=None):
    results_html = result_band(results)
    season_html = season_band(report)
    ucl_html = ucl_band(ucl or [])
    history_html = history_band(history) if history else ""

    pl_on = " on" if page == "pl" else ""
    ucl_on = " on" if page == "ucl" else ""
    flip_html = (
        '<nav class="pgnav">'
        f'<a class="pgnav-t{pl_on}" href="/">Premier League</a>'
        f'<a class="pgnav-t{ucl_on}" href="/champions-league">Champions League</a>'
        '</nav>'
    )
    cfg_json = json.dumps(cfg or {}).replace("<", "\\u003c").replace("</", "<\\/")

    callouts = ""
    if rows:
        best = {
            "goals": max(rows, key=lambda r: r["x"]["goals"]),
            "corners": max(rows, key=lambda r: r["x"]["corners"]),
            "cards": max(rows, key=lambda r: r["x"]["cards"]),
        }
        best_labels = {"goals": "MOST GOALS", "corners": "MOST CORNERS", "cards": "MOST CARDS"}

        def callout_bet(row, metric):
            for leg in row["legs"]:
                m = leg["check"][0]
                if m == metric or (metric == "goals" and m in ("goals", "goals_u")):
                    return ("under" if m == "goals_u" else "over"), f"{leg['check'][1]:g}"
            return "over", f"{half(row['x'][metric]):g}"

        callout_cells = []
        for k in ("goals", "corners", "cards"):
            direction, line = callout_bet(best[k], k)
            callout_cells.append(f"""
        <a class="callout callout--{k}" href="#fx-{best[k]['fx']['id']}">
          <span class="callout-head">
            <span class="callout-tag">{best_labels[k]}</span>
            <span class="callout-badge">Top call</span>
          </span>
          <span class="callout-fx">{best[k]['fx']['home_abbr']} v {best[k]['fx']['away_abbr']}</span>
          <span class="callout-val"><em>{direction}</em> {line}</span>
        </a>""")
        callouts = "".join(callout_cells)

    lines = []
    for i, r in enumerate(rows):
        f, x = r["fx"], r["x"]
        h, a = r["h"], r["a"]
        promo = ""
        if h["promoted"] or a["promoted"]:
            promo = ('<p class="flag-promo">Promoted side involved — thin top-flight '
                     'data, lower confidence.</p>')
        red = ('<p class="flag-red">Red-card watch: both sides among the most-carded '
               'last season.</p>') if x["red_risk"] else ""
        legs = "".join(
            f'<li class="leg leg--{L["cat"]}"><i></i>{L["text"]}</li>' for L in r["legs"]
        )
        result_pick = {"home": f"{f['home']} to win", "away": f"{f['away']} to win",
                       "draw": "Draw"}[x["result"]]
        btts_pick = "Yes" if x["btts"] else "No"
        wc_pick = (f'<p class="wc-pick"><b>Weekend Coupon\'s pick</b> &mdash; '
                   f'Result: <span>{result_pick}</span> &middot; '
                   f'BTTS: <span class="wc-btts">{btts_pick}</span></p>')
        top = " line--top" if i == 0 else ""
        badge = '<span class="top-badge">TOP CALL</span>' if i == 0 else ""
        lines.append(f"""
      <article class="line{top}" id="fx-{f['id']}" data-ko="{f['date']}">
        <div class="gutter">
          <span class="rank">{i + 1:02d}</span>
          <span class="juice" title="Model juice score, 0-100">{r['score']}</span>
        </div>
        <div class="body">
          <div class="fixture">
            <h2>{f['home']}<span class="v">v</span>{f['away']}</h2>
            <div class="ko">{kickoff(f['date'])}{badge}</div>
          </div>
          {promo}{red}
          <div class="meters">
            {meter("goals", "Goals", x['goals'], 1.8, 4.2, [2.5, 3.5])}
            {meter("corners", "Corners", x['corners'], 8.0, 13.0, [9.5, 10.5, 11.5])}
            {meter("cards", "Cards", x['cards'], 2.4, 5.4, [3.5, 4.5])}
          </div>
          <div class="split">
            <div><b>{f['home_abbr']}</b> {x['h_goals']:g} xG&nbsp;·&nbsp;{h['corners_pg']:g} cnr&nbsp;·&nbsp;{h['yc_pg']:.2f} yc</div>
            <div><b>{f['away_abbr']}</b> {x['a_goals']:g} xG&nbsp;·&nbsp;{a['corners_pg']:g} cnr&nbsp;·&nbsp;{a['yc_pg']:.2f} yc</div>
          </div>
          <div class="builder">
            <h3>This week's predictions</h3>
            <ul>{legs}</ul>
          </div>
          {wc_pick}
        </div>
      </article>""")

    if rows:
        fdates = sorted(r["fx"]["date"] for r in rows)
        span = match_day(fdates[0])
        if match_day(fdates[-1]) != span:
            span += f" &#8211; {match_day(fdates[-1])}"
        span += f" {fdates[-1][:4]}"
        issue = (f'<span>Premier League</span><span><b>{span}</b></span>'
                 f'<span>Built {generated}</span><span>Ranked by juice score</span>')
        intro = (
            '<p class="intro">Games run <b>1 to 10, strongest first</b>. #1 is where '
            'the model is most confident the goals, corners and cards will land; #10 is '
            'the one to be wary of. The number next to each game (0&#8211;100) is that '
            'confidence. A guide, not a guarantee.</p>')
        coupon_html = (f'<nav class="callouts" aria-label="Standout fixtures">{callouts}</nav>'
                       f'<div class="sortbar"><button type="button" id="sort-toggle" '
                       f'data-mode="juice">Order: strongest first</button></div>'
                       f'<main>{"".join(lines)}</main>')
    else:
        issue = (f'<span>Premier League</span><span>Built {generated}</span>'
                 f'<span>No fixtures &mdash; international break</span>')
        intro = ""
        coupon_html = ""

    if page == "ucl":
        issue = (f'<span>Champions League</span><span><b>Midweek</b></span>'
                 f'<span>Built {generated}</span><span>Ranked by juice score</span>')
        intro = ""
        coupon_html = ""
        results_html = season_html = ""
        if not ucl_html:
            ucl_html = ('<section class="ucl"><p class="ucl-key">No Champions League '
                        'games in the next week. This page fills in when the midweek '
                        'fixtures come round &mdash; the '
                        '<a href="/">Premier League sheet</a> is live as always.</p></section>')

    return f"""<meta charset="utf-8">
<title>Weekend Coupon</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=Barlow:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
  :root {{
    --paper:#ece4d3; --card:#f6f1e6; --ink:#221f18; --muted:#726a58;
    --rule:#d3c8ae; --rule-soft:#e1d8c2;
    --goals:#1f7a44; --corners:#2f6f9e; --cards:#c98a08; --hot:#bf3b2b;
    --btts:#7a4fb0; --result:#1c8f82;
    --warn:#b0521c; --warn-bg:#f1e0cd;
    --shadow:0 1px 0 rgba(0,0,0,.04);
  }}
  @media (prefers-color-scheme:dark) {{
    :root:not([data-theme="light"]) {{
      --paper:#15170f; --card:#1d1f16; --ink:#e8e3d5; --muted:#8f8873;
      --rule:#343625; --rule-soft:#282a1c;
      --goals:#4cae74; --corners:#5aa0cf; --cards:#e0a72c; --hot:#e0614f;
      --btts:#b98ee0; --result:#4cc9ba;
      --warn:#e0863f; --warn-bg:#2c2113;
      --shadow:0 1px 0 rgba(0,0,0,.3);
    }}
  }}
  :root[data-theme="dark"] {{
    --paper:#15170f; --card:#1d1f16; --ink:#e8e3d5; --muted:#8f8873;
    --rule:#343625; --rule-soft:#282a1c;
    --goals:#4cae74; --corners:#5aa0cf; --cards:#e0a72c; --hot:#e0614f;
    --btts:#b98ee0; --result:#4cc9ba;
    --warn:#e0863f; --warn-bg:#2c2113;
    --shadow:0 1px 0 rgba(0,0,0,.3);
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{
    background:var(--paper); color:var(--ink);
    font:16px/1.55 Barlow,-apple-system,Segoe UI,Roboto,sans-serif;
    padding:32px 18px 72px; -webkit-font-smoothing:antialiased;
  }}
  .sheet {{ max-width:760px; margin:0 auto; }}

  .masthead {{ border-bottom:3px double var(--ink); padding-bottom:14px; margin-bottom:20px; }}
  .masthead h1 {{
    font-family:"Barlow Condensed",sans-serif; font-weight:700;
    font-size:clamp(30px,7vw,46px); line-height:.98; letter-spacing:.01em;
    text-transform:uppercase; text-wrap:balance;
  }}
  .tagline {{
    font-family:"Barlow",sans-serif; font-size:14px; font-weight:500;
    color:var(--warn); margin-top:4px;
  }}
  .issue {{
    display:flex; flex-wrap:wrap; gap:6px 16px; margin-top:8px;
    font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.04em;
  }}
  .issue b {{ color:var(--ink); font-weight:600; }}
  .pgnav {{ display:flex; gap:6px; margin:0 0 20px; }}
  .pgnav:last-of-type {{ margin:20px 0 0; }}
  .pgnav-t {{
    font-family:"IBM Plex Mono",monospace; font-size:11px; font-weight:600;
    text-transform:uppercase; letter-spacing:.05em; text-decoration:none;
    padding:6px 12px; border:1px solid var(--warn); color:var(--warn);
  }}
  .pgnav-t:hover {{ background:var(--warn-bg); }}
  .pgnav-t.on {{ background:var(--warn); color:#fff; border-color:var(--warn); }}
  .intro {{
    font-size:12.5px; line-height:1.6; color:var(--muted); margin:-6px 0 22px;
    max-width:64ch;
  }}
  .intro b {{ color:var(--ink); font-weight:600; }}
  .sortbar {{ display:flex; justify-content:flex-end; margin-bottom:2px; }}
  #sort-toggle {{
    font-family:"IBM Plex Mono",monospace; font-size:10px; font-weight:600;
    text-transform:uppercase; letter-spacing:.05em; cursor:pointer;
    background:var(--card); color:var(--ink); border:1px solid var(--rule); padding:5px 10px;
  }}
  #sort-toggle:hover {{ border-color:var(--ink); }}
  #sort-toggle::before {{ content:"\\21c5  "; }}

  .fresh-note {{
    font-family:"IBM Plex Mono",monospace; font-size:10px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.04em; line-height:1.55; margin-bottom:8px;
  }}
  .freshbar {{
    position:sticky; top:0; z-index:10;
    display:flex; justify-content:space-between; align-items:center; gap:12px;
    background:var(--paper); border-bottom:1px solid var(--rule);
    padding:8px 0; margin-bottom:16px;
    font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.04em;
  }}
  .freshbar-btn {{
    font:inherit; text-transform:uppercase; letter-spacing:.04em;
    color:var(--ink); background:var(--card);
    border:1px solid var(--rule); padding:4px 10px; cursor:pointer;
  }}
  .freshbar-btn:hover {{ border-color:var(--ink); }}
  .freshbar-btn:focus-visible {{ outline:2px solid var(--hot); outline-offset:2px; }}
  .freshbar--stale {{
    background:var(--warn-bg); border-bottom-color:var(--warn); color:var(--warn);
  }}
  .freshbar--stale .freshbar-btn {{
    background:var(--warn); border-color:var(--warn); color:#fff; font-weight:600;
  }}

  .results {{ margin-bottom:28px; }}
  .results-head {{
    display:flex; justify-content:space-between; align-items:baseline;
    border-bottom:1px solid var(--rule); padding-bottom:6px; margin-bottom:10px;
  }}
  .results-head h2 {{
    font-family:"Barlow Condensed",sans-serif; font-weight:700; font-size:16px;
    text-transform:uppercase; letter-spacing:.03em;
  }}
  .results-rate {{
    font-family:"IBM Plex Mono",monospace; font-size:12px; font-weight:600;
    color:var(--muted); font-variant-numeric:tabular-nums;
  }}
  .results-key {{
    font-size:12px; color:var(--muted); margin-bottom:12px; line-height:1.5;
  }}
  .results-key b {{ color:var(--ink); font-weight:600; }}
  .results-key span {{ font-family:"IBM Plex Mono",monospace; font-size:10.5px; }}

  .season {{ margin-bottom:28px; }}
  .season-head {{
    display:flex; justify-content:space-between; align-items:baseline;
    border-bottom:1px solid var(--rule); padding-bottom:6px; margin-bottom:10px;
  }}
  .season-head h2 {{
    font-family:"Barlow Condensed",sans-serif; font-weight:700; font-size:16px;
    text-transform:uppercase; letter-spacing:.03em;
  }}
  .season-pct {{
    font-family:"IBM Plex Mono",monospace; font-size:20px; font-weight:600;
    color:var(--ink); font-variant-numeric:tabular-nums;
  }}
  .sbar {{ height:8px; background:var(--rule-soft); overflow:hidden; }}
  .sbar span {{ display:block; height:100%; width:var(--w); background:var(--muted); }}
  .sbar--all {{ height:12px; }}
  .sbar--all span {{ background:var(--goals); }}
  .season-sub {{ font-size:11.5px; color:var(--muted); margin:8px 0 12px; line-height:1.5; }}
  .season-metrics {{ display:flex; flex-direction:column; gap:6px; margin-bottom:14px; }}
  .smetric {{ display:grid; grid-template-columns:58px 1fr 40px; align-items:center; gap:9px; }}
  .smetric b {{
    font-family:"Barlow Condensed",sans-serif; font-weight:600; font-size:11px;
    text-transform:uppercase; letter-spacing:.03em; color:var(--muted);
  }}
  .smetric i {{
    font-family:"IBM Plex Mono",monospace; font-style:normal; font-size:11px;
    text-align:right; color:var(--ink); font-variant-numeric:tabular-nums;
  }}
  .smetric:nth-child(1) .sbar span {{ background:var(--goals); }}
  .smetric:nth-child(2) .sbar span {{ background:var(--corners); }}
  .smetric:nth-child(3) .sbar span {{ background:var(--cards); }}
  .smetric:nth-child(4) .sbar span {{ background:var(--btts); }}
  .smetric:nth-child(5) .sbar span {{ background:var(--result); }}
  .season-weeks {{ display:flex; gap:4px; align-items:flex-end; }}
  .swk {{
    flex:1; min-width:0; display:flex; flex-direction:column; align-items:center; gap:4px;
  }}
  .swk-bar {{
    width:100%; height:34px; background:var(--rule-soft); position:relative;
  }}
  .swk-bar::after {{
    content:""; position:absolute; left:0; right:0; bottom:0; height:var(--h);
    background:var(--ink); opacity:.55;
  }}
  .swk em {{
    font-family:"IBM Plex Mono",monospace; font-style:normal; font-size:8px;
    letter-spacing:.03em; color:var(--muted); white-space:nowrap;
  }}
  .rz {{ padding:8px 0; border-bottom:1px dotted var(--rule); }}
  .rz:last-child {{ border-bottom:0; }}
  .rz-score {{
    font-family:"Barlow Condensed",sans-serif; text-transform:uppercase;
    font-size:14px; letter-spacing:.02em; margin-bottom:5px;
    display:flex; flex-wrap:wrap; align-items:baseline; gap:8px;
  }}
  .rz-score b {{ font-weight:600; }}
  .rz-day {{
    font-family:"IBM Plex Mono",monospace; font-size:9.5px; font-weight:400;
    text-transform:none; letter-spacing:.03em; color:var(--muted);
    border:1px solid var(--rule); padding:1px 5px;
  }}
  .rz-tot {{
    font-family:"IBM Plex Mono",monospace; font-size:10.5px; font-weight:400;
    text-transform:none; letter-spacing:0; color:var(--muted);
  }}
  .rz-lines {{ display:flex; flex-wrap:wrap; gap:5px; }}
  .rzl {{
    font-family:"IBM Plex Mono",monospace; font-size:11px; line-height:1.35;
    padding:2px 7px; border:1px solid var(--rule); color:var(--ink);
    font-variant-numeric:tabular-nums; display:inline-flex; align-items:center; gap:5px;
  }}
  .rzl b {{ font-weight:600; color:var(--muted); text-transform:uppercase;
    font-size:9.5px; letter-spacing:.04em; }}
  .rzl-p {{ font-weight:600; color:var(--ink); }}
  .rzl::after {{ font-weight:700; }}
  .rzl--hit {{ border-color:var(--goals); }}
  .rzl--hit::after {{ content:"\\2713"; color:var(--goals); }}
  .rzl--miss {{ border-color:var(--hot); }}
  .rzl--miss::after {{ content:"\\2715"; color:var(--hot); }}
  .rzl--na {{ color:var(--muted); font-style:italic; }}
  .rzl--na .rzl-p {{ color:var(--muted); font-weight:400; }}
  .rzl-d {{ font-style:normal; color:var(--muted); font-size:9.5px; }}

  .history {{ margin-bottom:26px; }}
  .hist-btn {{
    font-family:"IBM Plex Mono",monospace; font-size:11px; font-weight:700;
    text-transform:uppercase; letter-spacing:.06em; cursor:pointer;
    background:#ab47bc; color:#fff; border:1px solid #ab47bc; padding:8px 16px;
  }}
  .hist-btn:hover {{ background:#9036a3; border-color:#9036a3; }}
  .hist-panel {{ margin-top:14px; }}
  .hist-panel[hidden] {{ display:none; }}
  .hist-panel h2 {{
    font-family:"Barlow Condensed",sans-serif; font-weight:700; font-size:18px;
    text-transform:uppercase; letter-spacing:.02em; margin-bottom:10px;
  }}
  details.wk {{
    background:var(--card); border:1px solid var(--rule); margin-bottom:8px;
  }}
  details.wk summary {{
    display:flex; justify-content:space-between; align-items:center; cursor:pointer;
    padding:10px 13px; list-style:none;
    font-family:"Barlow Condensed",sans-serif; font-weight:700;
    text-transform:uppercase; font-size:13px; letter-spacing:.02em;
  }}
  details.wk summary::-webkit-details-marker {{ display:none; }}
  details.wk summary::before {{ content:"+ "; color:var(--muted); }}
  details.wk[open] summary::before {{ content:"\\2212 "; }}
  .wk-rate {{ font-family:"IBM Plex Mono",monospace; font-size:10.5px; color:var(--muted); }}
  .wk-body {{ padding:0 13px 6px; }}
  .wk-body .rz {{ border-bottom:1px dotted var(--rule); }}
  .wk-body .rz:last-child {{ border-bottom:0; }}

  .callouts {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-bottom:26px; }}
  .callout {{
    display:flex; flex-direction:column; gap:3px; padding:11px 12px;
    background:var(--card); border:1px solid var(--rule); border-top:3px solid var(--muted);
    text-decoration:none; color:var(--ink); box-shadow:var(--shadow);
  }}
  .callout--goals {{ border-top-color:var(--goals); }}
  .callout--corners {{ border-top-color:var(--corners); }}
  .callout--cards {{ border-top-color:var(--cards); }}
  .callout-head {{ display:flex; justify-content:space-between; align-items:center; gap:6px; }}
  .callout-tag {{ font-family:"IBM Plex Mono",monospace; font-size:10px; letter-spacing:.09em; color:var(--muted); }}
  .callout-badge {{
    font-family:"IBM Plex Mono",monospace; font-size:8.5px; font-weight:600;
    text-transform:uppercase; letter-spacing:.08em; white-space:nowrap;
    color:var(--hot); border:1px solid var(--hot); padding:1px 5px;
  }}
  .callout-fx {{ font-family:"Barlow Condensed",sans-serif; font-weight:600; font-size:15px; line-height:1.1; text-transform:uppercase; }}
  .callout-val {{ font-family:"IBM Plex Mono",monospace; font-weight:600; font-size:19px; font-variant-numeric:tabular-nums; }}
  .callout-val em {{ font-style:normal; font-weight:600; font-size:10px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }}
  .callout:hover {{ border-color:var(--ink); }}
  .callout:focus-visible {{ outline:2px solid var(--hot); outline-offset:2px; }}

  .line {{
    display:grid; grid-template-columns:52px minmax(0, 1fr); gap:16px;
    padding:18px 0; border-top:1px solid var(--rule);
  }}
  .line > .body {{ min-width:0; }}
  .line:last-of-type {{ border-bottom:1px solid var(--rule); }}
  .line--top {{
    background:
      linear-gradient(var(--card),var(--card)) padding-box;
    border-top:2px solid var(--hot);
    margin:0 -14px; padding:18px 14px;
  }}
  .gutter {{ display:flex; flex-direction:column; align-items:flex-start; gap:8px; }}
  .rank {{
    font-family:"Barlow Condensed",sans-serif; font-weight:700; font-size:30px;
    line-height:1; color:var(--muted); font-variant-numeric:tabular-nums;
  }}
  .line--top .rank {{ color:var(--hot); }}
  .juice {{
    font-family:"IBM Plex Mono",monospace; font-weight:600; font-size:13px;
    padding:2px 7px; border:1px solid var(--rule); color:var(--ink);
    font-variant-numeric:tabular-nums;
  }}
  .line--top .juice {{ border-color:var(--hot); color:var(--hot); }}

  .fixture {{ display:flex; justify-content:space-between; align-items:baseline; gap:12px; flex-wrap:wrap; }}
  .fixture h2 {{
    font-family:"Barlow Condensed",sans-serif; font-weight:600;
    font-size:clamp(19px,4.4vw,25px); line-height:1.05; text-transform:uppercase;
    display:flex; align-items:baseline; gap:9px; flex-wrap:wrap;
  }}
  .fixture .v {{ color:var(--muted); font-weight:500; font-size:.7em; }}
  .ko {{
    font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--muted);
    letter-spacing:.05em; white-space:nowrap; display:flex; align-items:center; gap:8px;
  }}
  .top-badge {{
    font-weight:600; color:var(--hot); border:1px solid var(--hot);
    padding:1px 6px; letter-spacing:.08em;
  }}

  .flag-promo, .flag-red {{
    font-size:12.5px; margin-top:9px; padding-left:11px;
    border-left:2px solid var(--cards); color:var(--muted);
  }}
  .flag-red {{ border-left-color:var(--hot); }}

  .meters {{ margin:14px 0 4px; display:flex; flex-direction:column; gap:11px; }}
  .meter-head {{
    display:flex; justify-content:space-between; align-items:baseline;
    font-family:"Barlow Condensed",sans-serif; text-transform:uppercase;
    letter-spacing:.03em; font-size:13px; color:var(--muted); margin-bottom:5px;
  }}
  .meter-head b {{
    font-family:"IBM Plex Mono",monospace; font-size:16px; font-weight:600;
    color:var(--ink); font-variant-numeric:tabular-nums;
  }}
  .track {{ position:relative; height:6px; background:var(--rule-soft); }}
  .fill {{
    position:absolute; inset:0 auto 0 0; width:var(--w);
    animation:grow .7s cubic-bezier(.2,.7,.2,1) both;
  }}
  .meter--goals .fill {{ background:var(--goals); }}
  .meter--corners .fill {{ background:var(--corners); }}
  .meter--cards .fill {{ background:var(--cards); }}
  .tick {{ position:absolute; top:-3px; bottom:-3px; width:0; }}
  .tick i {{ position:absolute; inset:0; width:1px; background:var(--ink); opacity:.28; }}
  .tick em {{
    position:absolute; top:11px; left:0; transform:translateX(-50%);
    font-family:"IBM Plex Mono",monospace; font-style:normal; font-size:9.5px;
    color:var(--muted);
  }}
  @keyframes grow {{ from {{ width:0; }} }}
  @media (prefers-reduced-motion:reduce) {{ .fill {{ animation:none; }} }}

  .split {{
    display:flex; flex-wrap:wrap; gap:4px 22px; margin:22px 0 4px;
    font-family:"IBM Plex Mono",monospace; font-size:11.5px; color:var(--muted);
    font-variant-numeric:tabular-nums;
  }}
  .split b {{ color:var(--ink); font-weight:600; }}

  .builder {{ margin-top:12px; }}
  .builder h3 {{
    font-family:"Barlow Condensed",sans-serif; text-transform:uppercase;
    letter-spacing:.06em; font-size:12px; color:var(--muted); margin-bottom:7px;
  }}
  .builder ul {{ list-style:none; display:flex; flex-direction:column; gap:4px; }}
  .leg {{
    display:flex; align-items:flex-start; gap:9px; font-size:14px;
    padding:6px 10px; background:var(--card); border-left:3px solid var(--muted);
  }}
  .leg i {{
    flex:none; width:13px; height:13px; margin-top:3px; border:1.5px solid currentColor;
    position:relative;
  }}
  .leg i::after {{
    content:""; position:absolute; left:2px; top:-2px; width:5px; height:9px;
    border:solid currentColor; border-width:0 2px 2px 0; transform:rotate(40deg);
  }}
  .leg--goals {{ border-left-color:var(--goals); color:var(--goals); }}
  .leg--corners {{ border-left-color:var(--corners); color:var(--corners); }}
  .leg--cards {{ border-left-color:var(--cards); color:var(--cards); }}
  .leg {{ color:var(--ink); }}
  .leg--goals i {{ color:var(--goals); }}
  .leg--corners i {{ color:var(--corners); }}
  .leg--cards i {{ color:var(--cards); }}

  .wc-pick {{
    margin-top:9px; padding:7px 10px; background:var(--card);
    border-left:3px solid var(--result); font-size:12.5px; color:var(--muted);
  }}
  .wc-pick b {{
    font-family:"Barlow Condensed",sans-serif; text-transform:uppercase;
    letter-spacing:.04em; color:var(--ink); font-weight:600;
  }}
  .wc-pick span {{ color:var(--ink); font-weight:600; }}
  .wc-pick .wc-btts {{ color:var(--btts); }}

  .colophon {{
    margin-top:34px; padding-top:16px; border-top:3px double var(--ink);
    font-size:12.5px; color:var(--muted); line-height:1.65;
  }}
  .colophon b {{ color:var(--ink); }}
  .colophon a {{ color:var(--ink); }}

  .sponsors {{
    margin-top:20px; display:grid; gap:10px;
    grid-template-columns:repeat(3, 1fr);
  }}
  .sponsor {{
    display:flex; align-items:center; gap:10px; padding:10px 12px;
    background:var(--card); border:1px solid var(--rule);
    border-top:3px solid var(--muted);
    text-decoration:none; color:var(--ink); box-shadow:var(--shadow);
  }}
  .sponsor:hover {{ border-color:var(--ink); }}
  .sponsor:focus-visible {{ outline:2px solid var(--hot); outline-offset:2px; }}
  .sponsor img {{ border-radius:50%; flex:none; object-fit:cover; background:var(--paper); }}
  .sponsor span {{ display:flex; flex-direction:column; gap:1px; min-width:0; }}
  .sponsor em {{
    font-family:"IBM Plex Mono",monospace; font-style:normal; font-size:9px;
    text-transform:uppercase; letter-spacing:.08em; color:var(--muted);
  }}
  .sponsor b {{
    font-family:"Barlow Condensed",sans-serif; font-weight:600; font-size:15px;
    line-height:1.1; text-transform:uppercase;
  }}
  .sponsor i {{ font-style:normal; font-size:11px; color:var(--muted); }}
  @media (max-width:560px) {{ .sponsors {{ grid-template-columns:1fr; }} }}
  .tg-line {{
    margin-top:14px; text-align:center;
    font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.04em;
  }}
  .tg-line a {{ color:var(--ink); font-weight:600; }}

  /* ---- picks + leaderboard ---- */
  .mypicks {{
    margin-bottom:26px; padding:14px 16px;
    background:var(--card); border:1px solid var(--rule); border-top:3px solid var(--goals);
  }}
  .mp-h {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:2px; }}
  .mp-h-btns {{ display:flex; gap:6px; }}
  .mp-h b {{
    font-family:"Barlow Condensed",sans-serif; font-weight:700; font-size:16px;
    text-transform:uppercase; letter-spacing:.03em;
  }}
  #lb-open, .modal-go, .lb-tab {{
    font-family:"IBM Plex Mono",monospace; font-size:11px; font-weight:600;
    text-transform:uppercase; letter-spacing:.04em; cursor:pointer;
    background:var(--card); color:var(--ink); border:1px solid var(--rule); padding:5px 11px;
  }}
  #lb-open:hover, .lb-tab:hover {{ border-color:var(--ink); }}
  .mp-who {{
    font-family:"IBM Plex Mono",monospace; font-size:10.5px; color:var(--muted);
    letter-spacing:.03em; margin-bottom:9px;
    display:flex; align-items:center; gap:8px;
  }}
  .mp-empty {{ font-size:12.5px; color:var(--muted); }}
  .mp-list {{ list-style:none; display:flex; flex-direction:column; gap:5px; }}
  .mp-list li {{
    display:flex; align-items:center; gap:6px 8px; flex-wrap:wrap; font-size:12.5px;
    padding:5px 8px; background:var(--paper); border-left:3px solid var(--rule);
  }}
  .mp-fx {{
    font-family:"Barlow Condensed",sans-serif; font-weight:600; text-transform:uppercase;
    font-size:12px; letter-spacing:.02em; flex:none;
  }}
  .mp-sel {{ flex:1; min-width:120px; color:var(--muted); font-family:"IBM Plex Mono",monospace; font-size:11px; }}
  .pk-hit {{ color:var(--goals); font-style:normal; font-weight:700; }}
  .pk-miss {{ color:var(--hot); font-style:normal; font-weight:700; }}
  .pk-pend {{ color:var(--muted); font-style:normal; }}
  .mp-foot {{
    margin-top:9px; font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--muted);
  }}
  .mp-foot b {{ color:var(--ink); }}

  .picks {{ margin-top:14px; border-top:1px solid var(--rule); padding-top:11px; }}
  .picks-h {{
    font-family:"Barlow Condensed",sans-serif; text-transform:uppercase;
    letter-spacing:.06em; font-size:11px; color:var(--muted); margin-bottom:7px;
  }}
  .picks-h span {{ color:var(--hot); }}
  .pkrow {{ display:flex; align-items:center; gap:9px; margin-bottom:5px; min-width:0; }}
  .pkrow > b {{
    font-family:"IBM Plex Mono",monospace; font-size:9.5px; font-weight:600;
    text-transform:uppercase; letter-spacing:.02em; color:var(--muted);
    width:66px; flex:none;
  }}
  .pkbtns {{ display:flex; gap:4px; flex:1; min-width:0; }}
  .pk {{
    flex:1 1 0; min-width:0; font-family:"IBM Plex Mono",monospace; font-size:10.5px;
    font-weight:600; text-transform:uppercase; letter-spacing:.01em; cursor:pointer;
    background:var(--paper); color:var(--ink); border:1px solid var(--rule);
    padding:5px 2px; text-align:center; white-space:nowrap; overflow:hidden;
    text-overflow:ellipsis;
  }}
  .pk-note {{ font-size:11.5px; color:var(--muted); margin-top:2px; }}
  .pk:hover:not(:disabled) {{ border-color:var(--ink); }}
  .pk.on {{ background:var(--goals); border-color:var(--goals); color:#fff; }}
  .pk:disabled {{ opacity:.4; cursor:default; }}
  .picks--locked .picks-h {{ color:var(--hot); }}

  .modal {{
    position:fixed; inset:0; z-index:50; display:flex; align-items:center; justify-content:center;
    background:rgba(0,0,0,.6); padding:20px;
  }}
  .modal[hidden] {{ display:none; }}
  .nv[hidden] {{ display:none; }}
  .modal-box {{
    background:var(--paper); border:1px solid var(--ink); max-width:400px; width:100%;
    padding:22px 22px 20px; position:relative;
  }}
  .modal-box.lb {{ max-width:460px; }}
  .modal-x {{
    position:absolute; top:8px; right:10px; background:none; border:0; cursor:pointer;
    font-size:22px; line-height:1; color:var(--muted);
  }}
  .modal-box h3 {{
    font-family:"Barlow Condensed",sans-serif; font-weight:700; font-size:20px;
    text-transform:uppercase; letter-spacing:.02em; margin-bottom:8px;
  }}
  .modal-box p {{ font-size:12.5px; color:var(--muted); line-height:1.5; }}
  #name-input, .modal-in {{
    width:100%; margin:14px 0 0; padding:10px 12px; font:inherit; font-size:15px;
    background:var(--card); border:1px solid var(--rule); color:var(--ink);
  }}
  .modal-in + .modal-in {{ margin-top:8px; }}
  #name-input:focus, .modal-in:focus {{ outline:2px solid var(--goals); outline-offset:1px; }}
  .pin-wrap {{ position:relative; margin-top:8px; }}
  .pin-wrap .modal-in {{ margin-top:0; padding-right:56px; }}
  .pin-eye {{
    position:absolute; right:8px; top:50%; transform:translateY(-50%);
    background:none; border:0; cursor:pointer; padding:3px 4px;
    font-family:"IBM Plex Mono",monospace; font-size:10px; text-transform:uppercase;
    letter-spacing:.03em; color:var(--corners); text-decoration:underline;
  }}
  .modal-go {{ width:100%; margin-top:12px; padding:10px; background:var(--goals); border-color:var(--goals); color:#fff; }}
  .linklike {{
    display:block; margin:10px 0 0; padding:0; background:none; border:0; cursor:pointer;
    font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--corners);
    text-decoration:underline; letter-spacing:.02em;
  }}
  .rc-box {{
    display:flex; align-items:center; gap:8px; margin:14px 0 4px; padding:12px;
    background:var(--card); border:1px dashed var(--cards);
  }}
  #rc-value {{
    flex:1; font-family:"IBM Plex Mono",monospace; font-size:16px; font-weight:600;
    letter-spacing:.08em; color:var(--ink); word-break:break-all;
  }}
  .lb-tabs {{ display:flex; gap:6px; margin:12px 0 10px; }}
  .lb-tab.on {{ background:var(--ink); color:var(--paper); border-color:var(--ink); }}
  .lb-scroll {{ max-height:52vh; overflow-y:auto; border:1px solid var(--rule); }}
  .hist-search {{ display:flex; gap:6px; margin-top:10px; }}
  .hist-search .modal-in {{ margin:0; flex:1; }}
  #lb-body {{ width:100%; border-collapse:collapse; font-family:"IBM Plex Mono",monospace; font-size:12px; }}
  #lb-body th {{
    text-align:left; font-size:9.5px; letter-spacing:.08em; color:var(--muted);
    padding:7px 10px; border-bottom:1px solid var(--rule); position:sticky; top:0; background:var(--paper);
  }}
  #lb-body td {{ padding:7px 10px; border-bottom:1px solid var(--rule-soft); font-variant-numeric:tabular-nums; }}
  #lb-body td:nth-child(3) {{ font-weight:600; color:var(--ink); }}
  #lb-body tr.me td {{ background:var(--card); color:var(--goals); font-weight:600; }}
  .lb-empty {{ color:var(--muted); text-align:center; }}
  .lb-name {{ color:inherit; text-decoration:none; }}
  .lb-name:hover {{ text-decoration:underline; }}
  .lb-note {{ margin-top:10px; font-size:11px !important; }}
  .lb-prizes {{
    margin-top:12px; padding:10px 12px; border:1px solid var(--cards);
    display:flex; flex-direction:column; gap:3px;
    font-family:"IBM Plex Mono",monospace; font-size:11.5px;
  }}
  .lb-prizes b {{
    font-family:"Barlow Condensed",sans-serif; font-size:12px; text-transform:uppercase;
    letter-spacing:.05em; color:var(--muted); margin-bottom:3px;
  }}
  .lb-prizes span {{ color:var(--ink); }}

  .mp-ca {{
    display:flex; flex-wrap:wrap; align-items:center; gap:4px 8px; margin-bottom:9px;
    font-family:"IBM Plex Mono",monospace; font-size:10px;
  }}
  .mp-ca span {{ text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }}
  .mp-ca a {{ color:var(--ink); word-break:break-all; text-decoration:none; border-bottom:1px solid var(--rule); }}
  .mp-note {{
    font-family:"IBM Plex Mono",monospace; font-size:10px; letter-spacing:.03em;
    text-transform:uppercase; color:var(--warn); border:1px solid var(--warn);
    padding:6px 9px; margin-bottom:11px; line-height:1.5;
  }}
  .mp-imp {{ color:#ab47bc; font-weight:700; }}
  .mp-wallet {{
    display:flex; align-items:center; flex-wrap:wrap; gap:6px 9px; margin-bottom:10px;
    font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--muted);
  }}
  .mp-wallet b {{ color:var(--ink); }}
  .mp-wallet.linked {{ color:var(--goals); }}
  #wl-connect, #wl-disconnect, #wl-signin, #wl-account, .modal-go2, #lb-open, #hist-open, #hist-go, .mp-copy, #wl-signout, #wl-pin, #wl-recovery {{
    font-family:"IBM Plex Mono",monospace; font-size:11px; font-weight:600;
    text-transform:uppercase; letter-spacing:.04em; cursor:pointer;
    background:#ab47bc; color:#fff; border:1px solid #ab47bc; padding:5px 11px;
  }}
  #wl-connect:hover, #wl-disconnect:hover, #wl-signin:hover, #wl-account:hover, #lb-open:hover, #hist-open:hover, #hist-go:hover, .mp-copy:hover, #wl-signout:hover, #wl-pin:hover, #wl-recovery:hover {{
    background:#9036a3; border-color:#9036a3;
  }}
  .modal-go2 {{ width:100%; margin-top:8px; padding:9px; font-size:12px; }}
  #wl-signout, #wl-pin, #wl-recovery {{ font-size:9px; padding:3px 8px; }}
  .mp-copy {{ font-size:10px; padding:3px 9px; }}
  .name-or {{
    text-align:center; font-size:10.5px; color:var(--muted); text-transform:uppercase;
    letter-spacing:.05em; margin:14px 0 8px;
  }}
  .name-err {{ color:var(--hot); font-size:12px !important; margin:6px 0 10px; line-height:1.45; }}
  .name-err:empty {{ display:none; }}
  #wl-connect.wl-change, #wl-disconnect.wl-change {{ font-size:9px; padding:3px 8px; }}
  .mp-pay {{
    margin-top:11px; font-family:"IBM Plex Mono",monospace; font-size:9.5px;
    letter-spacing:.06em; text-transform:uppercase; color:var(--muted);
    background:none; border:0; border-bottom:1px dotted var(--rule); cursor:pointer; padding:0 0 1px;
  }}
  #pay-body td.pay-w {{ font-size:10px; word-break:break-all; max-width:170px; }}
  #pay-body td.pay-w em {{ color:var(--muted); }}
  .pay-json {{
    width:100%; margin-top:10px; font-family:"IBM Plex Mono",monospace; font-size:10px;
    background:var(--card); border:1px solid var(--rule); color:var(--ink); padding:8px; resize:vertical;
  }}

  .ucl {{ margin-top:36px; padding-top:18px; border-top:3px double var(--ink); }}
  .ucl-head {{ display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px; }}
  .ucl-head h2 {{
    font-family:"Barlow Condensed",sans-serif; font-weight:700; font-size:22px;
    text-transform:uppercase; letter-spacing:.02em;
  }}
  .ucl-rate {{
    font-family:"IBM Plex Mono",monospace; font-size:10px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.05em;
  }}
  .ucl-key {{ font-size:12.5px; color:var(--muted); line-height:1.5; margin:8px 0 16px; }}
  .ucl-key b {{ color:var(--ink); }}
  .ucl-fx {{
    background:var(--card); border:1px solid var(--rule); border-left:3px solid var(--corners);
    padding:11px 13px; margin-bottom:9px;
  }}
  .ucl-fx--top {{ border-left-color:var(--cards); }}
  .ucl-fh {{
    display:flex; justify-content:space-between; align-items:baseline; gap:10px;
    flex-wrap:wrap; margin-bottom:8px;
  }}
  .ucl-name {{
    font-family:"Barlow Condensed",sans-serif; font-weight:700; text-transform:uppercase;
    font-size:16px; letter-spacing:.02em;
  }}
  .ucl-name i {{ color:var(--muted); font-style:normal; font-weight:400; }}
  .ucl-meta {{
    font-family:"IBM Plex Mono",monospace; font-size:9px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.05em;
  }}
  .ucl-legs {{ display:flex; flex-direction:column; gap:5px; }}
  .ucl-l {{
    font-family:"IBM Plex Mono",monospace; font-size:11.5px; display:flex; align-items:center;
    gap:8px; flex-wrap:wrap; padding:5px 8px; background:var(--paper); border-left:3px solid var(--rule);
  }}
  .ucl-l b {{
    min-width:56px; text-transform:uppercase; font-size:9.5px; letter-spacing:.05em; color:var(--muted);
  }}
  .ucl-p {{ font-weight:700; color:var(--ink); }}
  .ucl-m {{ color:var(--muted); font-size:10px; }}
  .ucl-x {{ color:var(--goals); font-style:normal; font-weight:600; font-size:10px; text-transform:uppercase; }}
  .ucl-x--red {{ color:var(--hot); }}
  .ucl-note {{ margin-top:12px; border:1px solid var(--warn); background:var(--warn-bg); padding:9px 12px; }}
  .ucl-note summary {{
    font-family:"IBM Plex Mono",monospace; font-size:10px; text-transform:uppercase;
    letter-spacing:.04em; color:var(--warn); cursor:pointer;
  }}
  .ucl-note ul {{ margin:8px 0 0 16px; display:flex; flex-direction:column; gap:6px; }}
  .ucl-note li {{ font-size:12px; line-height:1.5; color:var(--ink); }}
  .ucl-note li b {{ color:var(--ink); }}

  @media (max-width:520px) {{
    .callouts {{ grid-template-columns:1fr; }}
    .line {{ grid-template-columns:44px 1fr; gap:12px; }}
  }}
</style>
<div class="sheet">
  <p class="fresh-note">Rebuilt on every visit. When this bar turns orange the page
  has been open 15+ minutes &mdash; tap Refresh for the latest numbers.</p>
  <div class="freshbar" data-built="{built_iso}">
    <span class="freshbar-age">Updated just now</span>
    <button type="button" class="freshbar-btn"
      onclick="location.replace(location.pathname + '?t=' + Date.now())">&#8635; Refresh</button>
  </div>
  <header class="masthead">
    <h1>Weekend Coupon</h1>
    <p class="tagline">Can you beat the bot?</p>
    <div class="issue">{issue}</div>
  </header>
  {flip_html}
  {intro}
  {results_html}
  {season_html}
  {history_html}
  <section id="mypicks" class="mypicks" hidden></section>
  {coupon_html}
  {ucl_html}
  <script>
  (function () {{
    var el = document.querySelector('.freshbar');
    if (!el) return;
    var t = Date.parse(el.getAttribute('data-built'));
    if (isNaN(t)) return;
    var out = el.querySelector('.freshbar-age');
    var STALE = 900;
    function tick() {{
      var s = Math.max(0, (Date.now() - t) / 1000), txt;
      if (s < 60) txt = 'just now';
      else if (s < 3600) txt = Math.floor(s / 60) + ' min ago';
      else if (s < 86400) txt = Math.floor(s / 3600) + 'h ago';
      else txt = Math.floor(s / 86400) + 'd ago';
      var stale = s > STALE;
      el.classList.toggle('freshbar--stale', stale);
      out.textContent = 'Updated ' + txt + (stale ? ' \\u2014 tap refresh' : '');
    }}
    tick();
    setInterval(tick, 30000);
  }})();

  (function () {{
    var btn = document.getElementById('sort-toggle');
    var main = document.querySelector('.sheet main');
    if (!btn || !main) return;
    var LS = 'coupon_sort';
    var cards = [].slice.call(main.querySelectorAll('.line'));
    var juice = cards.slice();
    var kickoff = cards.slice().sort(function (a, b) {{
      return Date.parse(a.dataset.ko) - Date.parse(b.dataset.ko);
    }});
    function apply(mode) {{
      (mode === 'kickoff' ? kickoff : juice).forEach(function (c) {{ main.appendChild(c); }});
      btn.dataset.mode = mode;
      btn.textContent = mode === 'kickoff' ? 'Order: kickoff time' : 'Order: strongest first';
      try {{ localStorage.setItem(LS, mode); }} catch (e) {{}}
    }}
    btn.addEventListener('click', function () {{
      apply(btn.dataset.mode === 'kickoff' ? 'juice' : 'kickoff');
    }});
    var saved;
    try {{ saved = localStorage.getItem(LS); }} catch (e) {{}}
    if (saved === 'kickoff') apply('kickoff');
  }})();
  </script>

  <div class="modal" id="name-modal" hidden>
    <div class="modal-box">
      <button class="modal-x" data-close aria-label="Close">&times;</button>

      <div class="nv nv-new">
        <h3>Create an account</h3>
        <p>Pick a name and a PIN. You need the PIN to sign in on another phone or
           after signing out &mdash; so pick one you will remember.</p>
        <input id="name-input" class="modal-in" maxlength="20" placeholder="username (e.g. GoonerLewis)" autocomplete="off">
        <div class="pin-wrap">
          <input id="name-pin" class="modal-in" type="password" inputmode="numeric" maxlength="6" placeholder="choose a 4 to 6 digit PIN" autocomplete="off">
          <button type="button" class="pin-eye" data-for="name-pin">show</button>
        </div>
        <p id="name-err" class="name-err"></p>
        <button class="modal-go" id="name-go">Create account</button>
        <div class="name-or">already played?</div>
        <button class="modal-go2" id="to-signin">Sign in with username + PIN</button>
        <button class="modal-go2" id="name-signin">Sign in with Phantom</button>
      </div>

      <div class="nv nv-signin" hidden>
        <h3>Sign in</h3>
        <p>Enter the username and PIN you picked when you created the account.</p>
        <input id="si-name" class="modal-in" maxlength="20" placeholder="your username" autocomplete="off">
        <div class="pin-wrap">
          <input id="si-pin" class="modal-in" type="password" inputmode="numeric" maxlength="6" placeholder="your PIN" autocomplete="off">
          <button type="button" class="pin-eye" data-for="si-pin">show</button>
        </div>
        <p id="si-err" class="name-err"></p>
        <button class="modal-go" id="si-go">Sign in</button>
        <button class="linklike" id="to-recover">Forgot your PIN?</button>
        <div class="name-or">&nbsp;</div>
        <button class="modal-go2" id="to-new">Create a new account instead</button>
      </div>

      <div class="nv nv-recover" hidden>
        <h3>Reset your PIN</h3>
        <p>Enter the recovery code you saved when you created the account.</p>
        <input id="rc-name" class="modal-in" maxlength="20" placeholder="your username" autocomplete="off">
        <input id="rc-code" class="modal-in" maxlength="16" placeholder="recovery code" autocomplete="off">
        <div class="pin-wrap">
          <input id="rc-pin" class="modal-in" type="password" inputmode="numeric" maxlength="6" placeholder="new 4 to 6 digit PIN" autocomplete="off">
          <button type="button" class="pin-eye" data-for="rc-pin">show</button>
        </div>
        <p id="rc-err" class="name-err"></p>
        <button class="modal-go" id="rc-go">Reset PIN</button>
        <button class="linklike" id="rc-back">&larr; back</button>
      </div>

      <div class="nv nv-code" hidden>
        <h3>Save your recovery code</h3>
        <p>This is the <b>only</b> way back in if you forget your PIN and have not
           linked a wallet. Screenshot it or write it down now &mdash; you will not
           see it again.</p>
        <div class="rc-box"><code id="rc-value"></code><button type="button" class="mp-copy" id="rc-copy">copy</button></div>
        <button class="modal-go" id="rc-done">I have saved it</button>
      </div>

      <div class="nv nv-changepin" hidden>
        <h3>Change your PIN</h3>
        <div class="pin-wrap">
          <input id="cp-old" class="modal-in" type="password" inputmode="numeric" maxlength="6" placeholder="current PIN" autocomplete="off">
          <button type="button" class="pin-eye" data-for="cp-old">show</button>
        </div>
        <div class="pin-wrap">
          <input id="cp-new" class="modal-in" type="password" inputmode="numeric" maxlength="6" placeholder="new 4 to 6 digit PIN" autocomplete="off">
          <button type="button" class="pin-eye" data-for="cp-new">show</button>
        </div>
        <p id="cp-err" class="name-err"></p>
        <button class="modal-go" id="cp-go">Change PIN</button>
        <button class="linklike" id="cp-cancel">cancel</button>
      </div>

      <div class="nv nv-getcode" hidden>
        <h3>Get a recovery code</h3>
        <p>Enter your PIN and we will show you a fresh recovery code to save.</p>
        <div class="pin-wrap">
          <input id="gc-pin" class="modal-in" type="password" inputmode="numeric" maxlength="6" placeholder="your PIN" autocomplete="off">
          <button type="button" class="pin-eye" data-for="gc-pin">show</button>
        </div>
        <p id="gc-err" class="name-err"></p>
        <button class="modal-go" id="gc-go">Show my recovery code</button>
        <button class="linklike" id="gc-cancel">cancel</button>
      </div>

    </div>
  </div>

  <div class="modal" id="lb-modal" hidden>
    <div class="modal-box lb">
      <button class="modal-x" data-close aria-label="Close">&times;</button>
      <h3>Leaderboard</h3>
      <div class="lb-tabs">
        <button class="lb-tab on" data-tab="weekend">This weekend</button>
        <button class="lb-tab" data-tab="season">All season</button>
      </div>
      <div class="lb-scroll"><table id="lb-body"></table></div>
      <div class="lb-prizes">
        <b>End-of-season prizes</b>
        <span>&#129351; 1st &mdash; {SEASON_PRIZES[0]:,} {SSB_TICKER}</span>
        <span>&#129352; 2nd &mdash; {SEASON_PRIZES[1]:,} {SSB_TICKER}</span>
        <span>&#129353; 3rd &mdash; {SEASON_PRIZES[2]:,} {SSB_TICKER}</span>
      </div>
      <p class="lb-note">1 point per correct prediction &middot; points convert to {SSB_TICKER} prizes at {SSB_PER_POINT} {SSB_TICKER} per point &middot; prizes distributed weekly. Predictions lock at kickoff.</p>
    </div>
  </div>

  <div class="modal" id="history-modal" hidden>
    <div class="modal-box lb">
      <button class="modal-x" data-close aria-label="Close">&times;</button>
      <h3>Player History</h3>
      <div class="hist-search">
        <input type="text" id="hist-name" class="modal-in" placeholder="Player name">
        <button type="button" id="hist-go">Search</button>
      </div>
      <div id="history-body" class="lb-scroll"></div>
    </div>
  </div>

  <div class="modal" id="pay-modal" hidden>
    <div class="modal-box lb">
      <button class="modal-x" data-close aria-label="Close">&times;</button>
      <h3>Payout sheet</h3>
      <div class="lb-scroll"><table id="pay-body"></table></div>
      <p class="lb-note">Points convert to {SSB_TICKER} prizes at {SSB_PER_POINT} {SSB_TICKER} per point. Prizes are distributed weekly.</p>
      <textarea id="pay-json" class="pay-json" readonly rows="4" aria-label="Payout data as JSON"></textarea>
    </div>
  </div>

  <script id="coupon-cfg" type="application/json">{cfg_json}</script>
  <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.45.4/dist/umd/supabase.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/tweetnacl@1.0.3/nacl-fast.min.js"></script>
  <script>{PICKS_JS}</script>

  {flip_html}

  <footer class="colophon">
    <p><b>What this is.</b> A free weekly prediction game built on a statistical
    model with known blind spots. It's for entertainment — nothing here is
    financial advice.</p>
    <p style="margin-top:9px"><b>Reading the sheet.</b> Figures blend the 2025/26 full season with this
    season so far — the new-season weight climbs to 50&#37; by gameweek 10. Goals come
    from an attack-versus-defence strength model with a home tilt; corners sum each
    side's 2025/26 corners-taken rate; cards sum each side's yellow-card rate. Ticks on
    each meter mark the scoring bands. The juice score (0–100) weights goals 40,
    cards 35, corners 25.</p>
    <p style="margin-top:9px"><b>Last weekend.</b> The band takes the model's
    prediction for each metric and checks whether it landed against what actually
    happened (goals from the final score; corners and cards from ESPN's match
    stats, which can lag a few hours). Predictions are recomputed from current
    data, so it's a close guide to — not an exact replay of — what the sheet
    showed before kickoff.</p>
    <p style="margin-top:9px"><b>Not modelled:</b> the referee (the biggest card
    swing), team news, form momentum, and weather.</p>
    <p style="margin-top:9px"><b>For over-18s.</b> This is a free prediction game
    &mdash; see our full terms.</p>
  </footer>

  <section class="sponsors" aria-label="Sponsors">
    {sponsors_html()}
  </section>
  <p class="tg-line">Join our Telegram:
    <a href="https://t.me/HnLkicinit" target="_blank" rel="noopener">t.me/HnLkicinit</a></p>
</div>"""


if __name__ == "__main__":
    build()
