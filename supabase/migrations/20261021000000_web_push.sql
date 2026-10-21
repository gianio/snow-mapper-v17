-- Web push: a device subscribes (push_subscriptions), and new comments,
-- likes, direct messages and followers are pushed to the person they are
-- for, by the edge function push-notify.
--
-- * The VAPID key pair is made by push-notify on first use and kept in
--   push_config, which no API role can read; the app gets the public key
--   from push_public_key().
-- * AFTER INSERT triggers call push-notify through pg_net with the table
--   and the row id; the function reads the row itself with the service
--   role, so the call carries no text that could be forged. The shared
--   secret lives in the vault and is checked by push_hook_ok(), which only
--   the service role may run.
-- * push_enabled on the profile decides; a device stays subscribed while
--   the switch is off but gets nothing.

create extension if not exists pg_net with schema extensions;

create table if not exists public.push_subscriptions (
  endpoint text primary key check (char_length(endpoint) <= 1000),
  user_id uuid not null references auth.users(id) on delete cascade,
  p256dh text not null check (char_length(p256dh) <= 200),
  auth text not null check (char_length(auth) <= 100),
  user_agent text check (char_length(user_agent) <= 300),
  created_at timestamptz not null default now()
);
create index if not exists push_subscriptions_user on public.push_subscriptions (user_id);
alter table public.push_subscriptions enable row level security;

create policy push_sub_select_own on public.push_subscriptions
  for select to authenticated using ((select auth.uid()) = user_id);
create policy push_sub_delete_own on public.push_subscriptions
  for delete to authenticated using ((select auth.uid()) = user_id);

-- Subscribing goes through this function: the endpoint is the device's,
-- and when someone else signs in on the same browser the subscription
-- moves to them (an endpoint URL is only known to the device that holds it).
create or replace function public.push_subscribe(p_endpoint text, p_p256dh text, p_auth text, p_ua text)
  returns void language plpgsql security definer set search_path = public, pg_temp
as $$
begin
  if auth.uid() is null then raise exception 'not signed in'; end if;
  if p_endpoint !~ '^https://' then raise exception 'bad endpoint'; end if;
  insert into push_subscriptions (endpoint, user_id, p256dh, auth, user_agent)
  values (p_endpoint, auth.uid(), p_p256dh, p_auth, left(p_ua, 300))
  on conflict (endpoint) do update
    set user_id = excluded.user_id, p256dh = excluded.p256dh, auth = excluded.auth,
        user_agent = excluded.user_agent, created_at = now();
end $$;
revoke execute on function public.push_subscribe(text, text, text, text) from public, anon;
grant execute on function public.push_subscribe(text, text, text, text) to authenticated;

create or replace trigger rl_push_sub before insert on public.push_subscriptions
  for each row execute function public.rate_limit('user_id', '20', '1 day');

-- one row: the VAPID keys (no policies: API roles see nothing)
create table if not exists public.push_config (
  id boolean primary key default true check (id),
  vapid_public text not null,
  vapid_private text not null,
  created_at timestamptz not null default now()
);
alter table public.push_config enable row level security;

create or replace function public.push_public_key() returns text
  language sql stable security definer set search_path = public, pg_temp
as $$ select vapid_public from push_config where id $$;
grant execute on function public.push_public_key() to anon, authenticated;

-- what was pushed already: a like taken back and given again, or an
-- unfollow and follow, is pushed once (push-notify keeps it, 30 days)
create table if not exists public.push_log (
  key text primary key,
  created_at timestamptz not null default now()
);
alter table public.push_log enable row level security;

-- the hook secret, made once inside the database
do $$
begin
  if not exists (select 1 from vault.secrets where name = 'push_hook_secret') then
    perform vault.create_secret(encode(extensions.gen_random_bytes(32), 'hex'), 'push_hook_secret');
  end if;
end $$;

create or replace function public.push_hook_ok(s text) returns boolean
  language sql stable security definer set search_path = public, pg_temp
as $$ select exists (select 1 from vault.decrypted_secrets
                     where name = 'push_hook_secret' and decrypted_secret = s) $$;
revoke execute on function public.push_hook_ok(text) from public, anon, authenticated;
grant execute on function public.push_hook_ok(text) to service_role;

create or replace function public.push_hook() returns trigger
  language plpgsql security definer set search_path = public, pg_temp
as $$
declare
  j jsonb := to_jsonb(new);                     -- not every table has an id
  rid text;
begin
  rid := case tg_table_name
           when 'report_reactions' then (j->>'report_id') || ':' || (j->>'user_id')
           when 'follows' then (j->>'follower_id') || ':' || (j->>'following_id')
           else j->>'id' end;
  perform net.http_post(
    url := 'https://gdtxwowcqtbdkcoksivb.supabase.co/functions/v1/push-notify',
    body := jsonb_build_object('table', tg_table_name, 'id', rid),
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-push-secret', (select decrypted_secret from vault.decrypted_secrets
                        where name = 'push_hook_secret')),
    timeout_milliseconds := 5000);
  return null;
exception when others then
  return null;                                  -- a push must never block the write
end $$;
revoke execute on function public.push_hook() from public, anon, authenticated;

create or replace trigger push_comment after insert on public.report_comments
  for each row execute function public.push_hook();
create or replace trigger push_reaction after insert on public.report_reactions
  for each row execute function public.push_hook();
create or replace trigger push_dm after insert on public.dm_messages
  for each row execute function public.push_hook();
create or replace trigger push_follow after insert on public.follows
  for each row execute function public.push_hook();

-- the profile's «Test-Benachrichtigung»: a push to the caller's own devices
create or replace function public.push_test() returns void
  language plpgsql security definer set search_path = public, pg_temp
as $$
begin
  if auth.uid() is null then raise exception 'not signed in'; end if;
  perform net.http_post(
    url := 'https://gdtxwowcqtbdkcoksivb.supabase.co/functions/v1/push-notify',
    body := jsonb_build_object('table', 'test', 'id', auth.uid()),
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-push-secret', (select decrypted_secret from vault.decrypted_secrets
                        where name = 'push_hook_secret')),
    timeout_milliseconds := 5000);
end $$;
revoke execute on function public.push_test() from public, anon;
grant execute on function public.push_test() to authenticated;
