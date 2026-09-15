-- Undo the merge — put the two accounts back, one wallet each.
-- Run once in the shared project's SQL editor.
--
-- After this:
--   HnLkicin'it  ->  wallet GRJG…KLiU  ->  its 30 picks (made 29 Aug)
--   Lglondon     ->  wallet 9pt9…WDXh  ->  its 25 picks (made 30 Aug)
--   Yo           ->  unchanged

begin;

-- 1. recreate Lglondon
insert into public.coupon_players (name) values ('Lglondon')
  on conflict (name) do nothing;

-- 2. move the 30-Aug picks back to Lglondon
--    (everything HnLkicin'it "owns" that was created on/after 30 Aug was Lglondon's)
update public.coupon_picks
   set player = 'Lglondon'
 where player = 'HnLkicin''it'
   and created_at >= '2026-08-30T00:00:00+00';

-- 3. point the 9pt9 wallet at Lglondon (GRJG stays with HnLkicin'it)
update public.coupon_wallets
   set player = 'Lglondon'
 where wallet = '9pt9QkgzSrB3jvP6N5BBSpNeLj15yL5aXUYtVP5kWDXh';

commit;
