-- Trust leaderboard: confirmations (likes) other people gave a user's public
-- posts. Own likes don't count. Only authors whose profile the caller may see.
create or replace function public.trust_board(lim int default 50)
returns table(user_id uuid, username text, avatar_url text, trust int, posts int)
language sql stable security definer set search_path = public, pg_temp
as $$
  with pub as (
    select r.id, r.user_id from reports r
    where coalesce(r.visibility,'all') = 'all' and not coalesce(r.flagged,false)
      and public.report_author_visible(r.user_id)
  ), t as (
    select p.user_id, count(x.*)::int as trust, count(distinct p.id)::int as posts
    from pub p left join report_reactions x
      on x.report_id = p.id and x.type = 'like' and x.user_id <> p.user_id
    group by p.user_id
  )
  select t.user_id, pr.username, pr.avatar_url, t.trust, t.posts
  from t join profiles pr on pr.id = t.user_id
  order by t.trust desc, t.posts desc
  limit greatest(1, least(coalesce(lim,50), 200));
$$;
revoke all on function public.trust_board(int) from public;
grant execute on function public.trust_board(int) to anon, authenticated;
