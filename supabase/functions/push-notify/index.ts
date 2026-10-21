// Snowmapper — web push for comments, likes, direct messages and followers
//
// Called by the push_hook trigger (pg_net) with {table, id} and the vault
// secret in x-push-secret. The row is read here with the service role, so
// the call itself carries nothing to forge; the receiver's push_enabled
// decides, and dead subscriptions (404/410) are removed.
//
// The VAPID key pair is made on first use and stored in push_config (no API
// role can read it); the app reads the public half via push_public_key().
//
// DEPLOY: supabase functions deploy push-notify --no-verify-jwt
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';
import webpush from 'npm:web-push@3.6.7';

const APP = 'https://gianio.github.io/snow-mapper-v17/';
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const cut = (s: unknown, n: number) => {
  const t = String(s ?? '').replace(/\s+/g, ' ').trim();
  return t.length > n ? t.slice(0, n - 1) + '…' : t;
};

type Msg = { to: string; title: string; body: string; url: string; tag: string; once?: string };

Deno.serve(async (req) => {
  if (req.method !== 'POST') return json({ error: 'method' }, 405);
  const admin = createClient(Deno.env.get('SUPABASE_URL')!, Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!);
  const { data: ok } = await admin.rpc('push_hook_ok', { s: req.headers.get('x-push-secret') || '' });
  if (ok !== true) return json({ error: 'forbidden' }, 403);

  let table = '', id = '';
  try { const b = await req.json(); table = String(b.table || ''); id = String(b.id || ''); } catch { /* empty */ }

  // keys first: the init call ({table:'init'}) only makes them
  let { data: cfg } = await admin.from('push_config').select('*').maybeSingle();
  if (!cfg) {
    const k = webpush.generateVAPIDKeys();
    await admin.from('push_config').insert({ vapid_public: k.publicKey, vapid_private: k.privateKey });
    ({ data: cfg } = await admin.from('push_config').select('*').maybeSingle());
  }
  if (!cfg) return json({ error: 'no keys' }, 500);
  if (table === 'init') return json({ ok: true });

  const name = async (uid: string) => {
    const { data } = await admin.from('profiles').select('username').eq('id', uid).maybeSingle();
    return data?.username ? '@' + data.username : 'Jemand';
  };
  const fresh = (t: string) => Date.now() - Date.parse(t) < 5 * 60 * 1000;
  let m: Msg | null = null;

  if (table === 'report_comments') {
    const { data: c } = await admin.from('report_comments').select('*').eq('id', id).maybeSingle();
    if (c && fresh(c.created_at)) {
      const { data: r } = await admin.from('reports').select('user_id').eq('id', c.report_id).maybeSingle();
      if (r && r.user_id !== c.user_id) {
        m = { to: r.user_id, title: `💬 ${await name(c.user_id)} hat kommentiert`, body: cut(c.body, 140),
              url: APP + '?post=' + c.report_id, tag: 'post-' + c.report_id };
      }
    }
  } else if (table === 'report_reactions') {
    const [rid, uid] = id.split(':');
    const { data: x } = await admin.from('report_reactions').select('*').eq('report_id', rid).eq('user_id', uid)
      .order('created_at', { ascending: false }).limit(1).maybeSingle();
    if (x && fresh(x.created_at)) {
      const { data: r } = await admin.from('reports').select('user_id,caption,location_name').eq('id', rid).maybeSingle();
      if (r && r.user_id !== uid) {
        m = { to: r.user_id, title: `❤️ ${await name(uid)} gefällt dein Beitrag`,
              body: cut(r.caption || r.location_name || 'Dein Schnee-Report', 120),
              url: APP + '?post=' + rid, tag: 'like-' + rid, once: 'like:' + id };
      }
    }
  } else if (table === 'dm_messages') {
    const { data: d } = await admin.from('dm_messages').select('*').eq('id', id).maybeSingle();
    if (d && fresh(d.created_at)) {
      const { data: t } = await admin.from('dm_threads').select('user_a,user_b').eq('id', d.thread_id).maybeSingle();
      if (t) {
        const to = t.user_a === d.sender_id ? t.user_b : t.user_a;
        const who = await name(d.sender_id);
        const body = /[?&]post=/.test(d.body || '') ? 'hat dir einen Beitrag geschickt' : cut(d.body, 160);
        m = { to, title: `✉️ ${who}`, body,
              url: APP + '?dm=' + d.sender_id + '&n=' + encodeURIComponent(who.replace(/^@/, '')),
              tag: 'dm-' + d.thread_id };
      }
    }
  } else if (table === 'test') {
    // the profile's test button (push_test()): one a minute, also while the
    // switch is not saved yet
    m = { to: id, title: '❄️ Snowmapper', body: 'So sehen deine Benachrichtigungen aus.', url: APP,
          tag: 'test', once: 'test:' + id + ':' + Math.floor(Date.now() / 60000) };
  } else if (table === 'follows') {
    const [fid, tid] = id.split(':');
    const { data: f } = await admin.from('follows').select('*').eq('follower_id', fid).eq('following_id', tid).maybeSingle();
    if (f && fresh(f.created_at)) {
      m = { to: tid, title: `👋 ${await name(fid)} folgt dir jetzt`, body: 'Tippe, um das Profil zu sehen.',
            url: APP + '?user=' + fid, tag: 'follow-' + fid, once: 'follow:' + id };
    }
  }
  if (!m) return json({ sent: 0 });

  if (table !== 'test') {
    const { data: p } = await admin.from('profiles').select('push_enabled').eq('id', m.to).maybeSingle();
    if (!p?.push_enabled) return json({ sent: 0, off: true });
  }
  if (m.once) {
    const { error } = await admin.from('push_log').insert({ key: m.once });
    if (error) return json({ sent: 0, again: true });
  }
  const { data: subs } = await admin.from('push_subscriptions').select('*').eq('user_id', m.to);
  if (!subs?.length) return json({ sent: 0 });

  webpush.setVapidDetails(APP, cfg.vapid_public, cfg.vapid_private);
  const payload = JSON.stringify({ title: m.title, body: m.body, url: m.url, tag: m.tag });
  let sent = 0;
  const errs: number[] = [];
  await Promise.all(subs.map(async (s) => {
    try {
      await webpush.sendNotification({ endpoint: s.endpoint, keys: { p256dh: s.p256dh, auth: s.auth } },
        payload, { TTL: 60 * 60 * 24, urgency: 'normal' });
      sent++;
    } catch (e) {
      const code = (e as { statusCode?: number }).statusCode;
      errs.push(code || 0);
      if (code === 404 || code === 410) await admin.from('push_subscriptions').delete().eq('endpoint', s.endpoint);
    }
  }));
  // old once-keys go with the occasional call
  if (Math.random() < 0.02) {
    await admin.from('push_log').delete().lt('created_at', new Date(Date.now() - 30 * 864e5).toISOString());
  }
  return json({ sent, errs });
});
