-- Blocking (App Store guideline 1.2: an app with user content must let people
-- block abusive users).
--
-- * user_blocks holds who blocked whom; each person sees and edits only the
--   rows they made, so a blocked person never learns about the block.
-- * A block works both ways: neither side sees the other's posts or comments
--   any more, no new conversation or message can pass between them, and the
--   follows between them are removed.
-- * Reporting stays separate (report_flags, user_flags): blocking hides, a
--   report asks the owner to act.

create table if not exists public.user_blocks (
  blocker uuid not null references auth.users(id) on delete cascade,
  blocked uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (blocker, blocked),
  constraint user_blocks_not_self check (blocker <> blocked)
);
create index if not exists user_blocks_blocked on public.user_blocks (blocked);
alter table public.user_blocks enable row level security;

drop policy if exists user_blocks_read_own on public.user_blocks;
create policy user_blocks_read_own on public.user_blocks
  for select to authenticated using (blocker = (select auth.uid()));
drop policy if exists user_blocks_insert_own on public.user_blocks;
create policy user_blocks_insert_own on public.user_blocks
  for insert to authenticated with check (blocker = (select auth.uid()));
drop policy if exists user_blocks_delete_own on public.user_blocks;
create policy user_blocks_delete_own on public.user_blocks
  for delete to authenticated using (blocker = (select auth.uid()));

-- has either of the two blocked the other? (false when signed out)
create or replace function public.is_blocked_with(other uuid) returns boolean
  language sql stable security definer set search_path = public, pg_temp
as $$
  select auth.uid() is not null and other is not null and auth.uid() <> other
     and exists (select 1 from public.user_blocks
                 where (blocker = auth.uid() and blocked = other)
                    or (blocker = other and blocked = auth.uid()))
$$;
revoke execute on function public.is_blocked_with(uuid) from public;
grant execute on function public.is_blocked_with(uuid) to anon, authenticated;

-- posts: the existing visibility rule, plus the block
create or replace function public.report_author_visible(author uuid) returns boolean
  language plpgsql stable security definer set search_path = public, pg_temp
as $$
declare vis text;
begin
  if public.is_blocked_with(author) then return false; end if;
  select coalesce(visibility, 'all') into vis from profiles where id = author;
  if vis is null or vis = 'all' then return true; end if;      -- default: public
  if auth.uid() is null then return false; end if;
  if auth.uid() = author then return true; end if;
  if vis = 'friends' then
    return exists (select 1 from follows where follower_id = auth.uid() and following_id = author)
       and exists (select 1 from follows where follower_id = author and following_id = auth.uid());
  end if;
  return false;                                                 -- 'me'
end $$;

-- comments: on a post you can see, and not from someone on the other side of a block
drop policy if exists comments_read_visible on public.report_comments;
create policy comments_read_visible on public.report_comments for select using (
  exists (select 1 from public.reports r where r.id = report_comments.report_id)
  and not public.is_blocked_with(report_comments.user_id));

-- no comment on a post of someone you blocked or who blocked you
create or replace function public.comment_block_guard() returns trigger
  language plpgsql security definer set search_path = public, pg_temp
as $$
begin
  if public.is_api_role() and exists (
       select 1 from public.reports r join public.user_blocks b
         on (b.blocker = r.user_id and b.blocked = new.user_id)
         or (b.blocker = new.user_id and b.blocked = r.user_id)
       where r.id = new.report_id) then
    raise exception 'blocked';
  end if;
  return new;
end $$;
drop trigger if exists report_comments_block on public.report_comments;
create trigger report_comments_block before insert on public.report_comments
  for each row execute function public.comment_block_guard();

-- messages: no new conversation and no new message across a block
create or replace function public.dm_block_guard() returns trigger
  language plpgsql security definer set search_path = public, pg_temp
as $$
declare a uuid; b uuid;
begin
  if not public.is_api_role() then return new; end if;
  if tg_table_name = 'dm_threads' then
    a := new.user_a; b := new.user_b;
  else
    select user_a, user_b into a, b from public.dm_threads where id = new.thread_id;
  end if;
  if exists (select 1 from public.user_blocks
             where (blocker = a and blocked = b) or (blocker = b and blocked = a)) then
    raise exception 'blocked';
  end if;
  return new;
end $$;
drop trigger if exists dm_threads_block on public.dm_threads;
create trigger dm_threads_block before insert on public.dm_threads
  for each row execute function public.dm_block_guard();
drop trigger if exists dm_messages_block on public.dm_messages;
create trigger dm_messages_block before insert on public.dm_messages
  for each row execute function public.dm_block_guard();

-- a block ends the follows in both directions (the blocker cannot delete the
-- other person's follow row under RLS, hence definer rights)
create or replace function public.user_blocks_unfollow() returns trigger
  language plpgsql security definer set search_path = public, pg_temp
as $$
begin
  delete from public.follows
   where (follower_id = new.blocker and following_id = new.blocked)
      or (follower_id = new.blocked and following_id = new.blocker);
  return new;
end $$;
drop trigger if exists user_blocks_unfollow on public.user_blocks;
create trigger user_blocks_unfollow after insert on public.user_blocks
  for each row execute function public.user_blocks_unfollow();

-- trigger functions are not for calling
revoke execute on function public.comment_block_guard(), public.dm_block_guard(),
  public.user_blocks_unfollow() from public, anon, authenticated;

-- no following someone across a block
drop policy if exists follows_no_block on public.follows;
create policy follows_no_block on public.follows as restrictive for insert
  with check (not public.is_blocked_with(following_id));
