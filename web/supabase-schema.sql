-- Swiss Snow Mapper — Database Schema
-- Run this in Supabase SQL Editor (Dashboard → SQL Editor → New query)

-- Enable PostGIS
CREATE EXTENSION IF NOT EXISTS postgis;

-- Profiles (extends Supabase auth.users)
CREATE TABLE profiles (
  id UUID REFERENCES auth.users ON DELETE CASCADE PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  email TEXT UNIQUE NOT NULL,
  avatar_url TEXT,
  webauthn_credentials JSONB DEFAULT '[]',
  created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "profiles_read_all" ON profiles
  FOR SELECT USING (true);

CREATE POLICY "profiles_update_own" ON profiles
  FOR UPDATE USING (auth.uid() = id);

-- Auto-create profile on user signup
CREATE OR REPLACE FUNCTION handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
  INSERT INTO profiles (id, username, email)
  VALUES (
    NEW.id,
    COALESCE(NEW.raw_user_meta_data->>'username', 'user_' || LEFT(NEW.id::text, 8)),
    NEW.email
  );
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION handle_new_user();

-- Reports
CREATE TABLE reports (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  location GEOGRAPHY(POINT, 4326) NOT NULL,
  elevation_m INT,
  location_name TEXT,
  image_url TEXT,
  primary_categories TEXT[] NOT NULL,
  subtype TEXT,
  condition_data JSONB,
  caption TEXT,
  hashtags TEXT[],
  tagged_users UUID[],
  completion_score INT DEFAULT 0,
  captured_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  sync_status TEXT DEFAULT 'synced'
);

ALTER TABLE reports ENABLE ROW LEVEL SECURITY;

CREATE POLICY "reports_read_all" ON reports
  FOR SELECT USING (true);

CREATE POLICY "reports_insert_own" ON reports
  FOR INSERT WITH CHECK (auth.uid() = user_id);

CREATE POLICY "reports_update_own" ON reports
  FOR UPDATE USING (auth.uid() = user_id);

CREATE POLICY "reports_delete_own" ON reports
  FOR DELETE USING (auth.uid() = user_id);

-- Report reactions
CREATE TABLE report_reactions (
  report_id UUID REFERENCES reports(id) ON DELETE CASCADE,
  user_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  type TEXT CHECK (type IN ('like', 'helpful', 'stale')) NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (report_id, user_id, type)
);

ALTER TABLE report_reactions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "reactions_read_all" ON report_reactions
  FOR SELECT USING (true);

CREATE POLICY "reactions_insert_auth" ON report_reactions
  FOR INSERT WITH CHECK (auth.uid() = user_id);

CREATE POLICY "reactions_delete_own" ON report_reactions
  FOR DELETE USING (auth.uid() = user_id);

-- ============================================================
-- SOCIAL LAYER (follows, groups) — run this block to enable the
-- follow / connections / groups features. Safe to re-run.
-- Likes reuse the existing report_reactions table (type='like').
-- ============================================================

-- Follows (user connections)
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

-- Groups
CREATE TABLE IF NOT EXISTS groups (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  created_by UUID REFERENCES profiles(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE groups ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "groups_read_all" ON groups;
CREATE POLICY "groups_read_all" ON groups FOR SELECT USING (true);
DROP POLICY IF EXISTS "groups_create_auth" ON groups;
CREATE POLICY "groups_create_auth" ON groups FOR INSERT WITH CHECK (auth.uid() = created_by);

-- Group membership
CREATE TABLE IF NOT EXISTS group_members (
  group_id UUID REFERENCES groups(id) ON DELETE CASCADE,
  user_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (group_id, user_id)
);
ALTER TABLE group_members ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "group_members_read_all" ON group_members;
CREATE POLICY "group_members_read_all" ON group_members FOR SELECT USING (true);
DROP POLICY IF EXISTS "group_members_manage_own" ON group_members;
CREATE POLICY "group_members_manage_own" ON group_members
  FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- Link reports to an optional group
ALTER TABLE reports ADD COLUMN IF NOT EXISTS group_id UUID REFERENCES groups(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_follows_follower ON follows (follower_id);
CREATE INDEX IF NOT EXISTS idx_group_members_user ON group_members (user_id);
CREATE INDEX IF NOT EXISTS idx_reports_group ON reports (group_id);

-- ============================================================
-- STORAGE: public bucket for report photos.
-- Without this (public) bucket + policies, uploaded photos do NOT
-- appear in the feed. Run this once.
-- ============================================================
INSERT INTO storage.buckets (id, name, public)
VALUES ('report-images', 'report-images', true)
ON CONFLICT (id) DO UPDATE SET public = true;

DROP POLICY IF EXISTS "report_images_public_read" ON storage.objects;
CREATE POLICY "report_images_public_read" ON storage.objects
  FOR SELECT USING (bucket_id = 'report-images');

DROP POLICY IF EXISTS "report_images_auth_insert" ON storage.objects;
CREATE POLICY "report_images_auth_insert" ON storage.objects
  FOR INSERT WITH CHECK (bucket_id = 'report-images' AND auth.role() = 'authenticated');

DROP POLICY IF EXISTS "report_images_owner_update" ON storage.objects;
CREATE POLICY "report_images_owner_update" ON storage.objects
  FOR UPDATE USING (bucket_id = 'report-images' AND auth.uid() = owner);

DROP POLICY IF EXISTS "report_images_owner_delete" ON storage.objects;
CREATE POLICY "report_images_owner_delete" ON storage.objects
  FOR DELETE USING (bucket_id = 'report-images' AND auth.uid() = owner);

-- Indices
CREATE INDEX idx_reports_location ON reports USING GIST(location);
CREATE INDEX idx_reports_categories ON reports USING GIN(primary_categories);
CREATE INDEX idx_reports_hashtags ON reports USING GIN(hashtags);
CREATE INDEX idx_reports_created ON reports (created_at DESC);
CREATE INDEX idx_reports_user ON reports (user_id);

-- Helper: get reports as GeoJSON for map layer
CREATE OR REPLACE FUNCTION get_reports_geojson(
  cat TEXT DEFAULT NULL,
  since INTERVAL DEFAULT INTERVAL '30 days'
)
RETURNS JSON AS $$
  SELECT json_build_object(
    'type', 'FeatureCollection',
    'features', COALESCE(json_agg(json_build_object(
      'type', 'Feature',
      'geometry', ST_AsGeoJSON(r.location)::json,
      'properties', json_build_object(
        'id', r.id,
        'user_id', r.user_id,
        'username', p.username,
        'avatar_url', p.avatar_url,
        'primary_categories', r.primary_categories,
        'subtype', r.subtype,
        'condition_data', r.condition_data,
        'image_url', r.image_url,
        'caption', r.caption,
        'completion_score', r.completion_score,
        'elevation_m', r.elevation_m,
        'location_name', r.location_name,
        'created_at', r.created_at
      )
    )), '[]'::json)
  )
  FROM reports r
  JOIN profiles p ON p.id = r.user_id
  WHERE r.created_at > NOW() - since
    AND (cat IS NULL OR cat = ANY(r.primary_categories));
$$ LANGUAGE sql STABLE;
