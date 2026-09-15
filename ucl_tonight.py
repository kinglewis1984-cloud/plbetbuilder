"""
One-off: goals / corners / cards predictions for tonight's Champions League games.

Reuses the PL model's maths (build.expected / suggest / pick_line) but pulls each
club's DOMESTIC-league season stats from ESPN, since the strength model needs a
per-game history and the UCL itself has almost none yet.

Rough by nature: one league-average is used across all leagues, corners come from
a 2-4 game current-season sample (ESPN has no prior-season corner data), and UCL
refs / intensity differ from domestic. Treat as a guide, not the PL sheet.

Run:  python ucl_tonight.py [YYYYMMDD]
"""

import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from build import UA, expected, pick_line, half, rating, LEAGUE_AVG_GF

DATE = sys.argv[1] if len(sys.argv) > 1 else "20260908"

# ESPN club id -> domestic league slug
LEAGUE = {
    "887": "gre.1", "4411": "aut.1", "570": "bel.1", "362": "eng.1",
    "124": "ger.1", "102": "esp.1", "437": "por.1", "382": "eng.1",
    "166": "fra.1", "244": "esp.1", "86": "esp.1", "110": "ita.1",
}
LAST, THIS = 2025, 2026


def get(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25))


def flat(raw):
    out = {}
    for cat in raw.get("splits", {}).get("categories", []):
        for s in cat.get("stats", []):
            out[s["name"]] = s.get("value", 0.0) or 0.0
    return out


def season(team_id, league, year):
    try:
        raw = get(f"https://sports.core.api.espn.com/v2/sports/soccer/leagues/{league}"
                  f"/seasons/{year}/types/1/teams/{team_id}/statistics")
    except Exception:
        return None
    s = flat(raw)
    gp = s.get("appearances", 0)
    if not gp:
        return None
    return {
        "gp": gp,
        "gf_pg": s.get("totalGoals", 0) / gp,
        "ga_pg": s.get("goalsConceded", 0) / gp,
        "yc_pg": s.get("yellowCards", 0) / gp,
        "rc": s.get("redCards", 0),
        "won_corners_pg": s.get("wonCorners", 0) / gp,   # only populated for THIS season
    }


def blend(team_id):
    league = LEAGUE.get(team_id, "eng.1")
    last = season(team_id, league, LAST)
    cur = season(team_id, league, THIS)
    base = last or {"gf_pg": 1.25, "ga_pg": 1.35, "yc_pg": 2.0, "rc": 4, "won_corners_pg": 5.0}

    if cur and cur["gp"] >= 2:
        w = min(cur["gp"] / 16.0, 0.35)   # tiny samples this early -> lean on last season
        m = {k: base[k] * (1 - w) + cur[k] * w for k in ("gf_pg", "ga_pg", "yc_pg")}
    else:
        m = {k: base[k] for k in ("gf_pg", "ga_pg", "yc_pg")}
    m["rc"] = base["rc"]
    # corners: current-season sample only (prior season has none); fall back to ~5
    cpg = cur["won_corners_pg"] if (cur and cur["gp"] >= 2 and cur["won_corners_pg"] > 0) else 5.0
    m["corners_pg"] = cpg
    m["promoted"] = False
    return m


def side_line(metric, val):
    line = pick_line(metric, val)
    return ("Over" if val > line else "Under"), line


CAVEATS = [
    ("Home / away goal splits are skewed.", "The model has weaker-league defences "
     "(Brugge, Porto) looking elite, so the away side's goals get crushed &mdash; "
     "e.g. Villa and City barely scoring on the road. The <b>match total</b> is far "
     "more reliable than the split. Fade &ldquo;Porto v City under 3.5&rdquo; &mdash; "
     "City drags games up regardless."),
    ("Corners are a guess.", "ESPN has no prior-season corner data, so these come "
     "from a 2&ndash;4 game current-season sample. 13&ndash;14 for Madrid / Porto is shaky."),
    ("One scoring average for all of Europe.", "The model uses a single league-average "
     "goals figure across the Bundesliga, La Liga, Ligue 1, etc. UCL referees also card "
     "more than domestic games, so the card picks probably lean a touch high."),
    ("Best-looking calls:", "Dortmund overs, and Real Madrid&ndash;Inter BTTS / cards over 2.5."),
]


