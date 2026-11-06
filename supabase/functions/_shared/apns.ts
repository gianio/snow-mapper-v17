// APNs for the iPhone app. The app keeps its device token in
// push_subscriptions as endpoint https://api.push.apple.com/3/device/<token>
// with p256dh = 'apns' (auth = 'production' | 'sandbox'), so the senders can
// tell it from a web-push subscription.
//
// Secrets (Supabase > Edge Functions > Secrets):
//   APNS_KEY_P8   the .p8 key from developer.apple.com > Keys (Apple Push
//                 Notifications service), the whole text incl. BEGIN/END lines
//   APNS_KEY_ID   its Key ID (10 characters)
//   APNS_TEAM_ID  your Team ID (10 characters, top right in the developer portal)
// Optional: APNS_TOPIC (default ch.snowmapper.app).
import { importPKCS8, SignJWT } from 'npm:jose@5.9.6';

export const isApns = (s: { p256dh?: string }) => s?.p256dh === 'apns';

let jwt: { token: string; at: number } | null = null;
async function bearer(): Promise<string | null> {
  const p8 = Deno.env.get('APNS_KEY_P8'), kid = Deno.env.get('APNS_KEY_ID'), iss = Deno.env.get('APNS_TEAM_ID');
  if (!p8 || !kid || !iss) return null;
  // Apple wants a token no older than an hour and not renewed more than
  // every 20 minutes: keep one for 40.
  if (jwt && Date.now() - jwt.at < 40 * 60 * 1000) return jwt.token;
  const key = await importPKCS8(p8.replace(/\\n/g, '\n').trim(), 'ES256');
  const token = await new SignJWT({}).setProtectedHeader({ alg: 'ES256', kid })
    .setIssuer(iss).setIssuedAt().sign(key);
  jwt = { token, at: Date.now() };
  return token;
}

// badge: the number on the app icon (unread messages); left out = unchanged
export type ApnsMsg = { title: string; body: string; url: string; tag: string; ttlSec: number; badge?: number };

// -> HTTP status (200 ok; 410 / 400 BadDeviceToken: drop the token), 0 = not configured
export async function sendApns(endpoint: string, env: string, m: ApnsMsg): Promise<number> {
  try {
    return await sendOnce(endpoint, env, m);
  } catch (e) {
    console.warn('apns failed', String(e));   // network, or a malformed key
    return 503;
  }
}

async function sendOnce(endpoint: string, env: string, m: ApnsMsg): Promise<number> {
  const auth = await bearer();
  if (!auth) return 0;
  const token = endpoint.split('/').pop() || '';
  const host = env === 'sandbox' ? 'api.sandbox.push.apple.com' : 'api.push.apple.com';
  const r = await fetch(`https://${host}/3/device/${token}`, {
    method: 'POST',
    headers: {
      authorization: `bearer ${auth}`,
      'apns-topic': Deno.env.get('APNS_TOPIC') || 'ch.snowmapper.app',
      'apns-push-type': 'alert',
      'apns-priority': '10',
      'apns-expiration': String(Math.floor(Date.now() / 1000) + m.ttlSec),
      'apns-collapse-id': m.tag.slice(0, 64),
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      aps: { alert: { title: m.title, body: m.body }, sound: 'default', 'thread-id': m.tag,
             ...(m.badge != null ? { badge: m.badge } : {}) },
      url: m.url,
    }),
  });
  if (r.status !== 200) {
    let reason = '';
    try { reason = (await r.json()).reason || ''; } catch { /* empty */ }
    if (r.status === 400 && /BadDeviceToken|DeviceTokenNotForTopic/.test(reason)) return 410;
    console.warn('apns', r.status, reason);
  }
  return r.status;
}
