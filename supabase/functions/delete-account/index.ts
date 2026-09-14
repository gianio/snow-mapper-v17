// Snowmapper — account deletion (App Store Guideline 5.1.1(v))
//
// WHY THIS EXISTS
// The client can delete a user's CONTENT (it owns those rows under RLS), but it
// can never delete the user's `auth.users` row — that needs the service role.
// So before this function existed, "Konto & Daten löschen" left the account
// itself alive: the email stayed registered, the person could not sign up again,
// and Apple's requirement for real account deletion was not met. That is a hard
// App Review rejection, not a nitpick.
//
// WHAT IT DOES (in this order, so a failure never leaves an orphaned login)
//   1. verifies the caller's own JWT — a user may only delete THEMSELVES
//   2. removes their files from the report-images bucket
//   3. deletes their profile row (content cascades from it)
//   4. deletes the auth.users row  <-- the part only the service role can do
//
// DEPLOY
//   supabase functions deploy delete-account
// SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are injected by the platform; do
// NOT add them as secrets by hand and never expose the service role to a client.
//
// The client calls this and falls back to content-only deletion if it is not
// deployed yet (see profDeleteAccount), so shipping this is safe either way.

import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

const BUCKET = 'report-images';

const cors = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
};

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { ...cors, 'Content-Type': 'application/json' },
  });

/** Every object under the user's own prefixes, recursively. */
async function listUserFiles(admin: ReturnType<typeof createClient>, userId: string) {
  const paths: string[] = [];

  async function walk(prefix: string, depth = 0) {
    if (depth > 3) return; // the app only nests <uid>/ and avatars/, so this is plenty
    const { data, error } = await admin.storage.from(BUCKET).list(prefix, { limit: 1000 });
    if (error || !data) return;
    for (const entry of data) {
      const full = prefix ? `${prefix}/${entry.name}` : entry.name;
      // A folder comes back with no id/metadata; a file has both.
      if (entry.id === null || entry.metadata === null) await walk(full, depth + 1);
      else paths.push(full);
    }
  }

  // Reports are uploaded to `<uid>/…`; avatars to `avatars/<uid>_<ts>.<ext>`.
  await walk(userId);
  const { data: avatars } = await admin.storage.from(BUCKET).list('avatars', { limit: 1000 });
  for (const a of avatars ?? []) {
    if (a.name.startsWith(`${userId}_`)) paths.push(`avatars/${a.name}`);
  }
  return paths;
}

Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: cors });
  if (req.method !== 'POST') return json({ error: 'method not allowed' }, 405);

  const url = Deno.env.get('SUPABASE_URL');
  const serviceKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY');
  if (!url || !serviceKey) return json({ error: 'function not configured' }, 500);

  const authHeader = req.headers.get('Authorization') ?? '';
  if (!authHeader.startsWith('Bearer ')) return json({ error: 'missing bearer token' }, 401);

  // Resolve the caller from their OWN token. This is the authorisation check:
  // whoever the token belongs to is the only account this call can delete, so
  // there is no user id in the request body to tamper with.
  const admin = createClient(url, serviceKey, { auth: { persistSession: false } });
  const { data: claims, error: authErr } = await admin.auth.getUser(
    authHeader.replace('Bearer ', ''),
  );
  const user = claims?.user;
  if (authErr || !user) return json({ error: 'invalid or expired token' }, 401);

  const userId = user.id;
  const report: Record<string, unknown> = { user: userId };

  // 1. storage — best effort. A leftover image is a cost problem, not a
  //    compliance one, so it must never block deleting the account itself.
  try {
    const paths = await listUserFiles(admin, userId);
    if (paths.length) {
      const { error } = await admin.storage.from(BUCKET).remove(paths);
      report.files = error ? `failed: ${error.message}` : paths.length;
    } else {
      report.files = 0;
    }
  } catch (e) {
    report.files = `failed: ${(e as Error).message}`;
  }

  // 2. profile row — content (reports, comments, reactions, follows) cascades
  //    from it via the schema's foreign keys.
  try {
    const { error } = await admin.from('profiles').delete().eq('id', userId);
    report.profile = error ? `failed: ${error.message}` : 'deleted';
  } catch (e) {
    report.profile = `failed: ${(e as Error).message}`;
  }

  // 3. the login itself. This is the whole point of the function: if it fails,
  //    the account still exists and we must NOT report success.
  const { error: delErr } = await admin.auth.admin.deleteUser(userId);
  if (delErr) {
    return json({ error: `auth user not deleted: ${delErr.message}`, ...report }, 500);
  }

  return json({ ok: true, ...report, auth: 'deleted' });
});
