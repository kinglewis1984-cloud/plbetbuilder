-- Weekend Coupon — picks + leaderboard
-- Run this once in the Supabase SQL editor of the SHARED project
-- (hsccirgnwfwxccwjmgjf — the one Football Fan Hub + Shadow Takedown use).
-- Safe to re-run: everything is IF NOT EXISTS / OR REPLACE.

-- ── usernames ──────────────────────────────────────────────────────────────
create table if not exists public.coupon_players (
  id         uuid primary key default gen_random_uuid(),
  name       text not null unique,
  created_at timestamptz not null default now()
);

-- ── picks (insert-only, one row per selection) ─────────────────────────────
-- market:    goals_ou | corners_ou | cards_ou | btts | result
-- selection: over | under | yes | no | home | draw | away
-- Lines are fixed (goals 2.5, corners 10.5, cards 3.5) so everyone picks the
-- same line and there is nothing to tamper with. A pick only scores if it was
-- made before kickoff — and the grader checks that against the real fixture
-- time, not anything stored here.
create table if not exists public.coupon_picks (
  id         uuid primary key default gen_random_uuid(),
  player     text not null,
  fixture_id text not null,
  market     text not null check (market in ('goals_ou','corners_ou','cards_ou','btts','result')),
  selection  text not null check (selection in ('over','under','yes','no','home','draw','away')),
  kickoff    timestamptz,                       -- advisory / display only
  created_at timestamptz not null default now()
);

create index if not exists coupon_picks_player_fixture_idx
  on public.coupon_picks (player, fixture_id);
create index if not exists coupon_picks_created_idx
  on public.coupon_picks (created_at);

-- ── force created_at server-side (client cannot back-date a pick) ──────────
create or replace function public.coupon_stamp() returns trigger
  language plpgsql as $$
begin
  new.created_at := now();
  return new;
end $$;

drop trigger if exists coupon_picks_stamp on public.coupon_picks;
create trigger coupon_picks_stamp before insert on public.coupon_picks
  for each row execute function public.coupon_stamp();

drop trigger if exists coupon_players_stamp on public.coupon_players;
create trigger coupon_players_stamp before insert on public.coupon_players
  for each row execute function public.coupon_stamp();

-- ── row-level security: anyone may read and insert, nobody may edit/delete ──
alter table public.coupon_players enable row level security;
alter table public.coupon_picks   enable row level security;

drop policy if exists coupon_players_read   on public.coupon_players;
drop policy if exists coupon_players_insert on public.coupon_players;
create policy coupon_players_read   on public.coupon_players for select using (true);
create policy coupon_players_insert on public.coupon_players for insert with check (true);

drop policy if exists coupon_picks_read   on public.coupon_picks;
drop policy if exists coupon_picks_insert on public.coupon_picks;
create policy coupon_picks_read   on public.coupon_picks for select using (true);
create policy coupon_picks_insert on public.coupon_picks for insert with check (true);
-- (no update / delete policy = anon key can never modify or remove a row)

grant usage on schema public to anon;
grant select, insert on public.coupon_players to anon;
grant select, insert on public.coupon_picks   to anon;
