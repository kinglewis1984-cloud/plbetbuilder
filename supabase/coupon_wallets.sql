-- Weekend Coupon — wallet links (SSB payouts)
-- Run once in the SQL editor of the SHARED project (football-fan-hub / hsccirgnwfwxccwjmgjf),
-- same place you ran coupon_picks.sql. Safe to re-run.

create table if not exists public.coupon_wallets (
  id         uuid primary key default gen_random_uuid(),
  player     text not null,
  wallet     text not null,            -- Solana address (base58)
  sig        text,                     -- base64 signature proving ownership
  msg        text,                     -- the message that was signed
  created_at timestamptz not null default now()
);

create index if not exists coupon_wallets_player_idx on public.coupon_wallets (player);
create index if not exists coupon_wallets_wallet_idx on public.coupon_wallets (wallet);

-- force created_at server-side (same helper the picks table uses)
drop trigger if exists coupon_wallets_stamp on public.coupon_wallets;
create trigger coupon_wallets_stamp before insert on public.coupon_wallets
  for each row execute function public.coupon_stamp();

alter table public.coupon_wallets enable row level security;

drop policy if exists coupon_wallets_read   on public.coupon_wallets;
drop policy if exists coupon_wallets_insert on public.coupon_wallets;
create policy coupon_wallets_read   on public.coupon_wallets for select using (true);
create policy coupon_wallets_insert on public.coupon_wallets for insert with check (true);
-- no update / delete: a link can be added, never edited or removed by the anon key.
-- To re-link a wallet a player just adds a new row; the latest row per player wins.

grant select, insert on public.coupon_wallets to anon;
