-- Profile metric: how many feedbacks a user has sent. The feedback rows stay
-- unreadable through the API; this only returns the number.
create or replace function public.feedback_count(uid uuid)
returns integer
language sql
stable
security definer
set search_path = public
as $$ select count(*)::int from public.feedback where user_id = uid $$;
revoke all on function public.feedback_count(uuid) from public;
grant execute on function public.feedback_count(uuid) to anon, authenticated;
