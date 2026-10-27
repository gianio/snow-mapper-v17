-- Favourite areas and tours, and a push when the forecast brings enough new
-- snow there.
--
-- * The app saves a favourite (a tour or a spot on the map) with its own
--   threshold (default 20 cm new snow in the next 48 h).
-- * Every live build writes data/alerts.json: the new snow of the next 24
--   and 48 h per grid cell. The edge function fav-alerts reads it every two
--   hours (pg_cron, through pg_net with the push_hook_secret of web_push),
--   and sends one push per person for the favourites over their threshold.
-- * A favourite is not alerted again within 36 h; a forecast older than
--   18 h sends nothing. push_enabled on the profile decides, as for every
--   other push.

create table if not exists public.favorites (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  kind text not null check (kind in ('tour','area')),
  ref text,
  name text not null check (char_length(name) between 1 and 120),
  lat double precision not null check (lat between 45 and 48.5),
  lon double precision not null check (lon between 5 and 11.5),
  elev integer,
  threshold_cm integer not null default 20 check (threshold_cm between 5 and 100),
  notify boolean not null default true,
  last_alert_at timestamptz,
  created_at timestamptz not null default now()
);
create unique index if not exists favorites_user_kind_ref on public.favorites (user_id, kind, ref) where ref is not null;
create index if not exists favorites_user on public.favorites (user_id);
alter table public.favorites enable row level security;
create policy favorites_own_select on public.favorites for select to authenticated using (user_id = (select auth.uid()));
create policy favorites_own_insert on public.favorites for insert to authenticated with check (user_id = (select auth.uid()));
create policy favorites_own_update on public.favorites for update to authenticated using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
create policy favorites_own_delete on public.favorites for delete to authenticated using (user_id = (select auth.uid()));

-- at most 50 per person
create or replace function public.favorites_cap() returns trigger language plpgsql security definer set search_path = public, pg_temp as $$
begin
  if (select count(*) from public.favorites where user_id = new.user_id) >= 50 then
    raise exception 'Maximal 50 Favoriten';
  end if;
  return new;
end $$;
create trigger favorites_cap before insert on public.favorites for each row execute function public.favorites_cap();
revoke execute on function public.favorites_cap() from public, anon, authenticated;

-- every two hours at :17 -- the live builds land at different times of day
create extension if not exists pg_cron;
select cron.unschedule(jobid) from cron.job where jobname = 'fav-alerts';
select cron.schedule('fav-alerts', '17 */2 * * *', $job$
  select net.http_post(
    url := 'https://gdtxwowcqtbdkcoksivb.supabase.co/functions/v1/fav-alerts',
    body := '{}'::jsonb,
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-push-secret', (select decrypted_secret from vault.decrypted_secrets where name = 'push_hook_secret')),
    timeout_milliseconds := 30000)
$job$);
