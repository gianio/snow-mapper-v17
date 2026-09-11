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
2. **Photo uploads.** Native camera images are far larger than web-picked ones.
   Downscale client-side (~1600 px) before hitting Storage, or upload
   cost/latency spikes. Already on the Teil A P1 list.
3. **Offline writes.** Reports currently go straight to Supabase. In the
   mountains they should queue locally and sync — natural fit with SwiftData.

Still missing in the DB: `dm_threads` / `dm_messages` (verified absent), so the
messaging screen stays hidden behind `dmAvailable()`. Run
`web/migration-messages.sql` to unhide it.

---

## What I would do next

1. Run `ios-testflight.yml` with `upload: false` — proves the chain on a hosted
   Mac, needs no Apple account and no secrets. Cheapest possible next step.
2. In parallel: the account-deletion Edge Function, since it is the one hard
   rejection blocker and is independent of everything else.
3. Then Apple Developer enrolment → TestFlight with internal testers.
4. Then Phase 3, starting with PencilKit and push.
