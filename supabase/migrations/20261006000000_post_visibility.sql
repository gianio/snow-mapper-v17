-- ============================================================
-- Per-post visibility (Snowp: friends only; activities: followers).
-- RUN THIS in Supabase -> SQL Editor -> New query -> Run.
-- Safe to run more than once.
--
-- Until this has been run the app refuses to post a Snowp (it never falls
-- back to a public post) and shows activities to followers only inside
-- the app, without database enforcement.
-- ============================================================

-- 'all'       everyone who may see the author at all (profile setting)
-- 'followers' people who follow the author
-- 'friends'   mutual follows only
-- 'me'        only the author
ALTER TABLE reports ADD COLUMN IF NOT EXISTS visibility TEXT DEFAULT 'all';
DO $$ BEGIN
  ALTER TABLE reports ADD CONSTRAINT reports_visibility_check
    CHECK (visibility IN ('all', 'followers', 'friends', 'me'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE OR REPLACE FUNCTION report_post_visible(author UUID, vis TEXT)
RETURNS BOOLEAN AS $$
BEGIN
  IF vis IS NULL OR vis = 'all' THEN RETURN TRUE; END IF;
  IF auth.uid() IS NULL THEN RETURN FALSE; END IF;
  IF auth.uid() = author THEN RETURN TRUE; END IF;
  IF vis = 'followers' THEN
    RETURN EXISTS (SELECT 1 FROM follows
                   WHERE follower_id = auth.uid() AND following_id = author);
  END IF;
  IF vis = 'friends' THEN
    RETURN EXISTS (SELECT 1 FROM follows
                   WHERE follower_id = auth.uid() AND following_id = author)
       AND EXISTS (SELECT 1 FROM follows
                   WHERE follower_id = author AND following_id = auth.uid());
  END IF;
  RETURN FALSE;                                                 -- 'me'
END;
$$ LANGUAGE plpgsql SECURITY DEFINER STABLE SET search_path = public;

-- Reading a report needs both: the author's profile setting and the post's
-- own visibility.
DROP POLICY IF EXISTS "reports_read_visible" ON reports;
CREATE POLICY "reports_read_visible" ON reports
  FOR SELECT USING (report_author_visible(user_id)
                    AND report_post_visible(user_id, visibility));

CREATE INDEX IF NOT EXISTS idx_reports_visibility ON reports (visibility);

NOTIFY pgrst, 'reload schema';
