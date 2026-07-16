-- ============================================================
-- RUN THIS in Supabase → SQL Editor → New query → Run.
-- Fixes: "Could not find the bio column of profiles in the schema
-- cache", comments not working, and the photo-upload RLS error.
-- 100% safe to run multiple times.
-- ============================================================

-- 1) Profile columns (bio + push toggle)
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS bio TEXT;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS push_enabled BOOLEAN DEFAULT false;

-- 2) Comments table
CREATE TABLE IF NOT EXISTS report_comments (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  report_id UUID REFERENCES reports(id) ON DELETE CASCADE,
  user_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  body TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE report_comments ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "comments_read_all" ON report_comments;
CREATE POLICY "comments_read_all" ON report_comments FOR SELECT USING (true);
DROP POLICY IF EXISTS "comments_insert_own" ON report_comments;
CREATE POLICY "comments_insert_own" ON report_comments FOR INSERT WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "comments_delete_own" ON report_comments;
CREATE POLICY "comments_delete_own" ON report_comments FOR DELETE USING (auth.uid() = user_id);
CREATE INDEX IF NOT EXISTS idx_comments_report ON report_comments (report_id, created_at);

-- 3) Follows (needed for "Folge ich")
CREATE TABLE IF NOT EXISTS follows (
  follower_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  following_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (follower_id, following_id)
);
ALTER TABLE follows ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "follows_read_all" ON follows;
CREATE POLICY "follows_read_all" ON follows FOR SELECT USING (true);
DROP POLICY IF EXISTS "follows_manage_own" ON follows;
CREATE POLICY "follows_manage_own" ON follows
  FOR ALL USING (auth.uid() = follower_id) WITH CHECK (auth.uid() = follower_id);

-- 4) Storage bucket + policies for report photos (fixes the RLS upload error)
INSERT INTO storage.buckets (id, name, public)
VALUES ('report-images', 'report-images', true)
ON CONFLICT (id) DO UPDATE SET public = true;

DROP POLICY IF EXISTS "report_images_public_read" ON storage.objects;
CREATE POLICY "report_images_public_read" ON storage.objects
  FOR SELECT USING (bucket_id = 'report-images');

DROP POLICY IF EXISTS "report_images_auth_insert" ON storage.objects;
CREATE POLICY "report_images_auth_insert" ON storage.objects
  FOR INSERT TO authenticated
  WITH CHECK (bucket_id = 'report-images');

DROP POLICY IF EXISTS "report_images_owner_update" ON storage.objects;
CREATE POLICY "report_images_owner_update" ON storage.objects
  FOR UPDATE TO authenticated
  USING (bucket_id = 'report-images' AND owner = auth.uid());

DROP POLICY IF EXISTS "report_images_owner_delete" ON storage.objects;
CREATE POLICY "report_images_owner_delete" ON storage.objects
  FOR DELETE TO authenticated
  USING (bucket_id = 'report-images' AND owner = auth.uid());

-- 5) Privacy: stop exposing profiles.email (and webauthn_credentials) to
--    public/authenticated readers. RLS is row-level only, so a column REVOKE
--    alone is ineffective when table-level SELECT is granted — instead revoke
--    the whole table then grant back only the columns the app actually reads.
--    The client always gets the current user's email from the auth session
--    (sbUser.email), never from this table, so nothing breaks.
REVOKE SELECT ON profiles FROM anon, authenticated;
GRANT SELECT (id, username, avatar_url, bio, push_enabled, created_at)
  ON profiles TO anon, authenticated;

-- 6) Moderation: flag column + report_flags table + auto-flag trigger.
ALTER TABLE reports ADD COLUMN IF NOT EXISTS flagged BOOLEAN DEFAULT false;

CREATE TABLE IF NOT EXISTS report_flags (
  report_id  UUID REFERENCES reports(id) ON DELETE CASCADE,
  user_id    UUID REFERENCES profiles(id) ON DELETE CASCADE,
  reason     TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (report_id, user_id)          -- one flag per user per report
);
ALTER TABLE report_flags ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "flags_insert_own" ON report_flags;
CREATE POLICY "flags_insert_own" ON report_flags
  FOR INSERT WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "flags_read_own" ON report_flags;
CREATE POLICY "flags_read_own" ON report_flags
  FOR SELECT USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "flags_delete_own" ON report_flags;
CREATE POLICY "flags_delete_own" ON report_flags
  FOR DELETE USING (auth.uid() = user_id);

-- Auto-hide: once >= 3 distinct users flag a report, mark it flagged so the
-- feed can filter it without any manual DB intervention. SECURITY DEFINER so
-- it can update a report it does not own and count across all flags.
CREATE OR REPLACE FUNCTION mark_report_flagged()
RETURNS TRIGGER AS $$
BEGIN
  IF (SELECT COUNT(*) FROM report_flags WHERE report_id = NEW.report_id) >= 3 THEN
    UPDATE reports SET flagged = true WHERE id = NEW.report_id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
DROP TRIGGER IF EXISTS trg_mark_flagged ON report_flags;
CREATE TRIGGER trg_mark_flagged AFTER INSERT ON report_flags
  FOR EACH ROW EXECUTE FUNCTION mark_report_flagged();

