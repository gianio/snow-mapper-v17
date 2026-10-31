// Snowmapper — Stripe webhook: a payment unlocks Pro, a cancelled
// subscription locks it again (entitlements table, service role).
//
// Events (Stripe Dashboard › Developers › Webhooks › endpoint
// https://<project>.functions.supabase.co/stripe-webhook):
//   checkout.session.completed, customer.subscription.created,
//   customer.subscription.updated, customer.subscription.deleted
//
// SECRETS: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET (whsec_…)
// DEPLOY: supabase functions deploy stripe-webhook --no-verify-jwt
//   (Stripe sends no Supabase JWT; the Stripe signature is the check)
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

// Stripe-Signature: t=…,v1=… -- HMAC-SHA256 of "t.payload", 5 min tolerance
async function verified(payload: string, header: string, secret: string): Promise<boolean> {
  const parts = Object.fromEntries(header.split(',').map((p) => p.split('=') as [string, string]));
  const t = Number(parts.t);
  const sigs = header.split(',').filter((p) => p.startsWith('v1=')).map((p) => p.slice(3));
  if (!t || !sigs.length || Math.abs(Date.now() / 1000 - t) > 300) return false;
  const k = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const mac = new Uint8Array(await crypto.subtle.sign('HMAC', k, new TextEncoder().encode(`${t}.${payload}`)));
  const hex = [...mac].map((b) => b.toString(16).padStart(2, '0')).join('');
  return sigs.some((s) => s.length === hex.length && [...s].reduce((d, c, i) => d | (c.charCodeAt(0) ^ hex.charCodeAt(i)), 0) === 0);
}

// a season pass runs to 31 May of the winter it was bought in
function seasonEnd(now = new Date()): string {
  const y = now.getUTCMonth() >= 5 ? now.getUTCFullYear() + 1 : now.getUTCFullYear();
  return new Date(Date.UTC(y, 4, 31, 21, 59, 59)).toISOString();   // 23:59:59 Zurich (summer time)
}

Deno.serve(async (req) => {
  const secret = Deno.env.get('STRIPE_WEBHOOK_SECRET'), key = Deno.env.get('STRIPE_SECRET_KEY');
  const url = Deno.env.get('SUPABASE_URL'), service = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY');
  if (!secret || !key || !url || !service) return json({ error: 'not configured' }, 503);
  const payload = await req.text();
  if (!(await verified(payload, req.headers.get('Stripe-Signature') ?? '', secret))) return json({ error: 'bad signature' }, 400);

  const ev = JSON.parse(payload);
  const admin = createClient(url, service, { auth: { persistSession: false } });
  const stripe = async (path: string) =>
    (await fetch(`https://api.stripe.com/v1/${path}`, { headers: { Authorization: `Bearer ${key}` } })).json();
  const periodEnd = (sub: any) => sub?.current_period_end ?? sub?.items?.data?.[0]?.current_period_end;
  const upsert = async (row: Record<string, unknown>) => {
    const { error } = await admin.from('entitlements').upsert({ ...row, updated_at: new Date().toISOString() }, { onConflict: 'user_id' });
    if (error) throw error;
  };

  try {
    const o = ev.data?.object ?? {};
    if (ev.type === 'checkout.session.completed') {
      const uid = o.client_reference_id ?? o.metadata?.user_id;
      if (!uid) return json({ ok: true, skipped: 'no user' });
      if (o.mode === 'subscription' && o.subscription) {
        const sub = await stripe(`subscriptions/${o.subscription}`);
        await upsert({ user_id: uid, source: 'stripe', product: o.metadata?.product ?? 'month',
          valid_until: new Date(periodEnd(sub) * 1000).toISOString(),
          stripe_customer: o.customer ?? null, stripe_subscription: o.subscription });
      } else {
        // a one-off season pass; a pass bought while Pro still runs extends nothing beyond 31 May
        const { data: cur } = await admin.from('entitlements').select('valid_until').eq('user_id', uid).maybeSingle();
        const end = seasonEnd();
        await upsert({ user_id: uid, source: 'stripe', product: o.metadata?.product ?? 'season',
          valid_until: cur && cur.valid_until > end ? cur.valid_until : end, stripe_customer: o.customer ?? null });
      }
    } else if (ev.type.startsWith('customer.subscription.')) {
      const uid = o.metadata?.user_id;
      if (!uid) return json({ ok: true, skipped: 'no user' });
      const live = ['active', 'trialing', 'past_due'].includes(o.status) && ev.type !== 'customer.subscription.deleted';
      await upsert({ user_id: uid, source: 'stripe', product: o.metadata?.product ?? 'month',
        valid_until: live ? new Date(periodEnd(o) * 1000).toISOString() : new Date().toISOString(),
        stripe_customer: o.customer ?? null, stripe_subscription: o.id });
    }
    return json({ ok: true });
  } catch (e) {
    console.error('stripe-webhook', e);
    return json({ error: 'failed' }, 500);   // Stripe retries
  }
});
