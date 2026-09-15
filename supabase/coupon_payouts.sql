-- Weekend Coupon — payout ledger
-- Run once in the SQL editor of the shared project (football-fan-hub /
-- hsccirgnwfwxccwjmgjf), same place as the other coupon_*.sql files.
-- Safe to re-run.

create table if not exists public.coupon_payouts (
  id         uuid primary key default gen_random_uuid(),
  round      text not null,          -- e.g. 'weekly:2026-08-28'  or 'season:2026-27:1'
  player     text not null,
  wallet     text not null,
  points     integer not null,
  ssb        numeric not null,       -- tokens sent
  signature  text,                   -- Solana tx signature (null until confirmed)
  status     text not null default 'pending',   -- pending | sent | failed
  created_at timestamptz not null default now()
);

-- one payout per player per round (stops double-paying)
create unique index if not exists coupon_payouts_round_player_uk
  on public.coupon_payouts (round, player);
create index if not exists coupon_payouts_round_idx on public.coupon_payouts (round);

drop trigger if exists coupon_payouts_stamp on public.coupon_payouts;
create trigger coupon_payouts_stamp before insert on public.coupon_payouts
  for each row execute function public.coupon_stamp();

alter table public.coupon_payouts enable row level security;

-- Everyone can READ the ledger (transparency). Writes come only from the
-- payout function using the service_role key, which bypasses RLS — so there is
-- deliberately NO anon insert/update/delete policy here.
drop policy if exists coupon_payouts_read on public.coupon_payouts;
create policy coupon_payouts_read on public.coupon_payouts for select using (true);

grant select on public.coupon_payouts to anon;
