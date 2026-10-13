// Snowmapper — mail a feedback entry (review mode)
//
// The app inserts a row into public.feedback (and the screenshot into the
// private "feedback" bucket), then calls this function with the row's id.
// The function reads the row with the service role, mails it once and marks
// it mailed, so a repeated call cannot send it twice and the endpoint cannot
// be used to send anything else.
//
// Mail goes through Resend (https://resend.com). Secrets:
//   RESEND_API_KEY   required for mail; without it the row is only stored
//   FEEDBACK_TO      default gianandrimorf@gmail.com
//   FEEDBACK_FROM    default "Snowmapper Feedback <onboarding@resend.dev>"
//                    (resend.dev only delivers to the Resend account's own
//                    address; use a verified domain to send elsewhere)
//
// DEPLOY: supabase functions deploy feedback-mail
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

const cors = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { ...cors, 'Content-Type': 'application/json' } });
const esc = (s: unknown) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]!));

Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: cors });
  if (req.method !== 'POST') return json({ error: 'method' }, 405);
  let id = '';
  try { id = String((await req.json()).id || ''); } catch { /* empty */ }
  if (!/^[0-9a-f-]{36}$/i.test(id)) return json({ error: 'id' }, 400);

  const admin = createClient(Deno.env.get('SUPABASE_URL')!, Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!);
  const { data: fb, error } = await admin.from('feedback').select('*').eq('id', id).maybeSingle();
  if (error || !fb) return json({ error: 'not found' }, 404);
  if (fb.mailed) return json({ mailed: true, again: true });
  // only fresh rows: the id alone must not be a way to mail old entries
  if (Date.now() - Date.parse(fb.created_at) > 15 * 60 * 1000) return json({ error: 'too old' }, 409);

  const key = Deno.env.get('RESEND_API_KEY');
  if (!key) return json({ stored: true, mailed: false });

  let shot = '';
  if (fb.screenshot_path) {
    const { data } = await admin.storage.from('feedback').createSignedUrl(fb.screenshot_path, 60 * 60 * 24 * 30);
    shot = data?.signedUrl || '';
  }
  let who = 'nicht angemeldet';
  if (fb.user_id) {
    const { data: p } = await admin.from('profiles').select('username').eq('id', fb.user_id).maybeSingle();
    who = p?.username ? '@' + p.username : fb.user_id;
  }
  const mood = fb.sentiment === 'positive' ? '👍 positiv' : fb.sentiment === 'negative' ? '👎 negativ' : '–';
  const stars = fb.stars != null ? '★'.repeat(fb.stars) + '☆'.repeat(5 - fb.stars) : '–';
  const ctx = fb.context ? JSON.stringify(fb.context, null, 2) : '';
  const html = `<div style="font-family:system-ui,sans-serif;font-size:15px;line-height:1.5">
<h2 style="margin:0 0 8px">Feedback: ${esc(fb.feature)}</h2>
<p><b>Stimmung:</b> ${mood} &nbsp; <b>Bewertung:</b> ${stars}<br><b>Von:</b> ${esc(who)} &nbsp; <b>Zeit:</b> ${esc(fb.created_at)}</p>
${fb.message ? `<p style="white-space:pre-wrap;background:#f4f6f9;padding:12px;border-radius:8px">${esc(fb.message)}</p>` : ''}
${shot ? `<p><a href="${esc(shot)}">Screenshot ansehen</a> (Link 30 Tage gültig)</p><img src="${esc(shot)}" style="max-width:100%;border-radius:8px">` : ''}
${ctx ? `<details><summary>Kontext</summary><pre style="font-size:12px">${esc(ctx)}</pre></details>` : ''}
</div>`;
  const r = await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      from: Deno.env.get('FEEDBACK_FROM') || 'Snowmapper Feedback <onboarding@resend.dev>',
      to: [Deno.env.get('FEEDBACK_TO') || 'gianandrimorf@gmail.com'],
      subject: `Snowmapper Feedback · ${fb.feature} · ${mood}${fb.stars != null ? ' · ' + fb.stars + '/5' : ''}`,
      html,
    }),
  });
  if (!r.ok) return json({ stored: true, mailed: false, error: 'mail ' + r.status }, 502);
  await admin.from('feedback').update({ mailed: true }).eq('id', id);
  return json({ stored: true, mailed: true });
});
