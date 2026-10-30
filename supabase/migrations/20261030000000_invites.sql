-- Invitations: Family & Friends and the first testers.
--
-- * The owner (profiles.role = 'admin') hands out codes from the app
--   (Profil › Einstellungen › Einladungen): FF-XXXX-XXXX for Family &
--   Friends, BETA-XXXX-XXXX for testers. A code can be good for one person or
--   a few, and can run out on a date.
-- * Whoever redeems one is in that group for good (profiles.cohort). The app
--   greets them once and shows the group as a small badge by their name.
-- * app_settings.access decides who may post: 'open' (anyone signed in) or
--   'invite' (only people in a group). The app shows a code screen in the
--   'invite' mode; the trigger below is what actually holds a post back.
-- * Everyone who had an account before this is a tester ('beta'); the owner
--   is 'team' and admin.

alter table public.profiles
  add column if not exists cohort text,
  add column if not exists role text,
  add column if not exists invited_at timestamptz,
  add column if not exists invite_code text;
alter table public.profiles drop constraint if exists profiles_cohort_chk;
alter table public.profiles add constraint profiles_cohort_chk check (cohort is null or cohort in ('ff','beta','team'));
alter table public.profiles drop constraint if exists profiles_role_chk;
alter table public.profiles add constraint profiles_role_chk check (role is null or role in ('admin'));

-- the group and the role are not the person's to change
create or replace function public.profiles_guard() returns trigger
  language plpgsql set search_path = public, pg_temp
as $$
begin
  if public.is_api_role() and (
       new.id is distinct from old.id or new.email is distinct from old.email
    or new.created_at is distinct from old.created_at
    or new.webauthn_credentials is distinct from old.webauthn_credentials
    or new.cohort is distinct from old.cohort or new.role is distinct from old.role
    or new.invited_at is distinct from old.invited_at or new.invite_code is distinct from old.invite_code) then
    raise exception 'this profile field cannot be changed';
  end if;
  return new;
end $$;

create table if not exists public.invite_codes (
  code text primary key check (code ~ '^[A-Z0-9-]{4,24}$'),
  cohort text not null check (cohort in ('ff','beta','team')),
  label text check (label is null or char_length(label) <= 80),
  max_uses integer not null default 1 check (max_uses between 1 and 500),
  uses integer not null default 0,
  expires_at timestamptz,
  revoked boolean not null default false,
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);
-- no policies: only the functions below read or write it
alter table public.invite_codes enable row level security;

create table if not exists public.invite_attempts (
  user_id uuid not null references auth.users(id) on delete cascade,
  at timestamptz not null default now()
);
create index if not exists invite_attempts_user on public.invite_attempts (user_id, at);
alter table public.invite_attempts enable row level security;

create table if not exists public.app_settings (
  key text primary key,
  value jsonb not null,
  updated_at timestamptz not null default now()
);
alter table public.app_settings enable row level security;
drop policy if exists app_settings_read on public.app_settings;
create policy app_settings_read on public.app_settings for select to anon, authenticated using (true);
insert into public.app_settings (key, value) values ('access', '"open"') on conflict (key) do nothing;

create or replace function public.is_admin() returns boolean
  language sql stable security definer set search_path = public, pg_temp
as $$ select exists (select 1 from public.profiles where id = auth.uid() and role = 'admin') $$;

-- may this person post? (open, or in a group)
create or replace function public.access_ok() returns boolean
  language sql stable security definer set search_path = public, pg_temp
