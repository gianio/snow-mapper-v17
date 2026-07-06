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

-- 5) Reload the API schema cache so the new columns/tables are visible now
NOTIFY pgrst, 'reload schema';