-- 7) Abuse guard: cap reports at 20 per user per rolling 24 h.
CREATE OR REPLACE FUNCTION enforce_report_rate_limit()
RETURNS TRIGGER AS $$
BEGIN
  IF (SELECT COUNT(*) FROM reports
        WHERE user_id = NEW.user_id
          AND created_at > NOW() - INTERVAL '24 hours') >= 20 THEN
    RAISE EXCEPTION 'Rate limit: max 20 reports per 24 h (%).', NEW.user_id
      USING HINT = 'Try again later.';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_report_rate_limit ON reports;
CREATE TRIGGER trg_report_rate_limit BEFORE INSERT ON reports
  FOR EACH ROW EXECUTE FUNCTION enforce_report_rate_limit();

-- 8) Storage hardening: cap upload size (~3 MB) and restrict to image types.
--    Combined with the client-side downscale this keeps the 1 GB free tier
--    from filling up with raw 12-MP phone photos.
UPDATE storage.buckets
SET file_size_limit = 3145728,
    allowed_mime_types = ARRAY['image/jpeg','image/png','image/webp']
WHERE id = 'report-images';

-- 9) Per-post condition ratings (crowdsourced snow quality: 1–5 stars + powder).
--    One rating per user per report (upsert on the PK).
CREATE TABLE IF NOT EXISTS report_conditions (
  report_id  UUID REFERENCES reports(id) ON DELETE CASCADE,
  user_id    UUID REFERENCES profiles(id) ON DELETE CASCADE,
  stars      SMALLINT CHECK (stars BETWEEN 1 AND 5),
  powder     BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (report_id, user_id)
);
ALTER TABLE report_conditions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "cond_read_all" ON report_conditions;
CREATE POLICY "cond_read_all" ON report_conditions FOR SELECT USING (true);
DROP POLICY IF EXISTS "cond_insert_own" ON report_conditions;
CREATE POLICY "cond_insert_own" ON report_conditions FOR INSERT WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "cond_update_own" ON report_conditions;
CREATE POLICY "cond_update_own" ON report_conditions FOR UPDATE USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "cond_delete_own" ON report_conditions;
CREATE POLICY "cond_delete_own" ON report_conditions FOR DELETE USING (auth.uid() = user_id);

-- 10) DSG/App-Store compliance: users may delete their own profile. The FK
--     cascades remove all their content (reports -> comments/reactions/flags/
--     conditions, follows). The auth.users login row itself needs the service
--     role (Edge Function) — until then it simply has no profile anymore.
DROP POLICY IF EXISTS "profiles_delete_own" ON profiles;
CREATE POLICY "profiles_delete_own" ON profiles
  FOR DELETE USING (auth.uid() = id);

-- 11) Profile visibility (Nur ich / Freunde / Alle) + user reporting.
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS visibility TEXT DEFAULT 'all';
-- §5 uses column-level grants on profiles, so the new column must be granted:
GRANT SELECT (visibility) ON profiles TO anon, authenticated;

CREATE TABLE IF NOT EXISTS user_flags (
  user_id     UUID REFERENCES profiles(id) ON DELETE CASCADE,  -- the reported user
  reporter_id UUID REFERENCES profiles(id) ON DELETE CASCADE,  -- who reported
  reason      TEXT,
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (user_id, reporter_id)          -- one report per reporter per user
);
ALTER TABLE user_flags ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "uflags_insert_own" ON user_flags;
CREATE POLICY "uflags_insert_own" ON user_flags
  FOR INSERT WITH CHECK (auth.uid() = reporter_id);
DROP POLICY IF EXISTS "uflags_read_own" ON user_flags;
CREATE POLICY "uflags_read_own" ON user_flags
  FOR SELECT USING (auth.uid() = reporter_id);

-- Enforcement: reports respect the author's visibility setting.
--   'all'      -> everyone (incl. anonymous)
--   'friends'  -> mutual follows only (both directions), plus the author
--   'me'       -> only the author
-- SECURITY DEFINER so the check can read profiles/follows regardless of the
-- caller's column grants and without RLS recursion.
CREATE OR REPLACE FUNCTION report_author_visible(author UUID)
RETURNS BOOLEAN AS $$
DECLARE vis TEXT;
BEGIN
  SELECT COALESCE(visibility, 'all') INTO vis FROM profiles WHERE id = author;
  IF vis IS NULL OR vis = 'all' THEN RETURN TRUE; END IF;      -- default: public
  IF auth.uid() IS NULL THEN RETURN FALSE; END IF;
  IF auth.uid() = author THEN RETURN TRUE; END IF;
  IF vis = 'friends' THEN
    RETURN EXISTS (SELECT 1 FROM follows
                   WHERE follower_id = auth.uid() AND following_id = author)
       AND EXISTS (SELECT 1 FROM follows
                   WHERE follower_id = author AND following_id = auth.uid());
  END IF;
  RETURN FALSE;                                                 -- 'me'
END;
$$ LANGUAGE plpgsql SECURITY DEFINER STABLE;

DROP POLICY IF EXISTS "reports_read_all" ON reports;
DROP POLICY IF EXISTS "reports_read_visible" ON reports;
CREATE POLICY "reports_read_visible" ON reports
  FOR SELECT USING (report_author_visible(user_id));

-- 12) Reload the API schema cache so the new columns/tables are visible now
NOTIFY pgrst, 'reload schema';
