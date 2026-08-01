-- ============================================================
-- RESET ALL POSTS  —  Supabase → SQL Editor → New query → Run
--
-- Deletes every report and everything hanging off it. Accounts,
-- profiles and follows are KEPT; only the content goes.
--
-- ⚠ This cannot be undone. Take a backup first if you might want
--   the test data back (Dashboard → Database → Backups).
-- ============================================================

BEGIN;

-- Everything below references reports(id) ON DELETE CASCADE, so deleting the
-- reports would be enough. They are listed explicitly anyway: it makes the
-- blast radius obvious, and it still works if a cascade was ever dropped.
DELETE FROM report_conditions;
DELETE FROM report_reactions;
DELETE FROM report_comments;
DELETE FROM report_flags;
DELETE FROM reports;

COMMIT;

-- What is left (should all be 0):
SELECT 'reports'           AS table, count(*) FROM reports
UNION ALL SELECT 'comments',         count(*) FROM report_comments
UNION ALL SELECT 'reactions',        count(*) FROM report_reactions
UNION ALL SELECT 'flags',            count(*) FROM report_flags
UNION ALL SELECT 'condition_ratings',count(*) FROM report_conditions;


-- ------------------------------------------------------------
-- OPTIONAL: also delete the uploaded photos.
-- The rows above are gone but the image files stay in Storage.
-- Uncomment to empty the bucket as well.
-- ------------------------------------------------------------
-- DELETE FROM storage.objects WHERE bucket_id = 'report-images';


-- ------------------------------------------------------------
-- OPTIONAL: reset one user instead of everyone.
-- Replace the e-mail, then run only this block.
-- ------------------------------------------------------------
-- DELETE FROM reports
--  WHERE user_id = (SELECT id FROM auth.users WHERE email = 'you@example.com');


-- ------------------------------------------------------------
-- OPTIONAL: full wipe INCLUDING accounts.
-- Deleting the auth user cascades to profiles and to everything
-- that references them. Only for a clean-slate test round.
-- ------------------------------------------------------------
-- DELETE FROM auth.users;
