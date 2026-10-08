-- Second round of hardening from the Supabase advisors (Oct 2026), for the
-- functions added since 20260914000000_harden_functions.sql (messaging,
-- moderation, post visibility).
--
-- Idempotent: ALTER/REVOKE/GRANT are all safe to re-run.

-- 1) Pin search_path everywhere. For a SECURITY DEFINER function a mutable
--    path lets anyone who can create objects earlier in the path shadow the
--    tables it touches; for the others it is just good hygiene.
ALTER FUNCTION public.dm_open_thread(uuid)              SET search_path = public, pg_temp;
ALTER FUNCTION public.dm_touch_thread()                 SET search_path = public, pg_temp;
ALTER FUNCTION public.mark_report_flagged()             SET search_path = public, pg_temp;
ALTER FUNCTION public.report_author_visible(uuid)       SET search_path = public, pg_temp;
ALTER FUNCTION public.report_post_visible(uuid, text)   SET search_path = public, pg_temp;
ALTER FUNCTION public.enforce_report_rate_limit()       SET search_path = public, pg_temp;
ALTER FUNCTION public.get_reports_geojson(text, interval) SET search_path = public, pg_temp;

-- 2) Trigger functions are not meant to be called over /rest/v1/rpc. Triggers
--    fire as the table owner and do not need EXECUTE grants. Functions get
--    EXECUTE for PUBLIC by default, so PUBLIC has to go as well.
REVOKE EXECUTE ON FUNCTION public.dm_touch_thread()     FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.mark_report_flagged() FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.handle_new_user()     FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.rls_auto_enable()     FROM PUBLIC, anon, authenticated;

-- 3) Opening a conversation needs an account.
REVOKE EXECUTE ON FUNCTION public.dm_open_thread(uuid) FROM PUBLIC, anon;
GRANT  EXECUTE ON FUNCTION public.dm_open_thread(uuid) TO authenticated;

-- Deliberately NOT revoked: report_author_visible / report_post_visible.
-- The reports read policy calls them as the reading role (anon included), so
-- revoking EXECUTE would hide every report. They only return a boolean about
-- visibility, which the caller could learn by reading reports anyway.
