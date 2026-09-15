-- Weekend Coupon — Champions League snapshot cache
-- Run ONCE in the SQL editor of the SHARED project (hsccirgnwfwxccwjmgjf).
-- Safe to re-run.
--
-- The UCL model is heavy (each club's domestic-league stats). Instead of
-- recomputing on every page load, the site stores a snapshot here and only
-- rebuilds it when it's more than ~40 minutes old. Two rows:
--   key = 'upcoming'  -> JSON array of not-yet-played fixtures + model lines
--   key = 'results'   -> JSON object {fixture_id: graded result} for the season
-- Writes come from the site build with the service key (bypasses RLS);
-- the anon key can only read.

create table if not exists public.coupon_ucl_cache (
  key          text primary key,
  payload      jsonb not null,
  refreshed_at timestamptz not null default now()
);

alter table public.coupon_ucl_cache enable row level security;

drop policy if exists coupon_ucl_cache_read on public.coupon_ucl_cache;
create policy coupon_ucl_cache_read on public.coupon_ucl_cache for select using (true);
-- no insert/update/delete policy: only the service key can write

grant select on public.coupon_ucl_cache to anon;
