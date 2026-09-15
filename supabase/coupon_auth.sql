-- Weekend Coupon — account PINs, recovery codes, PIN-gated wallet linking
-- ---------------------------------------------------------------------------
-- Run ONCE in the SQL editor of the SHARED project
-- (hsccirgnwfwxccwjmgjf — the one Football Fan Hub / Shadow Takedown use),
-- the same place you ran coupon_picks.sql / coupon_wallets.sql.
-- Safe to re-run (everything is IF NOT EXISTS / CREATE OR REPLACE).
--
-- What it does
--   * adds a PIN to every account  -> username + PIN to sign in on a new device
--   * gives each new account a one-time recovery code (reset a forgotten PIN)
--   * linking a wallet now REQUIRES the PIN  -> nobody can attach their wallet
--     to someone else's account and steal that account's SSB
--   * 5 wrong PINs in 15 minutes = that username is locked for 15 minutes
--   * the anon key can no longer write coupon_players / coupon_wallets directly
--     or read the PIN hashes — all of that goes through the functions below
--
-- Existing accounts (HnLkicin'it, Lglondon, Yo) have no PIN yet: the FIRST
-- time each signs in, whatever PIN is entered becomes that account's PIN.
-- >>> Sign into your own accounts right after running this, to claim them. <<<

create extension if not exists pgcrypto;

-- ── columns on coupon_players ──────────────────────────────────────────────
alter table public.coupon_players add column if not exists pin_hash      text;
alter table public.coupon_players add column if not exists recovery_hash text;

-- ── failed-login throttle ─────────────────────────────────────────────────
create table if not exists public.coupon_login_attempts (
  id   bigint generated always as identity primary key,
  name text not null,
  ok   boolean not null,
  at   timestamptz not null default now()
);
create index if not exists coupon_login_attempts_idx
  on public.coupon_login_attempts (name, at);

-- ── helpers ───────────────────────────────────────────────────────────────
create or replace function public._coupon_norm(p text) returns text
  language sql immutable as
$$ select btrim(regexp_replace(coalesce(p, ''), '\s+', ' ', 'g')) $$;

create or replace function public._coupon_locked(p_name text) returns boolean
  language sql stable as
$$
  select count(*) >= 5
    from public.coupon_login_attempts
   where name = p_name and ok = false
     and at > now() - interval '15 minutes'
$$;

-- ── sign up: create an account with a PIN, return a one-time recovery code ──
create or replace function public.coupon_signup(p_name text, p_pin text)
  returns text
  language plpgsql security definer
  set search_path = public, extensions, pg_temp as
$$
declare v_name text; v_code text;
begin
  v_name := public._coupon_norm(p_name);
  if length(v_name) < 2 or length(v_name) > 20 then raise exception 'bad_name'; end if;
  if p_pin !~ '^[0-9]{4,6}$'                    then raise exception 'bad_pin';  end if;
  if exists (select 1 from public.coupon_players where name = v_name) then
    raise exception 'taken';
  end if;
  v_code := upper(encode(gen_random_bytes(6), 'hex'));   -- 12 hex chars
  insert into public.coupon_players (name, pin_hash, recovery_hash)
    values (v_name, crypt(p_pin, gen_salt('bf')), crypt(v_code, gen_salt('bf')));
  return substr(v_code, 1, 4) || '-' || substr(v_code, 5, 4) || '-' || substr(v_code, 9, 4);
end
$$;

-- ── sign in: username + PIN -> {ok, reason} ────────────────────────────────
-- legacy accounts (pin_hash null) claim their PIN on first valid-format entry
create or replace function public.coupon_signin(p_name text, p_pin text)
  returns jsonb
  language plpgsql security definer
  set search_path = public, extensions, pg_temp as
$$
declare v_name text; v_hash text; v_id uuid;
begin
  v_name := public._coupon_norm(p_name);
  if public._coupon_locked(v_name) then
    return jsonb_build_object('ok', false, 'reason', 'locked');
  end if;
  select id, pin_hash into v_id, v_hash from public.coupon_players where name = v_name;
  if v_id is null then
    insert into public.coupon_login_attempts(name, ok) values (v_name, false);
    return jsonb_build_object('ok', false, 'reason', 'bad');
  end if;
  if v_hash is null then                     -- legacy account: claim the PIN now
    if p_pin !~ '^[0-9]{4,6}$' then
      insert into public.coupon_login_attempts(name, ok) values (v_name, false);
      return jsonb_build_object('ok', false, 'reason', 'bad');
    end if;
    update public.coupon_players set pin_hash = crypt(p_pin, gen_salt('bf')) where id = v_id;
    insert into public.coupon_login_attempts(name, ok) values (v_name, true);
    return jsonb_build_object('ok', true, 'claimed', true);
  end if;
  if crypt(p_pin, v_hash) = v_hash then
    insert into public.coupon_login_attempts(name, ok) values (v_name, true);
    return jsonb_build_object('ok', true);
  end if;
  insert into public.coupon_login_attempts(name, ok) values (v_name, false);
  return jsonb_build_object('ok', false, 'reason', 'bad');
end
$$;

-- ── recover: username + recovery code -> set a new PIN ─────────────────────
create or replace function public.coupon_recover(p_name text, p_code text, p_new_pin text)
  returns jsonb
  language plpgsql security definer
  set search_path = public, extensions, pg_temp as
$$
declare v_name text; v_rhash text; v_id uuid; v_code text;
begin
  v_name := public._coupon_norm(p_name);
  v_code := upper(regexp_replace(coalesce(p_code, ''), '[^0-9a-fA-F]', '', 'g'));
  if public._coupon_locked(v_name) then
    return jsonb_build_object('ok', false, 'reason', 'locked');
  end if;
  if p_new_pin !~ '^[0-9]{4,6}$' then
    return jsonb_build_object('ok', false, 'reason', 'bad_pin');
  end if;
  select id, recovery_hash into v_id, v_rhash from public.coupon_players where name = v_name;
  if v_id is null or v_rhash is null or crypt(v_code, v_rhash) <> v_rhash then
    insert into public.coupon_login_attempts(name, ok) values (v_name, false);
    return jsonb_build_object('ok', false, 'reason', 'bad');
  end if;
  update public.coupon_players set pin_hash = crypt(p_new_pin, gen_salt('bf')) where id = v_id;
  insert into public.coupon_login_attempts(name, ok) values (v_name, true);
  return jsonb_build_object('ok', true);
end
$$;

-- ── change PIN: username + old PIN -> new PIN ──────────────────────────────
create or replace function public.coupon_set_pin(p_name text, p_old_pin text, p_new_pin text)
  returns jsonb
  language plpgsql security definer
  set search_path = public, extensions, pg_temp as
$$
declare v_name text; v_hash text; v_id uuid;
begin
  v_name := public._coupon_norm(p_name);
  if public._coupon_locked(v_name) then
    return jsonb_build_object('ok', false, 'reason', 'locked');
  end if;
  if p_new_pin !~ '^[0-9]{4,6}$' then
    return jsonb_build_object('ok', false, 'reason', 'bad_pin');
  end if;
  select id, pin_hash into v_id, v_hash from public.coupon_players where name = v_name;
  if v_id is null then
    return jsonb_build_object('ok', false, 'reason', 'bad');
  end if;
  if v_hash is not null and crypt(p_old_pin, v_hash) <> v_hash then
    insert into public.coupon_login_attempts(name, ok) values (v_name, false);
    return jsonb_build_object('ok', false, 'reason', 'bad');
  end if;
  update public.coupon_players set pin_hash = crypt(p_new_pin, gen_salt('bf')) where id = v_id;
  return jsonb_build_object('ok', true);
end
$$;

-- ── link wallet: PIN-gated. Replaces the direct insert into coupon_wallets ──
create or replace function public.coupon_link_wallet(
    p_name text, p_pin text, p_wallet text,
    p_sig text default null, p_msg text default null)
  returns jsonb
  language plpgsql security definer
  set search_path = public, extensions, pg_temp as
$$
declare v_name text; v_hash text; v_id uuid; v_owner text;
begin
  v_name := public._coupon_norm(p_name);
  if public._coupon_locked(v_name) then
    return jsonb_build_object('ok', false, 'reason', 'locked');
  end if;
  if coalesce(p_wallet, '') !~ '^[1-9A-HJ-NP-Za-km-z]{32,44}$' then
    return jsonb_build_object('ok', false, 'reason', 'bad_wallet');
  end if;
  select id, pin_hash into v_id, v_hash from public.coupon_players where name = v_name;
  if v_id is null then
    return jsonb_build_object('ok', false, 'reason', 'bad');
  end if;
  if v_hash is null then                       -- legacy account: claim the PIN now
    if p_pin !~ '^[0-9]{4,6}$' then
      insert into public.coupon_login_attempts(name, ok) values (v_name, false);
      return jsonb_build_object('ok', false, 'reason', 'bad');
    end if;
    update public.coupon_players set pin_hash = crypt(p_pin, gen_salt('bf')) where id = v_id;
  elsif crypt(p_pin, v_hash) <> v_hash then
    insert into public.coupon_login_attempts(name, ok) values (v_name, false);
    return jsonb_build_object('ok', false, 'reason', 'bad');
  end if;
  select player into v_owner from public.coupon_wallets
    where wallet = p_wallet order by created_at desc limit 1;
  if v_owner is not null then
    if v_owner = v_name then
      return jsonb_build_object('ok', true);          -- already linked to you
    end if;
    return jsonb_build_object('ok', false, 'reason', 'wallet_taken');
  end if;
  insert into public.coupon_wallets (player, wallet, sig, msg)
    values (v_name, p_wallet, p_sig, p_msg);
  return jsonb_build_object('ok', true);
end
$$;

-- ── generate a fresh recovery code for the signed-in account ──────────────
-- lets accounts made before the PIN system (or anyone who lost their code)
-- get a new one. Overwrites the old code — the previous one stops working.
create or replace function public.coupon_new_recovery(p_name text, p_pin text)
  returns text
  language plpgsql security definer
  set search_path = public, extensions, pg_temp as
$$
declare v_name text; v_hash text; v_id uuid; v_code text;
begin
  v_name := public._coupon_norm(p_name);
  if public._coupon_locked(v_name) then raise exception 'locked'; end if;
  select id, pin_hash into v_id, v_hash from public.coupon_players where name = v_name;
  if v_id is null then raise exception 'bad'; end if;
  if v_hash is null then                        -- legacy account: claim the PIN now
    if p_pin !~ '^[0-9]{4,6}$' then
      insert into public.coupon_login_attempts(name, ok) values (v_name, false);
      raise exception 'bad';
    end if;
    update public.coupon_players set pin_hash = crypt(p_pin, gen_salt('bf')) where id = v_id;
  elsif crypt(p_pin, v_hash) <> v_hash then
    insert into public.coupon_login_attempts(name, ok) values (v_name, false);
    raise exception 'bad';
  end if;
  v_code := upper(encode(gen_random_bytes(6), 'hex'));
  update public.coupon_players set recovery_hash = crypt(v_code, gen_salt('bf')) where id = v_id;
  return substr(v_code, 1, 4) || '-' || substr(v_code, 5, 4) || '-' || substr(v_code, 9, 4);
end
$$;

-- ── lock down direct writes / hash reads ──────────────────────────────────
revoke insert on public.coupon_players  from anon;   -- signup  -> coupon_signup()
revoke insert on public.coupon_wallets  from anon;   -- linking -> coupon_link_wallet()

-- anon may still read the username list, but not the PIN / recovery hashes
revoke select on public.coupon_players from anon;
grant  select (id, name, created_at) on public.coupon_players to anon;

grant execute on function
  public.coupon_signup(text, text),
  public.coupon_signin(text, text),
  public.coupon_recover(text, text, text),
  public.coupon_set_pin(text, text, text),
  public.coupon_link_wallet(text, text, text, text, text),
  public.coupon_new_recovery(text, text)
  to anon;

-- coupon_wallets: SELECT stays (needed for "Sign in with Phantom" + payout sheet)
-- coupon_picks:   unchanged
