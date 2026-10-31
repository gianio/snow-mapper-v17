-- Snowmapper Pro: who may use the paid features, and until when.
-- (needs 20261030000000_invites.sql: app_settings, profiles.cohort / role)
--
-- * entitlements: one row per person with Pro -- from a purchase (Stripe on
--   the web, later Apple via RevenueCat), a gift, or set by hand. Written only
--   by the payment webhooks (service role) and the owner; each person reads
--   their own row.
-- * is_pro(): Pro if the row is still valid, or the person is the owner, or
--   they came in with an invite code (Family & Friends, testers) while
--   app_settings.tester_pro_until has not passed.
-- * app_settings.paywall: 'off' (default -- everything stays open, nothing
--   changes for anyone) or 'on' (the app asks for Pro at the gated features).
--   The owner switches it in the app or here.

create table if not exists public.entitlements (
  user_id uuid primary key references auth.users(id) on delete cascade,
  plan text not null default 'pro' check (plan in ('pro')),
  source text not null check (source in ('stripe','apple','gift','admin')),
  product text check (product is null or char_length(product) <= 40),       -- 'season' | 'month' | …
  valid_until timestamptz not null,
  stripe_customer text,
  stripe_subscription text,
  updated_at timestamptz not null default now()
);
alter table public.entitlements enable row level security;
drop policy if exists entitlements_read_own on public.entitlements;
create policy entitlements_read_own on public.entitlements
  for select to authenticated using (user_id = (select auth.uid()));

insert into public.app_settings (key, value) values ('paywall', '"off"') on conflict (key) do nothing;
insert into public.app_settings (key, value) values ('tester_pro_until', '"2027-05-31"') on conflict (key) do nothing;

-- the person's Pro state in one call: {pro, source, until, paywall}
create or replace function public.pro_state() returns jsonb
  language plpgsql stable security definer set search_path = public, pg_temp
as $$
declare
  uid uuid := auth.uid();
  e public.entitlements;
  co text; ro text;
  pw text := coalesce((select value #>> '{}' from public.app_settings where key = 'paywall'), 'off');
  tu timestamptz := coalesce((select (value #>> '{}')::timestamptz from public.app_settings where key = 'tester_pro_until'), now() - interval '1 day');
begin
  if uid is null then return jsonb_build_object('pro', false, 'paywall', pw); end if;
  select cohort, role into co, ro from public.profiles where id = uid;
  if ro = 'admin' then return jsonb_build_object('pro', true, 'source', 'admin', 'paywall', pw); end if;
  select * into e from public.entitlements where user_id = uid;
  if found and e.valid_until > now() then
    return jsonb_build_object('pro', true, 'source', e.source, 'product', e.product, 'until', e.valid_until, 'paywall', pw);
  end if;
  if co is not null and tu > now() then
    return jsonb_build_object('pro', true, 'source', 'tester', 'until', tu, 'paywall', pw);
  end if;
  return jsonb_build_object('pro', false, 'paywall', pw,
                            'expired', case when found then e.valid_until end);
end $$;
revoke execute on function public.pro_state() from public, anon;
grant execute on function public.pro_state() to authenticated;

-- for RLS and server checks (the tile worker, later)
create or replace function public.is_pro(uid uuid default auth.uid()) returns boolean
  language sql stable security definer set search_path = public, pg_temp
as $$
  select exists (select 1 from public.profiles where id = uid and role = 'admin')
      or exists (select 1 from public.entitlements where user_id = uid and valid_until > now())
      or (exists (select 1 from public.profiles where id = uid and cohort is not null)
          and coalesce((select (value #>> '{}')::timestamptz from public.app_settings where key = 'tester_pro_until'), now()) > now())
$$;
revoke execute on function public.is_pro(uuid) from public, anon;
grant execute on function public.is_pro(uuid) to authenticated;

-- the owner: switch the paywall, give Pro by hand (a gift, a partner)
create or replace function public.paywall_set(mode text) returns void
  language plpgsql security definer set search_path = public, pg_temp
as $$
begin
  if not public.is_admin() then raise exception 'forbidden'; end if;
  if mode not in ('on','off') then raise exception 'bad mode'; end if;
  insert into public.app_settings (key, value, updated_at) values ('paywall', to_jsonb(mode), now())
  on conflict (key) do update set value = excluded.value, updated_at = now();
end $$;

create or replace function public.pro_gift(uid uuid, days integer) returns void
  language plpgsql security definer set search_path = public, pg_temp
as $$
begin
  if not public.is_admin() then raise exception 'forbidden'; end if;
  if days not between 1 and 3650 then raise exception 'bad days'; end if;
  insert into public.entitlements (user_id, source, product, valid_until)
  values (uid, 'gift', 'gift', now() + make_interval(days => days))
  on conflict (user_id) do update
    set valid_until = greatest(public.entitlements.valid_until, now()) + make_interval(days => days),
        source = case when public.entitlements.source in ('stripe','apple') then public.entitlements.source else 'gift' end,
        updated_at = now();
end $$;
revoke execute on function public.paywall_set(text), public.pro_gift(uuid, integer) from public, anon;
grant execute on function public.paywall_set(text), public.pro_gift(uuid, integer) to authenticated;