as $$
  select coalesce((select value #>> '{}' from public.app_settings where key = 'access'), 'open') = 'open'
      or exists (select 1 from public.profiles where id = auth.uid() and cohort is not null)
$$;

-- runs as the caller, so is_api_role() sees who is writing
create or replace function public.access_guard() returns trigger
  language plpgsql set search_path = public, pg_temp
as $$
begin
  if public.is_api_role() and not public.access_ok() then
    raise exception 'Snowmapper ist in der geschlossenen Testphase – zum Posten braucht es einen Einladungscode.';
  end if;
  return new;
end $$;
drop trigger if exists reports_access on public.reports;
create trigger reports_access before insert on public.reports for each row execute function public.access_guard();
drop trigger if exists report_comments_access on public.report_comments;
create trigger report_comments_access before insert on public.report_comments for each row execute function public.access_guard();

-- redeem a code (10 tries an hour per person: codes cannot be guessed)
create or replace function public.invite_redeem(c text) returns jsonb
  language plpgsql security definer set search_path = public, pg_temp
as $$
declare
  k text := upper(regexp_replace(coalesce(c, ''), '\s', '', 'g'));
  r public.invite_codes;
  cur text;
begin
  if auth.uid() is null then raise exception 'not signed in'; end if;
  select cohort into cur from public.profiles where id = auth.uid();
  if cur is not null then return jsonb_build_object('ok', true, 'cohort', cur, 'already', true); end if;
  if (select count(*) from public.invite_attempts where user_id = auth.uid() and at > now() - interval '1 hour') >= 10 then
    return jsonb_build_object('ok', false, 'reason', 'rate');
  end if;
  insert into public.invite_attempts (user_id) values (auth.uid());
  select * into r from public.invite_codes where code = k for update;
  if not found or r.revoked or (r.expires_at is not null and r.expires_at < now()) then
    return jsonb_build_object('ok', false, 'reason', 'invalid');
  end if;
  if r.uses >= r.max_uses then return jsonb_build_object('ok', false, 'reason', 'used'); end if;
  update public.invite_codes set uses = uses + 1 where code = k;
  update public.profiles set cohort = r.cohort, invited_at = now(), invite_code = k where id = auth.uid();
  return jsonb_build_object('ok', true, 'cohort', r.cohort);
end $$;

-- the owner's tools
create or replace function public.invite_create(p_cohort text, p_n integer default 1, p_uses integer default 1,
                                                p_label text default null, p_days integer default null)
  returns setof text
  language plpgsql security definer set search_path = public, pg_temp
as $$
declare
  abc constant text := 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
  pre text := case p_cohort when 'ff' then 'FF' when 'beta' then 'BETA' else 'TEAM' end;
  k text; i integer; j integer;
begin
  if not public.is_admin() then raise exception 'forbidden'; end if;
  if p_cohort not in ('ff','beta','team') or p_n not between 1 and 50 then raise exception 'bad request'; end if;
  for i in 1..p_n loop
    loop
      k := pre || '-';
      for j in 1..8 loop
        k := k || substr(abc, 1 + floor(random() * length(abc))::int, 1);
        if j = 4 then k := k || '-'; end if;
      end loop;
      exit when not exists (select 1 from public.invite_codes where code = k);
    end loop;
    insert into public.invite_codes (code, cohort, label, max_uses, expires_at, created_by)
    values (k, p_cohort, nullif(btrim(p_label), ''), greatest(1, least(500, coalesce(p_uses, 1))),
            case when p_days is null then null else now() + make_interval(days => p_days) end, auth.uid());
    return next k;
  end loop;
end $$;

create or replace function public.invite_list()
  returns table (code text, cohort text, label text, max_uses integer, uses integer, expires_at timestamptz,
                 revoked boolean, created_at timestamptz, used_by text[])
  language plpgsql stable security definer set search_path = public, pg_temp
as $$
begin
  if not public.is_admin() then raise exception 'forbidden'; end if;
  return query
    select i.code, i.cohort, i.label, i.max_uses, i.uses, i.expires_at, i.revoked, i.created_at,
           array(select p.username from public.profiles p where p.invite_code = i.code order by p.invited_at)
    from public.invite_codes i order by i.created_at desc limit 200;
end $$;

create or replace function public.invite_revoke(c text) returns void
  language plpgsql security definer set search_path = public, pg_temp
as $$
begin
  if not public.is_admin() then raise exception 'forbidden'; end if;
  update public.invite_codes set revoked = true where code = c;
end $$;

create or replace function public.access_set(mode text) returns void
  language plpgsql security definer set search_path = public, pg_temp
as $$
begin
  if not public.is_admin() then raise exception 'forbidden'; end if;
  if mode not in ('open','invite') then raise exception 'bad mode'; end if;
  insert into public.app_settings (key, value, updated_at) values ('access', to_jsonb(mode), now())
  on conflict (key) do update set value = excluded.value, updated_at = now();
end $$;

-- put someone into a group (or take them out) by hand
create or replace function public.cohort_set(uid uuid, c text) returns void
  language plpgsql security definer set search_path = public, pg_temp
as $$
begin
  if not public.is_admin() then raise exception 'forbidden'; end if;
  if c is not null and c not in ('ff','beta','team') then raise exception 'bad cohort'; end if;
  update public.profiles set cohort = c, invited_at = coalesce(invited_at, now()) where id = uid;
end $$;

revoke execute on function public.invite_redeem(text), public.invite_create(text, integer, integer, text, integer),
  public.invite_list(), public.invite_revoke(text), public.access_set(text), public.cohort_set(uuid, text)
  from public, anon;
grant execute on function public.invite_redeem(text), public.invite_create(text, integer, integer, text, integer),
  public.invite_list(), public.invite_revoke(text), public.access_set(text), public.cohort_set(uuid, text)
  to authenticated;

-- who was here before: the first testers; the owner runs it
update public.profiles set cohort = 'beta', invited_at = coalesce(invited_at, now()) where cohort is null;
update public.profiles set role = 'admin', cohort = 'team' where username = 'gianio';
