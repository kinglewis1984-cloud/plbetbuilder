"""
TEST / PREVIEW ONLY — does not touch the live site, build.py output, or the
main artifact.

Renders an alternative "last weekend" band that shows, per fixture per metric:

    the PICK we suggested  (OVER / UNDER + line)  ->  actual  ->  WON / LOST

instead of the current "model prediction -> actual (within range)" band.

Run:  python test_picks_band.py    ->  writes test_picks_band.html
"""

from pathlib import Path

from build import (
    team_blends, weekend_windows, season_matches, last_weekend_results,
    pick_line, half,
)

HERE = Path(__file__).parent
METRICS = (("goals", "Goals"), ("corners", "Corners"), ("cards", "Cards"))


def pick_for(metric, model_val):
    """The suggested O/U line + side for a fixture, from the model's expectation."""
    line = pick_line(metric, model_val)
    side = "OVER" if model_val > line else "UNDER"
    return side, line


def graded_pick(metric, model_val, actual):
    side, line = pick_for(metric, model_val)
    won = actual > line if side == "OVER" else actual < line
    return side, line, won


def row_html(it):
    f, a, m = it["fx"], it["actual"], it["model"]
    cells = []
    for key, label in METRICS:
        if key in ("corners", "cards") and not a["has_stats"]:
            cells.append(
                f'<span class="c c--na"><b>{label}</b> no data</span>'
            )
            continue
        side, line, won = graded_pick(key, m[key], a[key])
        cls = "win" if won else "loss"
        mark = "WON" if won else "LOST"
        cells.append(
            f'<span class="c c--{cls}"><b>{label}</b> '
            f'<span class="pick">{side} {line:g}</span> '
            f'<span class="arrow">&rarr;</span> '
            f'<span class="act">{a[key]}</span> '
            f'<span class="mark">{mark}</span> '
            f'<span class="modelnote">model {half(m[key]):.1f}</span></span>'
        )
    score = f'{f["home"]} {a["h_goals"]}&ndash;{a["a_goals"]} {f["away"]}'
    return f'''
      <article class="fx">
        <div class="fx-h"><span class="fx-date">{f["date"][:10]}</span>
          <span class="fx-score">{score}</span></div>
        <div class="fx-cells">{"".join(cells)}</div>
      </article>'''


def build_page():
    blends = team_blends()
    prev_win, _cur = weekend_windows()
    season = season_matches(blends)
    results = last_weekend_results(season, prev_win) if prev_win else None

    if not results or not results[2]:
        body = "<p>No settled fixtures from last weekend to show.</p>"
        won = total = 0
    else:
        d1, d2, items = results
        won = total = 0
        for it in items:
            for key, _ in METRICS:
                if key in ("corners", "cards") and not it["actual"]["has_stats"]:
                    continue
                total += 1
                _s, _l, w = graded_pick(key, it["model"][key], it["actual"][key])
                if w:
                    won += 1
        body = "".join(row_html(it) for it in items)

    pct = f"{won / total * 100:.0f}%" if total else "&mdash;"
    return f"""<meta charset="utf-8">
<title>Weekend Coupon — picks preview</title>
<style>
  :root {{
    --bg:#ece4d3; --card:#f6f1e6; --ink:#221f18; --muted:#726a58; --rule:#d3c8ae;
    --win:#1f7a44; --loss:#bf3b2b;
  }}
  @media (prefers-color-scheme:dark) {{
    :root:not([data-theme="light"]) {{
      --bg:#15170f; --card:#1d1f16; --ink:#e8e3d5; --muted:#8f8873; --rule:#343625;
      --win:#4cae74; --loss:#e0614f;
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
    font-size:30px; text-transform:uppercase; letter-spacing:.01em;
  }}
  .head {{
    display:flex; justify-content:space-between; align-items:baseline;
    border-bottom:3px double var(--ink); padding-bottom:10px; margin-bottom:6px;
  }}
  .rate {{ font-family:"IBM Plex Mono",monospace; font-size:15px; font-weight:600; }}
  .key {{ font-size:12.5px; color:var(--muted); margin:10px 0 18px; }}
  .fx {{
    background:var(--card); border:1px solid var(--rule);
    border-left:3px solid var(--muted); padding:10px 12px; margin-bottom:9px;
  }}
  .fx-h {{ display:flex; gap:10px; align-items:baseline; margin-bottom:7px; }}
  .fx-date {{
    font-family:"IBM Plex Mono",monospace; font-size:9.5px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.06em;
  }}
  .fx-score {{
    font-family:"Barlow Condensed","Barlow",sans-serif; font-weight:700;
    text-transform:uppercase; font-size:15px; letter-spacing:.02em;
  }}
  .fx-cells {{ display:flex; flex-direction:column; gap:5px; }}
  .c {{
    font-family:"IBM Plex Mono",monospace; font-size:11.5px;
    display:flex; align-items:center; gap:7px; flex-wrap:wrap;
    padding:5px 8px; background:var(--bg); border-left:3px solid var(--rule);
  }}
  .c b {{ min-width:58px; text-transform:uppercase; font-size:9.5px; letter-spacing:.05em; color:var(--muted); }}
  .c .pick {{ font-weight:600; color:var(--ink); }}
  .c .act {{ font-weight:700; }}
  .c .mark {{ font-weight:700; letter-spacing:.04em; }}
  .c .modelnote {{ margin-left:auto; font-size:9.5px; color:var(--muted); }}
  .c--win {{ border-left-color:var(--win); }}
  .c--win .mark, .c--win .act {{ color:var(--win); }}
  .c--loss {{ border-left-color:var(--loss); }}
  .c--loss .mark, .c--loss .act {{ color:var(--loss); }}
  .c--na {{ color:var(--muted); }}
</style>
<div class="sheet">
  <div class="banner">Preview / test page &mdash; not the live site. Shows what
    the suggested Over/Under pick was and whether it landed, instead of the
    &plusmn;range band.</div>
  <div class="head">
    <h1>Last weekend &mdash; did our picks land?</h1>
    <span class="rate">{won}/{total} &nbsp;{pct}</span>
  </div>
  <p class="key">Each metric shows the <b>suggested pick</b> (over/under + line)
    &rarr; the <b>actual</b> number in the match &rarr; <b>won or lost</b>.
    "model" in grey is the raw expected value the pick came from.</p>
  {body}
</div>"""


if __name__ == "__main__":
    html = build_page()
    out = HERE / "test_picks_band.html"
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out}  ({len(html)} bytes)")
