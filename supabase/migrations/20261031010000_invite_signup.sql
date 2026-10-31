-- Closed phase, enforced at sign-up (needs 20261030000000_invites.sql first).
--
-- With app_settings.access = 'invite', a new account must carry a valid
-- invite code in its sign-up data: the app sends it as
-- raw_user_meta_data.invite. The code is checked and used up in the same
-- transaction that creates the account, so a one-person code admits exactly
-- one account -- and nobody can register first and look for a code later.
-- Without a valid code the insert into auth.users fails; Supabase answers
-- "Database error saving new user", which the app turns into "code invalid
-- or used up".
--
-- In the 'open' mode a code is optional and still puts the new account into
-- its group (badge), exactly like redeeming it later.

create or replace function public.handle_new_user() returns trigger
  language plpgsql security definer set search_path = public, pg_temp
as $$
declare
  un text := nullif(btrim(new.raw_user_meta_data->>'username'), '');
  k text := upper(regexp_replace(coalesce(new.raw_user_meta_data->>'invite', ''), '\s', '', 'g'));
  closed boolean := coalesce((select value #>> '{}' from app_settings where key = 'access'), 'open') = 'invite';
  r invite_codes;
  ok boolean := false;
begin
  if k <> '' then
    select * into r from invite_codes where code = k for update;
    ok := found and not r.revoked
          and (r.expires_at is null or r.expires_at > now())
          and r.uses < r.max_uses;
  end if;
  if closed and not ok then
    raise exception 'invite_required' using errcode = 'P0001';
  end if;

  if un is null or char_length(un) > 40
     or exists (select 1 from profiles where username = un) then
    un := 'user_' || left(new.id::text, 8);
  end if;
  -- an address already sitting on another profile must not block sign-up
  insert into profiles (id, username, email, cohort, invited_at, invite_code)
  values (new.id, un,
          case when exists (select 1 from profiles where email = new.email)
               then null else new.email end,
          case when ok then r.cohort end,
          case when ok then now() end,
          case when ok then k end)
  on conflict (id) do nothing;
  if ok then
    update invite_codes set uses = uses + 1 where code = k;
  end if;
  return new;
end $$;
revoke execute on function public.handle_new_user() from public, anon, authenticated;
