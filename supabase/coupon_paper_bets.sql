-- Weekend Coupon — paper account (model forward test)
-- Auto-places the model's own suggested legs each week and tracks a bankroll.
-- Run once in the SQL editor of the shared project (hsccirgnwfwxccwjmgjf),
-- same place as the other coupon_*.sql files. Safe to re-run.

create table if not exists public.coupon_paper_bets (
  id          uuid primary key default gen_random_uuid(),
  comp        text not null check (comp in ('pl','ucl')),
  round       text not null,                 -- e.g. 'pl:2026-09-12'
  bet_type    text not null check (bet_type in ('single','builder')),
  fixture_id  text not null,
  fixture     text not null,                 -- 'Ars v Che'
  kickoff     timestamptz,
  market      text not null,                 -- goals | goals_u | corners | cards | btts | red | builder
  line        numeric not null default 0,
  leg_text    text not null,
  legs        jsonb,                         -- builder only: [{market,line,text}]
  price       numeric not null,
  price_src   text not null default 'table',   -- real | table | mixed
  stake       numeric not null,
  status      text not null default 'pending' check (status in ('pending','won','lost','void')),
  pnl         numeric,
  placed_at   timestamptz not null default now(),
  settled_at  timestamptz
);

-- one bet per (comp, fixture, type, market, line) — makes the Friday/Thursday
-- placement run idempotent (re-running never double-places).
-- re-run safe: add price_src to an existing table
alter table public.coupon_paper_bets
  add column if not exists price_src text not null default 'table';

create unique index if not exists coupon_paper_bets_uk
  on public.coupon_paper_bets (comp, fixture_id, bet_type, market, line);
create index if not exists coupon_paper_bets_comp_status_idx
  on public.coupon_paper_bets (comp, status);
create index if not exists coupon_paper_bets_placed_idx
  on public.coupon_paper_bets (placed_at);

alter table public.coupon_paper_bets enable row level security;

-- Public can READ (the dashboard shows the bankroll). All writes come from the
-- paper engine via the service_role key, which bypasses RLS — so there is
-- deliberately no anon insert/update/delete policy.
drop policy if exists coupon_paper_bets_read on public.coupon_paper_bets;
create policy coupon_paper_bets_read on public.coupon_paper_bets for select using (true);

grant select on public.coupon_paper_bets to anon;
