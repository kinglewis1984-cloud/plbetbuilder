-- Add-on: "Get a recovery code" button for accounts that never had one.
-- Run ONCE in the SQL editor of the SHARED project (hsccirgnwfwxccwjmgjf).
-- Safe to re-run. (This is also already included in coupon_auth.sql.)

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
  if v_hash is null then
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

grant execute on function public.coupon_new_recovery(text, text) to anon;