def html_page(games, blends, date):
    rows = []
    for g in games:
        x = expected(blends[g["h_id"]], blends[g["a_id"]])
        g["x"], g["juice"] = x, rating(x)
    for g in sorted(games, key=lambda x: x["juice"], reverse=True):
        x = g["x"]
        cells = []
        for key, label in (("goals", "Goals"), ("corners", "Corners"), ("cards", "Cards")):
            s, l = side_line(key, x[key])
            m = half(x[key])
            extra = ""
            if key == "goals" and x["btts"]:
                extra = ' <span class="xtra">+ BTTS</span>'
            if key == "cards" and x["red_risk"]:
                extra = ' <span class="xtra xtra--red">red watch</span>'
            cells.append(
                f'<span class="c"><b>{label}</b> '
                f'<span class="pick">{s} {l:g}</span> '
                f'<span class="modelnote">model {m:.1f}</span>{extra}</span>'
            )
        rows.append(f'''
      <article class="fx">
        <div class="fx-h">
          <span class="fx-name">{g["h"]} <i>v</i> {g["a"]}</span>
          <span class="fx-meta">{g["kick"][11:16]} UTC &middot; juice {g["juice"]}/100</span>
        </div>
        <div class="fx-cells">{"".join(cells)}</div>
      </article>''')

    caveat_html = "".join(
        f'<li><b>{h}</b> {b}</li>' for h, b in CAVEATS
    )
    return f"""<meta charset="utf-8">
<title>Champions League — tonight (preview)</title>
<style>
  :root {{
    --bg:#ece4d3; --card:#f6f1e6; --ink:#221f18; --muted:#726a58; --rule:#d3c8ae;
    --goals:#1f7a44; --corners:#2f6f9e; --cards:#c98a08; --hot:#bf3b2b;
    --warn:#b0521c; --warn-bg:#f1e0cd;
  }}
  @media (prefers-color-scheme:dark) {{
    :root:not([data-theme="light"]) {{
      --bg:#15170f; --card:#1d1f16; --ink:#e8e3d5; --muted:#8f8873; --rule:#343625;
      --goals:#4cae74; --corners:#5aa0cf; --cards:#e0a72c; --hot:#e0614f;
      --warn:#e0863f; --warn-bg:#2c2113;
    }}
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{
    background:var(--bg); color:var(--ink); padding:28px 16px 60px;
    font:16px/1.5 "Barlow",-apple-system,Segoe UI,Roboto,sans-serif;
  }}
  .sheet {{ max-width:720px; margin:0 auto; }}
  .banner {{
    font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.06em; margin-bottom:14px;
    border:1px dashed var(--rule); padding:8px 10px;
  }}
  h1 {{
    font-family:"Barlow Condensed","Barlow",sans-serif; font-weight:700;
    font-size:32px; text-transform:uppercase; letter-spacing:.01em;
    border-bottom:3px double var(--ink); padding-bottom:10px; margin-bottom:6px;
  }}
  .sub {{
    font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.05em; margin-bottom:20px;
  }}
  .fx {{
    background:var(--card); border:1px solid var(--rule);
    border-left:3px solid var(--corners); padding:11px 13px; margin-bottom:9px;
  }}
  .fx-h {{ display:flex; justify-content:space-between; align-items:baseline;
    gap:10px; flex-wrap:wrap; margin-bottom:8px; }}
  .fx-name {{
    font-family:"Barlow Condensed","Barlow",sans-serif; font-weight:700;
    text-transform:uppercase; font-size:16px; letter-spacing:.02em;
  }}
  .fx-name i {{ color:var(--muted); font-style:normal; font-weight:400; }}
  .fx-meta {{
    font-family:"IBM Plex Mono",monospace; font-size:9.5px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.05em;
  }}
  .fx-cells {{ display:flex; flex-direction:column; gap:5px; }}
  .c {{
    font-family:"IBM Plex Mono",monospace; font-size:11.5px;
    display:flex; align-items:center; gap:8px; flex-wrap:wrap;
    padding:5px 8px; background:var(--bg); border-left:3px solid var(--rule);
  }}
  .c b {{ min-width:58px; text-transform:uppercase; font-size:9.5px;
    letter-spacing:.05em; color:var(--muted); }}
  .c .pick {{ font-weight:700; color:var(--ink); }}
  .c .modelnote {{ color:var(--muted); font-size:10px; }}
  .xtra {{ color:var(--goals); font-weight:600; font-size:10px; text-transform:uppercase; }}
  .xtra--red {{ color:var(--hot); }}
  .caveats {{
    margin-top:22px; border:1px solid var(--warn); background:var(--warn-bg);
    padding:12px 14px;
  }}
  .caveats h2 {{
    font-family:"Barlow Condensed","Barlow",sans-serif; font-weight:700;
    text-transform:uppercase; font-size:15px; color:var(--warn); margin-bottom:8px;
  }}
  .caveats ul {{ list-style:none; display:flex; flex-direction:column; gap:8px; }}
  .caveats li {{ font-size:12.5px; line-height:1.5; }}
  .caveats b {{ color:var(--ink); }}
</style>
<div class="sheet">
  <div class="banner">Preview / test page &mdash; NOT live. Rough one-off model
    for tonight's Champions League. Uses each club's domestic-league season stats.</div>
  <h1>Champions League &mdash; tonight</h1>
  <div class="sub">{date[6:8]}/{date[4:6]}/{date[0:4]} &middot; strongest first &middot;
    model &rarr; suggested over / under pick</div>
  {"".join(rows)}
  <div class="caveats">
    <h2>Trust these less than the PL sheet</h2>
    <ul>{caveat_html}</ul>
  </div>
</div>"""


