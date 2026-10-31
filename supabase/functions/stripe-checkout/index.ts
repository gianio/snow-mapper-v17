// Snowmapper — Stripe Checkout for Pro (web only; the iOS app uses Apple's
// in-app purchase, see docs/paywall.md)
//
// POST { product: 'season' | 'month' } with the user's own JWT
//   -> { url }  (the Stripe-hosted payment page; the app sends the person there)
// The user id travels as client_reference_id and metadata, so the webhook
// (stripe-webhook) knows whom to unlock -- never from the request body.
//
// SECRETS (supabase secrets set …): STRIPE_SECRET_KEY, STRIPE_PRICE_SEASON,
// STRIPE_PRICE_MONTH, APP_URL (optional, default the GitHub Pages URL).
// DEPLOY: supabase functions deploy stripe-checkout
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

const cors = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { ...cors, 'Content-Type': 'application/json' } });

Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: cors });
  if (req.method !== 'POST') return json({ error: 'method not allowed' }, 405);

  const key = Deno.env.get('STRIPE_SECRET_KEY');
  const prices: Record<string, string | undefined> = {
    season: Deno.env.get('STRIPE_PRICE_SEASON'),
    month: Deno.env.get('STRIPE_PRICE_MONTH'),
  };
  const app = Deno.env.get('APP_URL') ?? 'https://gianio.github.io/snow-mapper-v17/';
  const url = Deno.env.get('SUPABASE_URL'), service = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY');
  if (!key || !url || !service) return json({ error: 'stripe not configured' }, 503);

  const auth = req.headers.get('Authorization') ?? '';
  if (!auth.startsWith('Bearer ')) return json({ error: 'missing bearer token' }, 401);
  const admin = createClient(url, service, { auth: { persistSession: false } });
  const { data: u, error: authErr } = await admin.auth.getUser(auth.slice(7));
  const user = u?.user;
  if (authErr || !user) return json({ error: 'invalid or expired token' }, 401);

  const { product } = await req.json().catch(() => ({ product: null }));
  const price = prices[String(product)];
  if (!price) return json({ error: 'unknown product' }, 400);
  const mode = product === 'month' ? 'subscription' : 'payment';

  // a returning buyer keeps their Stripe customer (receipts, portal)
  const { data: ent } = await admin.from('entitlements').select('stripe_customer').eq('user_id', user.id).maybeSingle();

  const body = new URLSearchParams({
    mode,
    'line_items[0][price]': price,
    'line_items[0][quantity]': '1',
    client_reference_id: user.id,
    'metadata[user_id]': user.id,
    'metadata[product]': String(product),
    success_url: app + '?pro=ok',
    cancel_url: app + '?pro=cancel',
    allow_promotion_codes: 'true',
    locale: 'de',
  });
  if (ent?.stripe_customer) body.set('customer', ent.stripe_customer);
  else if (user.email) body.set('customer_email', user.email);
  if (mode === 'subscription') {
    body.set('subscription_data[metadata][user_id]', user.id);
    body.set('subscription_data[metadata][product]', String(product));
  } else if (!ent?.stripe_customer) {
    body.set('customer_creation', 'always');
  }

  const r = await fetch('https://api.stripe.com/v1/checkout/sessions', {
    method: 'POST',
    headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  });
  const s = await r.json();
  if (!r.ok || !s.url) return json({ error: s?.error?.message ?? 'stripe error' }, 502);
  return json({ url: s.url });
});
