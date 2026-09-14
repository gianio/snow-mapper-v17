-- Security hardening from the Supabase advisors (get_advisors, Sep 2026).
--
-- Both findings concern SECURITY DEFINER functions, which run with the
-- privileges of their owner. That is necessary for what they do, but it means
-- their exposure and their search_path matter much more than for an ordinary
-- function. It matters more again now that the anon key ships inside a mobile
-- app binary, where anyone can extract it.
--
-- Idempotent: ALTER/REVOKE are both safe to re-run.

-- 1) Pin search_path on handle_new_user().
--
-- A SECURITY DEFINER function with a mutable search_path is the classic
-- privilege-escalation shape: anyone able to create objects in a schema that
-- appears earlier in the resolved path can shadow `profiles` and have this
-- function write into their table instead, as the owner. Pinning the path
-- removes the vector. (rls_auto_enable already sets its own search_path.)
ALTER FUNCTION public.handle_new_user() SET search_path = public, pg_temp;

-- 2) Stop anon/authenticated from calling the trigger functions over REST.
--
-- PostgREST exposes every function in an API schema at /rest/v1/rpc/<name>.
-- Neither of these is meant to be called directly — handle_new_user() is a row
-- trigger and rls_auto_enable() is an event trigger, so both would error out
-- rather than do anything useful. But a SECURITY DEFINER function reachable by
-- an unauthenticated caller is exposure with no upside, so revoke it.
--
-- Triggers are unaffected: they fire as the table owner and do not consult
-- EXECUTE grants.
REVOKE EXECUTE ON FUNCTION public.handle_new_user() FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.rls_auto_enable() FROM anon, authenticated;

-- Deliberately NOT done here:
--
--   * `spatial_ref_sys` RLS, and moving the PostGIS extension out of `public`.
--     That table is PostGIS's own coordinate-system reference data — public by
--     nature, no user data in it. Enabling RLS on it or relocating the
--     extension risks breaking existing spatial queries for no real gain.
--
--   * Leaked-password protection. That is an Auth dashboard setting, not SQL.
--     Worth turning on: Authentication > Policies > leaked password protection.
--
--   * UPDATE/DELETE policies on `groups`. Nobody can currently edit or delete a
--     group, not even its creator. That is a missing feature rather than a
--     vulnerability, so it belongs with the change that needs it instead of
--     being guessed at here.
