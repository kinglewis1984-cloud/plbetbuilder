"""
TEST / PREVIEW ONLY — does not touch the live site or the main artifact.

A "History" view: every past week this season, each with its record and every
match's suggested pick -> actual -> won/lost (reuses the same grading as the
live "last weekend" band). A History button expands/collapses each week.

Run:  python test_history.py   ->  writes test_history.html
"""

from datetime import datetime, timedelta
from pathlib import Path

from build import team_blends, season_matches, pick_line, half, match_day

HERE = Path(__file__).parent
METRICS = (("goals", "Goals"), ("corners", "Corners"), ("cards", "Cards"))


def pick_landed(metric, model_val, actual):
    line = pick_line(metric, model_val)
    return actual > line if model_val > line else actual < line


def week_of(iso_date):
    dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    return (dt - timedelta(days=(dt.weekday() - 4) % 7)).date()   # football-week Friday


def match_html(g):
    f, a, m = g["fx"], g["actual"], g["model"]
    cells = []
    for key, label in METRICS:
        if key in ("corners", "cards") and not a["has_stats"]:
            cells.append(f'<span class="c c--na"><b>{label}</b> no data</span>')
            continue
        line = pick_line(key, m[key])
        side = "Over" if m[key] > line else "Under"
        won = pick_landed(key, m[key], a[key])
        cls = "win" if won else "loss"
        mark = "WON" if won else "LOST"
        cells.append(
            f'<span class="c c--{cls}"><b>{label}</b> '
            f'<span class="pick">{side} {line:g}</span> '
            f'<span class="arrow">&rarr;</span> '
            f'<span class="act">{a[key]}</span> '
            f'<span class="mark">{mark}</span></span>'
        )
    score = f'{f["home"]} {a["h_goals"]}&ndash;{a["a_goals"]} {f["away"]}'
    return f'''
        <div class="fx">
          <div class="fx-h"><span class="fx-date">{match_day(f["date"])}</span>
            <span class="fx-score">{score}</span></div>
          <div class="fx-cells">{"".join(cells)}</div>
        </div>'''


def week_html(fri, games, is_first):
    won = total = 0
    for g in games:
        for key, _ in METRICS:
            if key in ("corners", "cards") and not g["actual"]["has_stats"]:
                continue
            total += 1
            if pick_landed(key, g["model"][key], g["actual"][key]):
                won += 1
    pct = f"{won / total * 100:.0f}%" if total else "&mdash;"
    label = match_day(games[0]["fx"]["date"])
    span = f"{label}" if len(games) == 1 else f"w/o {fri.strftime('%d %b').upper()}"
    rows = "".join(match_html(g) for g in sorted(games, key=lambda g: g["fx"]["date"]))
    return f'''
      <details class="wk"{" open" if is_first else ""}>
        <summary>
          <span class="wk-span">{span}</span>
          <span class="wk-rate">{won}/{total} &nbsp;{pct}</span>
        </summary>
        <div class="wk-body">{rows}</div>
      </details>'''


