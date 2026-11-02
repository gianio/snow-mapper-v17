-- Datenschutz nach dem Schweizer DSG (needs 20261030000000_invites.sql,
-- 20261031000000_user_blocks.sql and 20261031020000_profile_private.sql first).
--
-- 1. my_data_export(): everything stored about the signed-in person, as one
--    JSON document -- the right of access and the right to receive one's data
--    (Art. 25 and 28 DSG). The app offers it as «Meine Daten herunterladen».
-- 2. delete_my_account(): the whole account, login included. Deleting the
--    auth.users row cascades to the profile and through it to posts,
--    comments, reactions, follows, messages, favourites, push subscriptions
--    and blocks; feedback stays without a link to the person (on delete set
--    null). The photos go first, from the app, through the Storage API (each
--    person may delete their own files). The delete-account edge function
--    does the same with the service role and stays the first choice.
-- 3. Retention: invite attempts after 30 days, the push log after 90.
-- 4. Photos stay public by their address, but nobody can list the bucket any
--    more: listing showed every user's file names to anyone.

create or replace function public.my_data_export() returns jsonb
  language plpgsql stable security definer set search_path = public, pg_temp
as $$
declare
  uid uuid := auth.uid();
  doc jsonb;
begin
  if uid is null then raise exception 'not signed in'; end if;
  select jsonb_build_object(
    'exportiert_am', now(),
    'hinweis', 'Alle Daten, die Snowmapper zu deinem Konto speichert (Auskunft und Datenherausgabe nach Art. 25 und 28 DSG). Fotos: die Adressen in image_url bzw. condition_data.',
    'konto', (select jsonb_build_object('id', u.id, 'email', u.email, 'email_bestaetigt', u.email_confirmed_at,
                       'telefon_bestaetigt', nullif(u.phone, ''), 'erstellt', u.created_at,
                       'letzte_anmeldung', u.last_sign_in_at, 'angaben_bei_registrierung', u.raw_user_meta_data)
              from auth.users u where u.id = uid),
    'profil', (select to_jsonb(p) - 'webauthn_credentials' from profiles p where p.id = uid),
    'privat', (select to_jsonb(x) from profile_private x where x.id = uid),
    'beitraege', coalesce((select jsonb_agg((to_jsonb(r) - 'location')
                              || jsonb_build_object('ort', st_asgeojson(r.location)::jsonb) order by r.created_at)
                           from reports r where r.user_id = uid), '[]'::jsonb),
    'kommentare', coalesce((select jsonb_agg(to_jsonb(c) order by c.created_at) from report_comments c where c.user_id = uid), '[]'::jsonb),
    'reaktionen', coalesce((select jsonb_agg(to_jsonb(x) order by x.created_at) from report_reactions x where x.user_id = uid), '[]'::jsonb),
    'bewertungen', coalesce((select jsonb_agg(to_jsonb(x)) from report_conditions x where x.user_id = uid), '[]'::jsonb),
    'gemeldete_beitraege', coalesce((select jsonb_agg(to_jsonb(x) order by x.created_at) from report_flags x where x.user_id = uid), '[]'::jsonb),
    'gemeldete_personen', coalesce((select jsonb_agg(jsonb_build_object('person', x.user_id, 'grund', x.reason, 'am', x.created_at))
                                    from user_flags x where x.reporter_id = uid), '[]'::jsonb),
    'folge_ich', coalesce((select jsonb_agg(jsonb_build_object('id', f.following_id, 'name', p.username, 'seit', f.created_at))
                           from follows f left join profiles p on p.id = f.following_id where f.follower_id = uid), '[]'::jsonb),
    'folgen_mir', coalesce((select jsonb_agg(jsonb_build_object('id', f.follower_id, 'name', p.username, 'seit', f.created_at))
                            from follows f left join profiles p on p.id = f.follower_id where f.following_id = uid), '[]'::jsonb),
    'blockiert', coalesce((select jsonb_agg(jsonb_build_object('id', b.blocked, 'name', p.username, 'seit', b.created_at))
                           from user_blocks b left join profiles p on p.id = b.blocked where b.blocker = uid), '[]'::jsonb),
    'favoriten', coalesce((select jsonb_agg(to_jsonb(x) order by x.created_at) from favorites x where x.user_id = uid), '[]'::jsonb),
    'nachrichten', coalesce((select jsonb_agg(jsonb_build_object(
                       'mit', case when t.user_a = uid then t.user_b else t.user_a end,
                       'mit_name', (select username from profiles where id = case when t.user_a = uid then t.user_b else t.user_a end),
                       'nachrichten', coalesce((select jsonb_agg(jsonb_build_object('von_mir', m.sender_id = uid, 'text', m.body,
                                                   'gesendet', m.created_at, 'gelesen', m.read_at) order by m.created_at)
                                                from dm_messages m where m.thread_id = t.id), '[]'::jsonb)) order by t.created_at)
                     from dm_threads t where uid in (t.user_a, t.user_b)), '[]'::jsonb),
    'feedback', coalesce((select jsonb_agg(to_jsonb(x) order by x.created_at) from feedback x where x.user_id = uid), '[]'::jsonb),
    'push_abos', coalesce((select jsonb_agg(jsonb_build_object('geraet', s.user_agent, 'seit', s.created_at))
                           from push_subscriptions s where s.user_id = uid), '[]'::jsonb),
    'gruppen', coalesce((select jsonb_agg(to_jsonb(x)) from group_members x where x.user_id = uid), '[]'::jsonb)
  ) into doc;
  return doc;
end $$;
revoke execute on function public.my_data_export() from public, anon;
grant execute on function public.my_data_export() to authenticated;

create or replace function public.delete_my_account() returns void
  language plpgsql security definer set search_path = public, pg_temp
as $$
declare uid uuid := auth.uid();
begin
  if uid is null then raise exception 'not signed in'; end if;
  delete from auth.users where id = uid;
end $$;
revoke execute on function public.delete_my_account() from public, anon;
grant execute on function public.delete_my_account() to authenticated;

-- retention: kept only as long as needed
do $$
begin
  if exists (select 1 from cron.job where jobname = 'privacy-retention') then
    perform cron.unschedule('privacy-retention');
  end if;
  perform cron.schedule('privacy-retention', '23 3 * * *', $job$
    delete from public.invite_attempts where at < now() - interval '30 days';
    delete from public.push_log where created_at < now() - interval '90 days';
  $job$);
end $$;

-- photos: public by address, listed only to their owner
drop policy if exists report_images_public_read on storage.objects;
drop policy if exists report_images_owner_read on storage.objects;
create policy report_images_owner_read on storage.objects for select to authenticated
  using (bucket_id = 'report-images' and owner = auth.uid());
