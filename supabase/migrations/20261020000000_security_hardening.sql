-- Security hardening after the October review.
--
-- * Direct messages: only the recipient may touch a message, and only its
--   read_at; threads can no longer be re-pointed by their members.
-- * Profiles: the e-mail column is no longer writable by the user (it could
--   be set to someone else's address and block their sign-up); a taken
--   username at sign-up falls back to user_xxxx instead of failing.
-- * Reports: user_id, created_at and flagged are server-owned; inserts get
--   created_at = now(), so the 20-per-day limit can't be dodged by
--   backdating.
-- * Rate limits for comments, DMs, follows, flags, reactions, feedback.
-- * Comments, conditions and reactions of a post are only readable when the
--   post itself is (they were public even for private posts).
-- * Length limits on free text.
--
-- Written with ALTER POLICY / CREATE OR REPLACE TRIGGER (no DROPs), as
-- applied to the live project in three steps on 2026-10-20.
--
-- The guards only apply to the API roles (anon/authenticated); triggers that
-- run as the owner (SECURITY DEFINER) and the service role are untouched.

create or replace function public.is_api_role() returns boolean
  language sql stable set search_path = public, pg_temp
as $$ select current_user in ('anon', 'authenticated') $$;

-- ---------------------------------------------------------------- DMs
alter policy dm_messages_update_member on public.dm_messages
  using (
    sender_id <> auth.uid() and exists (
      select 1 from public.dm_threads t
      where t.id = dm_messages.thread_id
        and (t.user_a = auth.uid() or t.user_b = auth.uid())))
  with check (
    sender_id <> auth.uid() and exists (
      select 1 from public.dm_threads t
      where t.id = dm_messages.thread_id
        and (t.user_a = auth.uid() or t.user_b = auth.uid())));
alter policy dm_messages_update_member on public.dm_messages rename to dm_messages_update_recipient;

create or replace function public.dm_messages_guard() returns trigger
  language plpgsql set search_path = public, pg_temp
as $$
begin
  if public.is_api_role() and (
       new.id is distinct from old.id or new.thread_id is distinct from old.thread_id
    or new.sender_id is distinct from old.sender_id or new.body is distinct from old.body
    or new.created_at is distinct from old.created_at) then
    raise exception 'only read_at can be changed';
  end if;
  return new;
end $$;
create or replace trigger dm_messages_guard before update on public.dm_messages
  for each row execute function public.dm_messages_guard();

-- last_at is kept by the dm_touch_thread trigger (SECURITY DEFINER); the
-- app itself never updates a thread.
alter policy dm_threads_update_own on public.dm_threads using (false) with check (false);
alter policy dm_threads_update_own on public.dm_threads rename to dm_threads_no_update;

-- ---------------------------------------------------------------- profiles
create or replace function public.profiles_guard() returns trigger
  language plpgsql set search_path = public, pg_temp
as $$
begin
  if public.is_api_role() and (
       new.id is distinct from old.id or new.email is distinct from old.email
    or new.created_at is distinct from old.created_at
    or new.webauthn_credentials is distinct from old.webauthn_credentials) then
    raise exception 'this profile field cannot be changed';
  end if;
  return new;
end $$;
create or replace trigger profiles_guard before update on public.profiles
  for each row execute function public.profiles_guard();

alter policy profiles_update_own on public.profiles
  using (auth.uid() = id) with check (auth.uid() = id);

create or replace function public.handle_new_user() returns trigger
  language plpgsql security definer set search_path = public, pg_temp
as $$
declare
  un text := nullif(btrim(new.raw_user_meta_data->>'username'), '');
begin
  if un is null or char_length(un) > 40
     or exists (select 1 from profiles where username = un) then
    un := 'user_' || left(new.id::text, 8);
  end if;
  -- an address already sitting on another profile must not block sign-up
  insert into profiles (id, username, email)
  values (new.id, un,
          case when exists (select 1 from profiles where email = new.email)
               then null else new.email end)
  on conflict (id) do nothing;
  return new;
end $$;

alter table public.profiles
  add constraint profiles_username_len check (char_length(username) between 1 and 40) not valid,
  add constraint profiles_bio_len check (bio is null or char_length(bio) <= 500) not valid,
  add constraint profiles_avatar_len check (avatar_url is null or char_length(avatar_url) <= 500) not valid;

-- ---------------------------------------------------------------- reports
create or replace function public.reports_guard() returns trigger
  language plpgsql set search_path = public, pg_temp
as $$
begin
  if public.is_api_role() then
    if tg_op = 'INSERT' then
      new.created_at := now();
      new.flagged := false;
    elsif new.id is distinct from old.id or new.user_id is distinct from old.user_id
       or new.created_at is distinct from old.created_at
       or new.flagged is distinct from old.flagged then
      raise exception 'this report field cannot be changed';
    end if;
  end if;
  return new;
end $$;
-- named to sort before trg_report_rate_limit, so the limit sees now()
create or replace trigger a_reports_guard before insert or update on public.reports
  for each row execute function public.reports_guard();

alter policy reports_update_own on public.reports
  using (auth.uid() = user_id) with check (auth.uid() = user_id);

alter table public.reports
  add constraint reports_caption_len check (caption is null or char_length(caption) <= 5000) not valid,
  add constraint reports_image_url_len check (image_url is null or char_length(image_url) <= 1000) not valid;

alter table public.report_comments
  add constraint report_comments_body_len check (char_length(body) between 1 and 2000) not valid;
alter table public.report_flags
  add constraint report_flags_reason_len check (reason is null or char_length(reason) <= 500) not valid;
alter table public.user_flags
  add constraint user_flags_reason_len check (reason is null or char_length(reason) <= 500) not valid;

-- ---------------------------------------------------------------- rate limits
-- inside a SECURITY DEFINER function current_user is the owner, so the
-- caller's role is read from the request instead
create or replace function public.is_api_role_caller() returns boolean
  language sql stable set search_path = public, pg_temp
as $$ select coalesce(auth.role(), '') in ('anon', 'authenticated') $$;

-- rate_limit(user column, max rows, window). SECURITY DEFINER so it also
-- counts rows the writer can't read (feedback, other people's flags).
create or replace function public.rate_limit() returns trigger
  language plpgsql security definer set search_path = public, pg_temp
