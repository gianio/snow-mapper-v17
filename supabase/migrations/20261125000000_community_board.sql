-- The community directory and score card: every profile, with what it has
-- done in public -- never its e-mail. A profile on «Nur ich» appears with name
-- and picture only: its posts stay hidden (reports RLS, report_author_visible)
-- and are not counted here either. confirmations = reactions others gave its
-- public reports: the app's trust points.
drop function if exists public.community_board();
create function public.community_board()
returns table(id uuid, username text, avatar_url text, member_since timestamptz, private boolean,
              reports int, reports_30d int, confirmations int, comments int,
              followers int, last_active timestamptz)
language sql stable security definer set search_path = public as $$
  select p.id, p.username, p.avatar_url, p.created_at,
    coalesce(p.visibility, 'all') <> 'all',
    case when coalesce(p.visibility, 'all') = 'all' then (select count(*) from reports r where r.user_id = p.id
       and coalesce(r.flagged, false) = false and coalesce(r.visibility, 'all') = 'all')::int else 0 end,
    case when coalesce(p.visibility, 'all') = 'all' then (select count(*) from reports r where r.user_id = p.id
       and coalesce(r.flagged, false) = false and coalesce(r.visibility, 'all') = 'all'
       and r.created_at > now() - interval '30 days')::int else 0 end,
    case when coalesce(p.visibility, 'all') = 'all' then (select count(*) from report_reactions x join reports r on r.id = x.report_id
       where r.user_id = p.id and x.user_id <> p.id and coalesce(r.visibility, 'all') = 'all')::int else 0 end,
    (select count(*) from report_comments c where c.user_id = p.id)::int,
    (select count(*) from follows f where f.following_id = p.id)::int,
    case when coalesce(p.visibility, 'all') = 'all' then greatest(p.created_at,
             (select max(r.created_at) from reports r where r.user_id = p.id and coalesce(r.visibility, 'all') = 'all'),
             (select max(c.created_at) from report_comments c where c.user_id = p.id))
         else p.created_at end
  from profiles p
  where p.username is not null
$$;
revoke all on function public.community_board() from public;
grant execute on function public.community_board() to anon, authenticated;
