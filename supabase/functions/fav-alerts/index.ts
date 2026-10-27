// Snowmapper — new-snow alerts for favourite areas and tours
//
// Run every two hours by pg_cron (job 'fav-alerts', pg_net) with the vault
// secret in x-push-secret, the same one push-notify checks. Reads the new
// snow of the next 24/48 h per grid cell from the published data/alerts.json
// (written by every live build), and sends one push per person when one or
// more of their favourites reach their own threshold. A favourite is not
// alerted again within 36 h; nothing goes out when the forecast is stale.
//
// DEPLOY: supabase functions deploy fav-alerts --no-verify-jwt
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';
import webpush from 'npm:web-push@3.6.7';

const APP = 'https://gianio.github.io/snow-mapper-v17/';
const AGAIN_H = 36, STALE_H = 18;
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const b64 = (s: string) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

type Fav = { id: string; user_id: string; name: string; lat: number; lon: number; threshold_cm: number; last_alert_at: string | null };

Deno.serve(async (req) => {
  if (req.method !== 'POST') return json({ error: 'method' }, 405);
  const admin = createClient(Deno.env.get('SUPABASE_URL')!, Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!);
  const { data: ok } = await admin.rpc('push_hook_ok', { s: req.headers.get('x-push-secret') || '' });
  if (ok !== true) return json({ error: 'forbidden' }, 403);
  const dry = new URL(req.url).searchParams.get('dry') === '1';

  const r = await fetch(APP + 'data/alerts.json?t=' + Date.now(), { headers: { 'Cache-Control': 'no-cache' } });
  if (!r.ok) return json({ error: 'alerts.json ' + r.status }, 502);
  const g = await r.json();
  if (!g.ns48 || !g.w || !g.h || !g.bounds) return json({ error: 'alerts.json incomplete' }, 502);
  const age = (Date.now() - Date.parse(g.generated || 0)) / 3.6e6;
  if (!(age < STALE_H)) return json({ sent: 0, stale: Math.round(age) });
  const ns48 = b64(g.ns48), ns24 = b64(g.ns24 || g.ns48);
  const [laMin, loMin, laMax, loMax] = g.bounds as number[];
  const cell = (lat: number, lon: number) => {
    const x = Math.round((lon - loMin) / (loMax - loMin) * (g.w - 1));
    const y = Math.round((laMax - lat) / (laMax - laMin) * (g.h - 1));
    return x < 0 || y < 0 || x >= g.w || y >= g.h ? -1 : y * g.w + x;
  };
  // until when the 48 h reach, in words: "bis Do 14 Uhr"
  const end = new Date(Date.parse(String(g.from) + ':00Z') + Math.max(1, g.hours || 48) * 3.6e6);
  const until = isNaN(+end) ? '' : 'bis ' + new Intl.DateTimeFormat('de-CH', { timeZone: 'Europe/Zurich', weekday: 'short', hour: '2-digit' })
    .format(end).replace(/\.?,?\s*(\d+)$/, ' $1 Uhr');

  const { data: favs, error } = await admin.from('favorites')
    .select('id,user_id,name,lat,lon,threshold_cm,last_alert_at').eq('notify', true);
  if (error) return json({ error: error.message }, 500);
  const cut = Date.now() - AGAIN_H * 3.6e6;
  const hits = new Map<string, { f: Fav; cm: number; cm24: number }[]>();
  for (const f of (favs || []) as Fav[]) {
    if (f.last_alert_at && Date.parse(f.last_alert_at) > cut) continue;
    const p = cell(f.lat, f.lon); if (p < 0) continue;
    const cm = ns48[p];
    if (cm < f.threshold_cm) continue;
    const l = hits.get(f.user_id) || []; l.push({ f, cm, cm24: ns24[p] }); hits.set(f.user_id, l);
  }
  if (!hits.size) return json({ sent: 0, favs: favs?.length || 0 });

  const { data: cfg } = await admin.from('push_config').select('*').maybeSingle();
  if (!cfg) return json({ error: 'no keys' }, 500);
  webpush.setVapidDetails(APP, cfg.vapid_public, cfg.vapid_private);
  const { data: profs } = await admin.from('profiles').select('id,push_enabled').in('id', [...hits.keys()]);
  const on = new Set((profs || []).filter((p) => p.push_enabled).map((p) => p.id));
  let sent = 0;
  const out: unknown[] = [];
  for (const [uid, l] of hits) {
    if (!on.has(uid)) continue;
    l.sort((a, b) => b.cm - a.cm);
    const top = l[0];
    const title = l.length === 1 ? `❄️ ${top.cm} cm Neuschnee · ${top.f.name}` : `❄️ Neuschnee an ${l.length} Favoriten`;
    const body = l.length === 1
      ? `${until ? until.charAt(0).toUpperCase() + until.slice(1) + ' erwartet' : 'In den nächsten 48 h erwartet'}${top.cm24 ? ` (${top.cm24} cm in 24 h)` : ''} · Modell, keine Garantie.`
      : l.slice(0, 4).map((h) => `${h.f.name} ${h.cm} cm`).join(' · ') + (until ? ` – ${until}` : '');
    const url = APP + '?fav=' + top.f.id;
    out.push({ uid, title, body });
    if (dry) continue;
    const { data: subs } = await admin.from('push_subscriptions').select('*').eq('user_id', uid);
    let okOne = false;
    for (const s of subs || []) {
      try {
        await webpush.sendNotification({ endpoint: s.endpoint, keys: { p256dh: s.p256dh, auth: s.auth } },
          JSON.stringify({ title, body, url, tag: 'fav-snow' }), { TTL: 60 * 60 * 12, urgency: 'normal' });
        sent++; okOne = true;
      } catch (e) {
        const code = (e as { statusCode?: number }).statusCode;
        if (code === 404 || code === 410) await admin.from('push_subscriptions').delete().eq('endpoint', s.endpoint);
      }
    }
    if (okOne) await admin.from('favorites').update({ last_alert_at: new Date().toISOString() }).in('id', l.map((h) => h.f.id));
  }
  return json({ sent, people: hits.size, dry, out: dry ? out : undefined });
});