def main():
    sb = get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/"
             f"scoreboard?dates={DATE}")
    events = sb.get("events", [])
    if not events:
        print(f"No Champions League fixtures found for {DATE}.")
        return

    games = []
    for ev in events:
        c = ev["competitions"][0]
        h = next(x for x in c["competitors"] if x["homeAway"] == "home")
        a = next(x for x in c["competitors"] if x["homeAway"] == "away")
        games.append({
            "kick": ev["date"], "h_id": h["team"]["id"], "a_id": a["team"]["id"],
            "h": h["team"]["displayName"], "a": a["team"]["displayName"],
        })

    ids = {g["h_id"] for g in games} | {g["a_id"] for g in games}
    with ThreadPoolExecutor(max_workers=8) as ex:
        blends = dict(zip(ids, ex.map(blend, ids)))

    print(f"\nCHAMPIONS LEAGUE — {DATE}   (league avg goals/team used: {LEAGUE_AVG_GF})")
    print("=" * 78)
    for g in sorted(games, key=lambda x: x["kick"]):
        x = expected(blends[g["h_id"]], blends[g["a_id"]])
        gs, gl = side_line("goals", x["goals"])
        cs, cl = side_line("corners", x["corners"])
        ks, kl = side_line("cards", x["cards"])
        juice = rating(x)
        print(f"\n{g['h']}  v  {g['a']}   ({g['kick'][11:16]} UTC)   juice {juice}/100")
        print(f"   Goals   : model {x['goals']:.2f}   "
              f"({x['h_goals']:.2f} - {x['a_goals']:.2f})   -> pick {gs} {gl:g}"
              f"{'   + BTTS' if x['btts'] else ''}")
        print(f"   Corners : model {half(x['corners']):.1f}   -> pick {cs} {cl:g}")
        print(f"   Cards   : model {x['cards']:.2f}   -> pick {ks} {kl:g}"
              f"{'   (red-card watch)' if x['red_risk'] else ''}")
    print("\n" + "=" * 78)
    print("Rough model: one Europe-wide scoring average, small home tilt, corners")
    print("from a 2-4 game current-season sample. Check the actual odds before backing.")

    from pathlib import Path
    out = Path(__file__).parent / "ucl_tonight.html"
    out.write_text(html_page(games, blends, DATE), encoding="utf-8")
    print(f"\nWrote {out}  ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
