# PL Bet Builder — Weekend Coupon

Estimates **expected goals, corners and cards** for every Premier League fixture
this weekend, ranks them by a "juice" score, and suggests bet-builder legs.
Built for the Paddy Power free £0.50 bet builder (pre-match, 3+ legs, min combined
odds 2.0 — check the odds yourself, they are not modelled here).

## Run it

```
python build.py                    # the upcoming Fri-Mon, auto-detected
python build.py 20260829 20260901  # an explicit date range
```

Writes `dashboard.html` in this folder. Open it in a browser.

## Weekend timing

- The coupon shows the **current** weekend's fixtures Friday through Monday, then
  rolls to next weekend on **Tuesday** (`weekend_windows()`).
- A sticky **"Updated Xm ago · ↻ Refresh"** bar sits at the top. Edge cache is 10
  min (`api/index.py` `s-maxage=600`), so visitors auto-get near-live data; the
  button forces an instant cache-busting reload.
- Once a weekend finishes, a **"Last weekend — how the model did"** band appears
  near the top: each match's model prediction -> actual for goals / corners / cards
  with the miss amount and a tick if within range.
- Below it, **"Model accuracy — season to date"**: overall hit rate, a bar per
  metric, and a week-by-week strip. Built by walking one scoreboard call per week
  back to 1 Aug -- the scoreboard carries goals, corners and card events, so there
  are no per-match lookups and the whole page builds in ~2.5s.
- During international breaks the coupon window rolls forward automatically (up to
  3 weekends) to the next round with fixtures.

## What it does

1. Pulls this weekend's fixtures from ESPN's public API.
2. For both teams in each tie, pulls season stats (goals, goals conceded, yellow
   cards, red cards, fouls) for **2025/26 (full)** and **2026/27 (so far)**.
3. Blends them — current-season weight rises to 50% by gameweek 10.
4. Corners come from `corners.json` (2025/26 corners-taken per game, from StatMuse —
   ESPN doesn't expose corners). Refresh once a season.
5. Model:
   - **Goals** = attack-vs-defence strength vs league average, with a home tilt.
   - **Corners** = sum of both sides' corners-taken rate.
   - **Cards** = sum of both sides' yellow-card rate.
   - **Juice score** (0-100) = goals 40% + cards 35% + corners 25%.

## Not modelled

Referee (the biggest card swing), team news, form momentum, weather, and the odds
themselves. Always sanity-check against the live Paddy Power app.

## Hosted (always on)

**https://plbetbuilder.vercel.app** — Vercel serverless function (`api/index.py`)
that builds the coupon fresh on each request. Responses are edge-cached 1h and
served stale-while-revalidate for a week, so an ESPN outage never blanks the page.
A Vercel cron (`vercel.json`, `0 7 * * 5`) hits it every Friday 07:00 UTC to warm
the cache before the weekend. Deploy updates with `vercel deploy --prod`.

## Local weekly automation

A Windows Task Scheduler job **"PL Bet Builder Coupon"** runs `run.bat` every
Friday at 08:00, regenerating the local `dashboard.html`. Remove it with:

```powershell
Unregister-ScheduledTask -TaskName "PL Bet Builder Coupon"
```

## Paper account (model forward test)

`/paper` — a bookkeeping test of the model's own coupon. Each week it auto-places
the legs `suggest()` would put on (£10 flat per single + one £10 combined
"builder" on the top-rated fixture), then settles them from ESPN results.
Premier League and Champions League are separate books, £1,000 bankroll each.
No real money.

**Odds:** "Over 2.5 goals" and "BTTS" legs are priced from real bookmaker odds
(The Odds API free tier — set env `ODDS_API_KEY`) fetched at placement time and
marked `live` on the dashboard; every other leg (Over/Under 1.5/3.5 goals,
corners, cards, red card) comes from the fixed `PRICES` table in `paper.py`.
Without the key it falls back entirely to the table. Each bet stores
`price_src` = real | table | mixed.

- **Runs weekly** on the back of the Thursday payout cron (`api/payout.py` calls
  `paper.run_all()` — settle everything finished, then place the coming week's).
  Vercel Hobby caps a project at 2 crons and both are used, hence the piggyback.
- **Manual run:** `GET /paper?run=<CRON_SECRET>` (settle + place now).
- **Setup:** run `supabase/coupon_paper_bets.sql` once in the shared project.

## Files

| File | Purpose |
|---|---|
| `build.py` | fetch + model + render |
| `paper.py` | paper account — place / settle / bankroll / `/paper` page |
| `corners.json` | season corners table (manual, ~once a season) |
| `dashboard.html` | generated output |
| `run.bat` | wrapper for the scheduled task |

## Refreshing corners each season

Ask StatMuse: *"premier league <season> teams corners per game"*, then update the
values in `corners.json` (keys are ESPN team IDs — see the map in `build.py`).
Promoted sides get an estimate.
