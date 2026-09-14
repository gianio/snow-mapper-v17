# Supabase — schema, migrations, functions

## Why this exists

Schema changes used to be loose SQL files you were expected to remember to
paste into the dashboard. Nothing recorded which ones had been run.

Checked against production on 14 Sep 2026: the privacy migration **is**
applied (`profiles.email` is correctly column-scoped away from `anon`), the
messaging one is not, and the hardening one is not. None of that was knowable
from the repo — it took a query against the live database to find out, and an
earlier version of this file confidently asserted the opposite.

That is the actual problem. With migrations under the CLI, "is this live?"
becomes `supabase migration list` instead of a memory test.

```
supabase/
├── config.toml          project ref (not a secret)
├── migrations/          applied in filename order, tracked in the database
│   ├── 20260618000000_baseline_schema.sql
│   ├── 20260901000000_privacy_moderation_ratings.sql
│   ├── 20260902000000_messaging.sql
│   └── 20260914000000_harden_functions.sql
└── functions/
    └── delete-account/  account deletion (App Store Guideline 5.1.1(v))
```

`web/reset-posts.sql` is **deliberately not a migration.** It deletes all
reports — an operational tool, run by hand, never automatically.

---

## One-time setup

```bash
brew install supabase/tap/supabase     # or: npm i -g supabase
supabase login
cd ~/snow-mapper-v17
supabase link --project-ref gdtxwowcqtbdkcoksivb
```

### Adopting migrations on a database that already exists

The live database has the baseline **and** the privacy migration applied
(verified 14 Sep 2026); the messaging and hardening migrations are not. The CLI
knows none of this — `supabase_migrations.schema_migrations` does not exist yet,
so nothing has ever been tracked. Tell it what is already there rather than
letting it re-apply blindly:

```bash
supabase migration list        # local files vs what the remote has recorded
```

Two ways forward, and the choice matters:

**Option A — mark the baseline as already applied (cleanest).**

```bash
supabase migration repair --status applied 20260618000000 20260901000000
```

Then `supabase db push` applies only what genuinely has not run — here, the
messaging and hardening migrations.

**Option B — just push everything.** Safe *specifically here*, because all
four migrations are written idempotently — `CREATE TABLE IF NOT EXISTS`,
`ADD COLUMN IF NOT EXISTS`, `DROP POLICY IF EXISTS` before every `CREATE
POLICY`, `CREATE OR REPLACE FUNCTION`, `DROP TRIGGER IF EXISTS`. Re-applying
them is a no-op. Verified by inspection before they were moved here.

```bash
supabase db push
```

Do **not** assume Option B is safe for future migrations. It is only true
while every migration is idempotent, which is a property you have to keep
choosing (see below).

---

## Everyday use

```bash
# new change
supabase migration new add_device_tokens      # creates a timestamped file
$EDITOR supabase/migrations/*_add_device_tokens.sql
supabase db push                              # apply to the linked project

# what is applied where?
supabase migration list

# pull remote drift back into a migration (after a dashboard edit)
supabase db pull
```

### Rules worth keeping

1. **Write every migration idempotently.** `IF NOT EXISTS`, `DROP ... IF
   EXISTS` before `CREATE`, `CREATE OR REPLACE`. It makes a half-applied
   migration recoverable instead of a puzzle.
2. **Never edit an applied migration.** Add a new one. The CLI tracks them by
   filename and will not notice a rewrite.
3. **New table ⇒ RLS in the same migration.** `ENABLE ROW LEVEL SECURITY` plus
   its policies. A table shipped without policies is either wide open or
   totally inaccessible, and both are found by users rather than by you. The
   project has an event trigger (`rls_auto_enable`) that enables RLS on new
   public tables automatically — that is a safety net, not a substitute for
   writing the policies.
4. **Reads need thought too.** Every existing read policy here is
   `USING (true)`, so anything readable is readable by *anyone with the anon
   key* — which now ships inside an app binary. If a new column is sensitive,
   use column-level grants the way the privacy migration does, because RLS is
   row-level and cannot hide a column.
5. **Dashboard edits are drift.** If you must make one, follow with
   `supabase db pull` so the repo catches up.

---

## Edge functions

```bash
supabase functions deploy delete-account
supabase functions list
supabase functions logs delete-account
```

`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are injected by the platform —
do not add them as secrets by hand, and never expose the service role to a
client.

---

## What is in each migration

| File | Contents |
|---|---|
| `20260618000000_baseline_schema.sql` | Core schema: `profiles`, `reports`, `report_comments`, `follows`, `report_reactions`, `groups`, `group_members`, the `handle_new_user` trigger, RLS policies, storage bucket. |
| `20260901000000_privacy_moderation_ratings.sql` | **Verified applied in production, 14 Sep 2026.** §5 stops exposing `profiles.email` and `webauthn_credentials`; plus storage policies and size/mime limits, `bio`/`push_enabled`/`visibility` columns, moderation (`report_flags` + auto-hide at 3 flags), a 20-reports-per-24 h rate limit, per-post condition ratings, own-profile and own-report deletion. |
| `20260902000000_messaging.sql` | `dm_threads` + `dm_messages` with RLS. Until applied, the app hides its messaging UI behind `dmAvailable()`. |
| `20260914000000_harden_functions.sql` | Pins `search_path` on `handle_new_user()` and revokes `EXECUTE` on both trigger functions from `anon`/`authenticated`. Closes the two actionable security advisories below. |

## Known advisories

From `get_advisors` (Sep 2026). The first two are **fixed in
`20260914000000_harden_functions.sql`** and just need pushing:

- ~~`handle_new_user()` is `SECURITY DEFINER` with a **mutable
  `search_path`**~~ — the classic privilege-escalation shape. Fixed.
- ~~`handle_new_user()` and `rls_auto_enable()` are `EXECUTE`-able by `anon`
  via `/rest/v1/rpc/`~~ — both are trigger functions and would error if
  called, but exposure with no upside. Revoked.

Left alone deliberately:

- `spatial_ref_sys` has RLS disabled and PostGIS lives in `public`. This is
  PostGIS's own reference table — low risk, and "fixing" it by moving the
  extension can break existing queries. Recommend leaving it.
- Leaked-password protection is off. One dashboard toggle, and worth enabling
  since the app uses passwords.
- `groups` has no UPDATE or DELETE policy, so nobody can edit or delete a
  group — including its creator. A functional gap rather than a security hole.
