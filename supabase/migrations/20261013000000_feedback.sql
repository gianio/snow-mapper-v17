-- Review mode: feedback from the app (a feature, thumbs up/down, 0-5 stars,
-- text, a screenshot). Anyone may send one, signed in or not; nobody can
-- read them back through the API -- they are read in the dashboard and
-- arrive by mail (edge function feedback-mail).
create table if not exists public.feedback (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  user_id uuid references auth.users(id) on delete set null,
  feature text not null check (char_length(feature) <= 80),
  sentiment text check (sentiment in ('positive', 'negative')),
  stars smallint check (stars between 0 and 5),
  message text check (char_length(message) <= 5000),
  screenshot_path text check (char_length(screenshot_path) <= 200),
  context jsonb,
  mailed boolean not null default false
);
alter table public.feedback enable row level security;

drop policy if exists feedback_insert on public.feedback;
create policy feedback_insert on public.feedback
  for insert to anon, authenticated
  with check ((user_id is null or user_id = (select auth.uid())) and mailed = false);

-- Screenshots: private bucket, images only, 5 MB; upload only.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('feedback', 'feedback', false, 5242880, array['image/jpeg', 'image/png', 'image/webp'])
on conflict (id) do nothing;

drop policy if exists feedback_upload on storage.objects;
create policy feedback_upload on storage.objects
  for insert to anon, authenticated
  with check (bucket_id = 'feedback');
