# Snowmapper iOS — roadmap

`README.md` in this folder is the **how** (commands, signing, App Store steps).
This file is the **what and when**: what is already done, what can be run today,
and what still needs a Mac or genuine native work.

Status: September 2026.

---

## The honest framing

A Capacitor wrapper is a **shipping probe, not the product**. It gets the app
into TestFlight in days and flushes out App Review, signing, privacy labels and
the compliance list while costing almost nothing. But it does not get you the
things that would make this a *good* iOS app — Apple Pencil in the draw tool,
push notifications, a widget, a Watch app. Those need native work (Phase 3).

The design is also still moving fast. The web app is the cheapest place to
settle it; rewriting a moving target in Swift means rewriting it repeatedly.
So: probe with the wrapper, settle the design on the web, nativise what is
settled.

---

## Phase 1 — done, no Mac needed ✅

All of this is in the repo and runs on Linux/CI today.

| Item | Where |
|---|---|
| Capacitor 6 config, plugins, npm scripts | `capacitor.config.ts`, `package.json` |
| App identity = **Snowmapper** / `ch.snowmapper.app` | `capacitor.config.ts`, `ios-config/Info.plist.additions.xml` |
| Web assets built from the same Python pipeline as the website | `scripts/build-web.mjs` |
| Icon + splash generated from the **real brand artwork** | `scripts/make-resources.py` → `resources/` |
| Info.plist purpose strings + privacy manifest applied automatically | `scripts/apply-ios-config.py` |
| Fresh-data-at-launch with offline fallback | `SNOW_REMOTE_DATA_BASE`, see below |
| macOS build → TestFlight, without a Mac on your desk | `.github/workflows/ios-testflight.yml` |
| Native haptics, status bar, splash, share (auto-detected) | already in the web app (`_capPlugin`) |
| Auth that works in a WKWebView | already email + 6-digit code, **no magic links** |

Fixed along the way: the identity was still `Snow Model` / `ch.snowmodel.app`,
and `make-resources.py` was drawing a charcoal snowflake from the app's older
monochrome era — so the iOS icon would not have matched the icon users already
know from the web app. Both now come from `pipeline/assets/brand_icon.png`.

### Data delivery — set this before any real build

A shipped binary carries a forecast frozen at build time, and refreshing it
must not mean an App Store release. The boot loader therefore tries a hosted
origin first and falls back to the copy bundled in the binary (so the very
first launch works with no signal at all):

```bash
cd apple-app
SNOW_REMOTE_DATA_BASE=https://gianio.github.io/snow-mapper-v17 npm run build:web
```

Unset, `build:web` warns and the app ships bundled-only — fine for a smoke
test, **not** for TestFlight. Point it at the model host once that exists
(see `docs/launch-architecture.md`, Teil B).

Behaviour is verified for four cases: web build unchanged, native+remote up,
native+remote offline, and native where the remote pointer resolves but the
blob 404s (falls back as a whole, never mixing bases).

---

## Phase 2 — needs a Mac or the CI workflow 🍎

This is the only hard gate: generating the native project, signing, uploading.

1. **Apple Developer Program** — 99 $/yr, and an App Store Connect app record
   with bundle ID `ch.snowmapper.app`.
2. **Generate + build**: either locally (`npm run ios`, see README) or run
   `.github/workflows/ios-testflight.yml` with `upload: false` — that proves the
   whole chain on a hosted Mac with **no secrets required**.
3. **Signing secrets** for `upload: true`:
   `APPLE_CERTIFICATE_P12`, `APPLE_CERTIFICATE_PASSWORD`,
   `APPLE_PROVISIONING_PROFILE`, `APPSTORE_API_KEY_JSON`.
4. **TestFlight**: internal testers (≤100) install immediately, no review.
   That is the "test app ready for the App Store" milestone.

### Blockers for App Store *release* (not TestFlight)

| Blocker | Why |
|---|---|
| **Account-deletion Edge Function** | Guideline 5.1.1(v) requires real deletion. The client deletes content but cannot remove the `auth.users` row — that needs the service role. **Hard rejection risk.** |
| Privacy Policy + Support URL | Must be hosted and linked in App Store Connect |
| App Privacy labels | Mirror `ios-config/PrivacyInfo.xcprivacy` |
| Demo account for App Review | A test login reviewers can use |
| Guideline 4.2 "minimum functionality" | Thin webview wrappers do get rejected. Mitigation: lean on the real native integrations + offline data, and land Phase 3 items sooner rather than later. |

---

## Phase 3 — what makes it actually native 🎯

Ordered by payoff. Each is independent; none requires a big-bang rewrite.

1. **PencilKit draw tool.** Drawing snow zones is the core input mechanism and
   the code already reaches for pressure (`drawBrushSize`). Native gives real
   pressure, tilt, palm rejection and predictive stroke smoothing. Biggest win.
2. **Push notifications (APNs).** `profTogglePush()` is currently a stub that
   flips a CSS class — the hint text admits it. "Fresh powder reported near your
   home area" is the notification that makes the app sticky.
3. **Native map layer.** MapKit or Mapbox rendering the raster overlay, with
   UIKit gesture recognizers. We spent a whole round fixing two-finger pan lag
   in the draw tool with rAF throttling — that was the webview ceiling, not a bug.
