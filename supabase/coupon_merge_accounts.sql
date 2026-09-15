-- One-off cleanup: merge the test accounts into "HnLkicin'it" and lock
-- "one wallet = one username". Run once in the shared project's SQL editor.
--
-- After this:
--   * HnLkicin'it owns all picks that were under Lglondon / HnLkicin'it1 / HnLkicin'it2
--   * both wallets (9pt9…WDXh and GRJG…KLiU) are linked to HnLkicin'it
--   * Yo is left as-is (separate player)
--   * a wallet can never be linked to two names again

begin;

-- 1. move picks
update public.coupon_picks
   set player = 'HnLkicin''it'
 where player in ('Lglondon', 'HnLkicin''it1', 'HnLkicin''it2');

-- 2. wipe the messy wallet links, re-add two clean ones
delete from public.coupon_wallets;
insert into public.coupon_wallets (player, wallet) values
  ('HnLkicin''it', '9pt9QkgzSrB3jvP6N5BBSpNeLj15yL5aXUYtVP5kWDXh'),
  ('HnLkicin''it', 'GRJGJZBMbG22sYFpJNQMTFLWHgjJnCohmcLQTzfnKLiU');

-- 3. drop the dead usernames
delete from public.coupon_players
 where name in ('Lglondon', 'HnLkicin''it1', 'HnLkicin''it2');

-- 4. lock one-wallet-one-username
create unique index if not exists coupon_wallets_wallet_uk
  on public.coupon_wallets (wallet);

commit;
