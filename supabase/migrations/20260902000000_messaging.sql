-- ============================================================
-- Direct messages.
-- RUN THIS in Supabase -> SQL Editor -> New query -> Run.
-- Safe to run more than once.
--
-- Until this has been run, the app hides the message button and says
-- so rather than failing: the client checks for the table on start-up.
-- ============================================================

-- One row per conversation. The pair is ordered (a < b) and unique, so
-- "open the conversation with X" is a lookup, not a search, and two people
-- cannot end up with two threads between them.
CREATE TABLE IF NOT EXISTS dm_threads (
  id         UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_a     UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  user_b     UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  last_at    TIMESTAMPTZ DEFAULT NOW(),
  CONSTRAINT dm_threads_ordered CHECK (user_a < user_b),
  CONSTRAINT dm_threads_pair UNIQUE (user_a, user_b)
);
ALTER TABLE dm_threads ENABLE ROW LEVEL SECURITY;

-- You can only see, or start, a thread you are in.
DROP POLICY IF EXISTS "dm_threads_read_own" ON dm_threads;
CREATE POLICY "dm_threads_read_own" ON dm_threads FOR SELECT
  USING (auth.uid() = user_a OR auth.uid() = user_b);
DROP POLICY IF EXISTS "dm_threads_insert_own" ON dm_threads;
CREATE POLICY "dm_threads_insert_own" ON dm_threads FOR INSERT
  WITH CHECK (auth.uid() = user_a OR auth.uid() = user_b);
DROP POLICY IF EXISTS "dm_threads_update_own" ON dm_threads;
CREATE POLICY "dm_threads_update_own" ON dm_threads FOR UPDATE
  USING (auth.uid() = user_a OR auth.uid() = user_b);

CREATE INDEX IF NOT EXISTS idx_dm_threads_a ON dm_threads (user_a, last_at DESC);
CREATE INDEX IF NOT EXISTS idx_dm_threads_b ON dm_threads (user_b, last_at DESC);

-- The messages themselves.
CREATE TABLE IF NOT EXISTS dm_messages (
  id         UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  thread_id  UUID NOT NULL REFERENCES dm_threads(id) ON DELETE CASCADE,
  sender_id  UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  body       TEXT NOT NULL CHECK (char_length(body) BETWEEN 1 AND 2000),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  read_at    TIMESTAMPTZ
);
ALTER TABLE dm_messages ENABLE ROW LEVEL SECURITY;

-- Membership of the thread is what grants access to its messages. The
-- subquery is the whole enforcement: there is no way to read a message
-- belonging to a thread you are not in.
DROP POLICY IF EXISTS "dm_messages_read_own" ON dm_messages;
CREATE POLICY "dm_messages_read_own" ON dm_messages FOR SELECT
  USING (EXISTS (SELECT 1 FROM dm_threads t
                 WHERE t.id = dm_messages.thread_id
                   AND (t.user_a = auth.uid() OR t.user_b = auth.uid())));

-- You may only send as yourself, and only into a thread you are in.
DROP POLICY IF EXISTS "dm_messages_insert_own" ON dm_messages;
CREATE POLICY "dm_messages_insert_own" ON dm_messages FOR INSERT
  WITH CHECK (auth.uid() = sender_id
              AND EXISTS (SELECT 1 FROM dm_threads t
                          WHERE t.id = dm_messages.thread_id
                            AND (t.user_a = auth.uid() OR t.user_b = auth.uid())));

-- Marking as read is an update by the *recipient*, so it is deliberately
-- not restricted to the sender.
DROP POLICY IF EXISTS "dm_messages_update_member" ON dm_messages;
CREATE POLICY "dm_messages_update_member" ON dm_messages FOR UPDATE
  USING (EXISTS (SELECT 1 FROM dm_threads t
                 WHERE t.id = dm_messages.thread_id
                   AND (t.user_a = auth.uid() OR t.user_b = auth.uid())));

DROP POLICY IF EXISTS "dm_messages_delete_own" ON dm_messages;
CREATE POLICY "dm_messages_delete_own" ON dm_messages FOR DELETE
  USING (auth.uid() = sender_id);

CREATE INDEX IF NOT EXISTS idx_dm_messages_thread ON dm_messages (thread_id, created_at);

-- Keep the thread list sorted by activity without the client having to
-- write to two tables.
CREATE OR REPLACE FUNCTION dm_touch_thread() RETURNS TRIGGER AS $$
BEGIN
  UPDATE dm_threads SET last_at = NEW.created_at WHERE id = NEW.thread_id;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS dm_messages_touch ON dm_messages;
CREATE TRIGGER dm_messages_touch AFTER INSERT ON dm_messages
  FOR EACH ROW EXECUTE FUNCTION dm_touch_thread();

-- Open or create the thread between the caller and someone else, in one
-- round trip, with the pair ordering handled here rather than in the client.
CREATE OR REPLACE FUNCTION dm_open_thread(other UUID) RETURNS UUID AS $$
DECLARE
  lo UUID; hi UUID; tid UUID;
BEGIN
  IF auth.uid() IS NULL THEN RAISE EXCEPTION 'not signed in'; END IF;
  IF other IS NULL OR other = auth.uid() THEN RAISE EXCEPTION 'bad recipient'; END IF;
  IF auth.uid() < other THEN lo := auth.uid(); hi := other;
  ELSE                      lo := other;      hi := auth.uid(); END IF;

  SELECT id INTO tid FROM dm_threads WHERE user_a = lo AND user_b = hi;
  IF tid IS NULL THEN
    INSERT INTO dm_threads (user_a, user_b) VALUES (lo, hi) RETURNING id INTO tid;
  END IF;
  RETURN tid;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Realtime, so a thread that is open updates itself.
DO $$
BEGIN
  BEGIN
    ALTER PUBLICATION supabase_realtime ADD TABLE dm_messages;
  EXCEPTION WHEN duplicate_object THEN NULL;
  END;
END $$;