4. **WidgetKit widget.** New snow / reported powder at your home area, glanceable.
5. **Apple Watch app.** Snow depth and aspect on the wrist mid-tour, with gloves
   on. Genuinely differentiating for ski touring.
6. **Offline region download.** Pick a touring area, download tiles + data before
   you leave signal. Painful via service worker, straightforward natively.
7. Later: Live Activities, App Intents/Siri, Handoff, share extension.

### The architectural move that keeps this tractable

**Do not rewrite the engine.** Split engine from shell:

- **Engine stays shared.** Much of what the JS computes at runtime can move into
  the Python pipeline (it already builds the grid and raster data). What must
  stay client-side is reports-matching (`progCell` against live reports) — port
  that to Swift, or run the existing JS headlessly in **JavaScriptCore** (no
  webview, same engine Safari uses, App Store legal) to keep one source of truth
  for the physics.
- **Shell goes native.** SwiftUI navigation, native map, PencilKit, Swift Charts
  for the timeline, SwiftData for offline-first reports with a sync queue.

Otherwise the codebase forks and every future change has to be made twice, in
two languages, with the iOS half requiring a Mac.

---

## Database — unchanged, with three iOS-specific notes

Supabase stays exactly as it is (project `Snowmapper v17`, eu-central-1). The
iOS app talks to the same API. No second backend, no local DB, no data move.

1. **RLS becomes the only security boundary.** The anon key now ships inside an
   app binary where anyone can extract it. RLS is already enabled on every app
   table (`profiles`, `reports`, `report_comments`, `follows`,
   `report_reactions`, `groups`, `group_members`) — which is why that is safe —
   but it deserves an audit pass before release.
2. **Photo uploads — already handled.** I previously listed client-side
   downscaling as a to-do here; that was wrong. `downscaleImage()` already runs
   at every one of the six upload sites (1600 px long edge, JPEG q=0.85, EXIF
   orientation honoured, skips re-encoding when it would not help). Native
   camera sizes are covered.
3. **Offline writes.** Reports currently go straight to Supabase. In the
   mountains they should queue locally and sync — natural fit with SwiftData.

**Account deletion is now implemented**: `supabase/functions/delete-account/`
verifies the caller's own JWT, clears their storage, deletes the profile, and
then deletes the `auth.users` row (the part needing the service role). The
client calls it and falls back to content-only deletion if it is not deployed,
so it is safe to ship before deploying. **It still has to be deployed** —
`supabase functions deploy delete-account` — or the release blocker stands.

Still missing in the DB: `dm_threads` / `dm_messages` (verified absent), so the
messaging screen stays hidden behind `dmAvailable()`. Run
`web/migration-messages.sql` to unhide it.

---

---

## What only you can do — the unblock list

Everything on this list needs a human with an account, a card, or a Mac. Until
these exist, no amount of code gets the app into TestFlight. Ordered so the
cheapest and most blocking come first.

| # | What | Why it's yours | Blocks |
|---|---|---|---|
| 1 | **Run `ios-testflight.yml` with `upload: false`** | Needs a click in the Actions tab | Nothing — free, no account, no secrets. Proves the whole chain on a hosted Mac. **Do this first.** |
| 2 | **Deploy the delete-account function**: `supabase functions deploy delete-account` | Needs your Supabase login/CLI | App Store release (Guideline 5.1.1(v)) |
| 3 | **Confirm the bundle ID** — `ch.snowmapper.app`, or tell me otherwise | Must be a domain you control | Everything downstream; changing it later means a new App Store record |
| 4 | **Apple Developer Program**, 99 $/yr | Card + identity verification, 24–48 h | TestFlight and everything after |
| 5 | **App Store Connect app record** with that bundle ID | Needs the enrolled account | Upload |
| 6 | **Signing assets** → four repo secrets: `APPLE_CERTIFICATE_P12`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_PROVISIONING_PROFILE`, `APPSTORE_API_KEY_JSON` | Generated in your Apple account; must never be in the repo | `upload: true` |
| 7 | **Host a Privacy Policy + Support URL** | Your legal text, your domain | App Store review |
| 8 | **App Privacy labels** in App Store Connect (mirror `ios-config/PrivacyInfo.xcprivacy`) | Only the account holder can | App Store review |
| 9 | **A demo account** for App Review to log in with | Your call what data it shows | App Store review |
| 10 | **Set `SNOW_REMOTE_DATA_BASE`** to the real data origin (default assumes `https://gianio.github.io/snow-mapper-v17`) | Depends on where you host data | Fresh forecasts in the shipped app |
| 11 | **MapKit vs Mapbox** decision | Cost/dependency call | Phase 3 native map |
| 12 | *(optional)* Run `web/migration-messages.sql` | Your database | Unhides the messaging screen |

Items 1, 2, 3 and 12 cost nothing and unblock the most. Item 4 has the longest
lead time, so start it early even if nothing else is ready.

Once 1–6 are done I can drive the rest: the workflow builds, signs and uploads,
and I can iterate on failures without a Mac.

## What I would do next

1. You: item 1 (`upload: false` run) and item 2 (deploy the function).
2. Me: react to whatever that build surfaces — the first `cap add ios` on a real
   runner is where unknowns show up.
3. You: items 3–5 (bundle ID, enrolment, App Store record) — longest lead time.
4. Me: Phase 3, starting with the native map decision and the PencilKit canvas,
   both seeded in `ios-native/`.