as $$
declare
  col text := tg_argv[0];
  lim int := tg_argv[1]::int;
  win interval := tg_argv[2]::interval;
  who uuid;
  n int;
begin
  if not public.is_api_role_caller() then return new; end if;
  who := (to_jsonb(new) ->> col)::uuid;
  if who is null then
    execute format('select count(*) from %I.%I where %I is null and created_at > now() - $1',
                   tg_table_schema, tg_table_name, col) into n using win;
  else
    execute format('select count(*) from %I.%I where %I = $1 and created_at > now() - $2',
                   tg_table_schema, tg_table_name, col) into n using who, win;
  end if;
  if n >= lim then
    raise exception 'Rate limit: max % per % on %', lim, win, tg_table_name
      using hint = 'Try again later.';
  end if;
  return new;
end $$;

create or replace trigger rl_comments before insert on public.report_comments
  for each row execute function public.rate_limit('user_id', '60', '1 hour');
create or replace trigger rl_dm before insert on public.dm_messages
  for each row execute function public.rate_limit('sender_id', '120', '1 hour');
create or replace trigger rl_follows before insert on public.follows
  for each row execute function public.rate_limit('follower_id', '200', '1 day');
create or replace trigger rl_report_flags before insert on public.report_flags
  for each row execute function public.rate_limit('user_id', '30', '1 day');
create or replace trigger rl_user_flags before insert on public.user_flags
  for each row execute function public.rate_limit('reporter_id', '20', '1 day');
create or replace trigger rl_reactions before insert on public.report_reactions
  for each row execute function public.rate_limit('user_id', '300', '1 hour');
-- signed-in: 30 a day each; anonymous: 100 a day in total
create or replace trigger rl_feedback_user before insert on public.feedback
  for each row when (new.user_id is not null)
  execute function public.rate_limit('user_id', '30', '1 day');
create or replace trigger rl_feedback_anon before insert on public.feedback
  for each row when (new.user_id is null)
  execute function public.rate_limit('user_id', '100', '1 day');

-- ---------------------------------------------------------------- privacy
-- the subquery runs under the reader's RLS on reports, so it only finds
-- posts this reader may see
alter policy comments_read_all on public.report_comments
  using (exists (select 1 from public.reports r where r.id = report_comments.report_id));
alter policy comments_read_all on public.report_comments rename to comments_read_visible;
alter policy cond_read_all on public.report_conditions
  using (exists (select 1 from public.reports r where r.id = report_conditions.report_id));
alter policy cond_read_all on public.report_conditions rename to cond_read_visible;
alter policy reactions_read_all on public.report_reactions
  using (exists (select 1 from public.reports r where r.id = report_reactions.report_id));
alter policy reactions_read_all on public.report_reactions rename to reactions_read_visible;

-- no-one but the triggers needs these
revoke execute on function public.rate_limit() from public, anon, authenticated;
revoke execute on function public.handle_new_user() from public, anon, authenticated;
