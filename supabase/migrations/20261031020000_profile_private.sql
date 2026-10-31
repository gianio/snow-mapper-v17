-- Vorname, Nachname, Telefonnummer: asked at sign-up, never public.
--
-- The app keeps them in the account's own metadata (auth.users
-- raw_user_meta_data: first_name, last_name, phone), which only the person
-- and the service role can read. This table mirrors them, so the owner can
-- query them (SQL editor, a later admin view) without digging through auth
-- metadata. The public profiles row never carries them.

create table if not exists public.profile_private (
  id uuid primary key references auth.users(id) on delete cascade,
  first_name text check (first_name is null or char_length(first_name) <= 60),
  last_name text check (last_name is null or char_length(last_name) <= 60),
  phone text check (phone is null or phone ~ '^\+[1-9][0-9]{7,14}$'),
  updated_at timestamptz not null default now()
);
alter table public.profile_private enable row level security;
-- each person reads their own row; writes come only from the trigger below
drop policy if exists profile_private_read_own on public.profile_private;
create policy profile_private_read_own on public.profile_private
  for select to authenticated using (id = (select auth.uid()));

create or replace function public.profile_private_sync() returns trigger
  language plpgsql security definer set search_path = public, pg_temp
as $$
declare
  m jsonb := coalesce(new.raw_user_meta_data, '{}'::jsonb);
  ph text := nullif(btrim(m->>'phone'), '');
begin
  if tg_op = 'UPDATE' and new.raw_user_meta_data is not distinct from old.raw_user_meta_data then
    return new;
  end if;
  if ph is not null and ph !~ '^\+[1-9][0-9]{7,14}$' then ph := null; end if;
  insert into public.profile_private (id, first_name, last_name, phone, updated_at)
  values (new.id, left(nullif(btrim(m->>'first_name'), ''), 60), left(nullif(btrim(m->>'last_name'), ''), 60), ph, now())
  on conflict (id) do update
    set first_name = excluded.first_name, last_name = excluded.last_name,
        phone = excluded.phone, updated_at = now();
  return new;
end $$;
revoke execute on function public.profile_private_sync() from public, anon, authenticated;

drop trigger if exists on_auth_user_private on auth.users;
create trigger on_auth_user_private after insert or update of raw_user_meta_data on auth.users
  for each row execute function public.profile_private_sync();

-- the accounts that exist already
insert into public.profile_private (id, first_name, last_name, phone)
select u.id,
       left(nullif(btrim(u.raw_user_meta_data->>'first_name'), ''), 60),
       left(nullif(btrim(u.raw_user_meta_data->>'last_name'), ''), 60),
       case when u.raw_user_meta_data->>'phone' ~ '^\+[1-9][0-9]{7,14}$' then u.raw_user_meta_data->>'phone' end
from auth.users u
on conflict (id) do nothing;