def build_page():
    blends = team_blends()
    season = season_matches(blends)
    by_week = {}
    for g in season:
        by_week.setdefault(week_of(g["fx"]["date"]), []).append(g)
    weeks = sorted(by_week.items(), key=lambda kv: kv[0], reverse=True)
    body = "".join(week_html(fri, games, i == 0) for i, (fri, games) in enumerate(weeks))

    return f"""<meta charset="utf-8">
<title>Weekend Coupon — history preview</title>
<style>
  :root {{
    --bg:#ece4d3; --card:#f6f1e6; --ink:#221f18; --muted:#726a58; --rule:#d3c8ae;
    --win:#1f7a44; --loss:#bf3b2b; --accent:#ab47bc;
  }}
  @media (prefers-color-scheme:dark) {{
    :root:not([data-theme="light"]) {{
      --bg:#15170f; --card:#1d1f16; --ink:#e8e3d5; --muted:#8f8873; --rule:#343625;
      --win:#4cae74; --loss:#e0614f; --accent:#ab47bc;
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
  .mast {{ border-bottom:3px double var(--ink); padding-bottom:12px; margin-bottom:18px; }}
  .mast h1 {{
    font-family:"Barlow Condensed","Barlow",sans-serif; font-weight:700;
    font-size:30px; text-transform:uppercase; letter-spacing:.01em;
  }}
  .mast p {{
    margin-top:4px; font-family:"IBM Plex Mono",monospace; font-size:11px;
    color:var(--muted); text-transform:uppercase; letter-spacing:.04em;
  }}
  .hist-open {{
    display:block; margin:0 0 18px; padding:9px 16px;
    background:var(--accent); color:#fff; border:1px solid var(--accent);
    font-family:"IBM Plex Mono",monospace; font-size:11px; font-weight:700;
    text-transform:uppercase; letter-spacing:.06em; cursor:pointer;
  }}
  .hist-open:hover {{ background:#9036a3; border-color:#9036a3; }}
  #hist-panel[hidden] {{ display:none; }}
  h2.hist-h {{
    font-family:"Barlow Condensed","Barlow",sans-serif; font-weight:700;
    font-size:20px; text-transform:uppercase; letter-spacing:.01em; margin-bottom:12px;
  }}
  details.wk {{
    background:var(--card); border:1px solid var(--rule); margin-bottom:8px;
  }}
  details.wk summary {{
    display:flex; justify-content:space-between; align-items:center; cursor:pointer;
    padding:11px 14px; list-style:none;
    font-family:"Barlow Condensed","Barlow",sans-serif; font-weight:700;
    text-transform:uppercase; font-size:14px; letter-spacing:.02em;
  }}
  details.wk summary::-webkit-details-marker {{ display:none; }}
  details.wk summary::before {{ content:"+ "; color:var(--muted); }}
  details.wk[open] summary::before {{ content:"\\2212 "; }}
  .wk-rate {{ font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--muted); }}
  .wk-body {{ padding:0 14px 12px; display:flex; flex-direction:column; gap:9px; }}
  .fx {{ border-top:1px dotted var(--rule); padding-top:9px; }}
  .fx-h {{ display:flex; gap:10px; align-items:baseline; margin-bottom:6px; }}
  .fx-date {{
    font-family:"IBM Plex Mono",monospace; font-size:9px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.05em;
  }}
  .fx-score {{
    font-family:"Barlow Condensed","Barlow",sans-serif; font-weight:700;
    text-transform:uppercase; font-size:14px;
  }}
  .fx-cells {{ display:flex; flex-direction:column; gap:5px; }}
  .c {{
    font-family:"IBM Plex Mono",monospace; font-size:11px;
    display:flex; align-items:center; gap:7px; flex-wrap:wrap;
    padding:4px 8px; background:var(--bg); border-left:3px solid var(--rule);
  }}
  .c b {{ min-width:52px; text-transform:uppercase; font-size:9px; letter-spacing:.05em; color:var(--muted); }}
  .c .pick {{ font-weight:600; color:var(--ink); }}
  .c .act {{ font-weight:700; }}
  .c .mark {{ font-weight:700; letter-spacing:.03em; font-size:9.5px; }}
  .c--win {{ border-left-color:var(--win); }}
  .c--win .mark, .c--win .act {{ color:var(--win); }}
  .c--loss {{ border-left-color:var(--loss); }}
  .c--loss .mark, .c--loss .act {{ color:var(--loss); }}
  .c--na {{ color:var(--muted); }}
</style>
<div class="sheet">
  <div class="banner">Preview / test page &mdash; not the live site. The History
    section starts hidden, same as it would on weekendcoupon.com &mdash; press
    the button to reveal it.</div>
  <header class="mast">
    <h1>Weekend Coupon</h1>
    <p>Premier League &middot; mock masthead for this preview</p>
  </header>
  <button type="button" class="hist-open" id="hist-btn"
    onclick="var p=document.getElementById('hist-panel'); p.hidden=!p.hidden; this.textContent = p.hidden ? 'History' : 'Hide history';">History</button>
  <section id="hist-panel" hidden>
    <h2 class="hist-h">Result history</h2>
    {body}
  </section>
</div>"""


if __name__ == "__main__":
    html = build_page()
    out = HERE / "test_history.html"
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out}  ({len(html)} bytes)")
