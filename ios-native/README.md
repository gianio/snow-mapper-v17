# Snowmapper — native iOS seed

This is **not** the Capacitor wrapper (that is `apple-app/`, and it is the thing
that ships first). This folder is the seed of the *real* native app — Phase 3 in
`apple-app/ROADMAP.md`.

## Status: pre-written, NOT compiled

Every `.swift` file here was written on Linux, where Apple's frameworks do not
exist. **None of it has been compiled.** SwiftUI, PencilKit and JavaScriptCore
cannot even be imported outside Apple platforms, so there was no way to check it
here beyond care.

What *is* verified:

- the **architecture** — engine in JS, shell in Swift
- the **JS API this consumes** — `tools/test_engine.js` asserts the exact
  surface (`progCell` returning `{like, conf, cm, n}`, `setReports`, …) in CI,
  so the contract these files code against is real and guarded

What is not: the Swift spelling. Expect a first-compile pass on a Mac.

## The idea: one engine, two shells

```
pipeline/interactive_export.py
        │  emits
        ├── app.js      (the whole web app)      -> GitHub Pages, Capacitor
        └── engine.js   (DOM-free scoring only)  -> node tests, JavaScriptCore
                                                       │
                              SnowEngine.swift ────────┘  runs it, no WebView
```

The terrain-similarity model is the part that took longest to get right and is
still being tuned. Porting it to Swift would create two implementations that
drift, and every future tweak would be done twice. So it stays JavaScript and
runs in **JavaScriptCore** — the same engine behind Safari, no WebView, no UI,
App Store legal. The Swift side owns the *shell*: map, gestures, drawing, feed.

What is **not** in the engine, on purpose: `progZones()`. It reads app state and
turns app report objects into zone records — that is glue, not model, so the
native app builds its zone list itself and hands it to `progCell`. Same boundary
`tools/test_model.js` has always used.

## Files

| File | What it is |
|---|---|
| `Sources/SnowEngine.swift` | JavaScriptCore bridge. Loads `engine.js`, exposes `score(aspect:elevation:slope:lat:lon:supporting:conflicting:)` returning a typed `PowderScore`. Serialises onto one queue because `JSContext` is not thread-safe. |
| `Sources/DrawCanvasView.swift` | PencilKit canvas for drawing snow zones. Real pressure/tilt, palm rejection, predictive smoothing, and `drawingPolicy = .pencilOnly` so a finger pans the map instead of drawing — the thing the web tool emulates by hand. |

## Getting it building (on a Mac)

There is no Xcode project here on purpose — a generated `.xcodeproj` is a large
binary-ish file that is painful to review and to keep in sync. Create it once:

1. Xcode → **New Project** → iOS App, SwiftUI, name `Snowmapper`, bundle ID
   `ch.snowmapper.app` (same as `apple-app/capacitor.config.ts`, so both
   builds can share the App Store record — decide which one owns it before you
   upload both).
2. Drag `ios-native/Sources/` into the project.
3. Build `engine.js` and add it as a **bundle resource**:
   ```bash
   python run_interactive.py --split --offline --out-dir /tmp/snowbuild
   # then drag /tmp/snowbuild/engine.js into the Xcode target
   ```
   Or add a build phase that runs the pipeline, so the engine can never go
   stale relative to the web app.
4. Smoke test the bridge before building any UI:
   ```swift
   let engine = try SnowEngine()                       // loads engine.js from the bundle
   let zone = EngineZone(type: "powder", lat: 46.80, lng: 9.83,
                         e0: 1700, e1: 2300, asp: 0, conc: 0.85, ageH: 2)
   let s = engine.score(aspect: 0, elevation: 2000, slope: 30,
                        lat: 46.80, lon: 9.83, supporting: [zone])
   // expect likelihood ≈ 0.98, confidence ≈ 96  (same numbers CI asserts)
   ```
   Those exact values come from `tools/test_engine.js`, so if Swift disagrees
   with them the bridge is wrong, not the model.

## What still has to be decided

- **MapKit or Mapbox.** MapKit is free and native but its raster-overlay story
  is weaker; Mapbox handles custom raster layers and offline regions better but
  costs money and adds a dependency. This decision shapes the whole map layer,
  so it should be made before much UI is written. The app currently uses
  swisstopo WMTS tiles, which both can consume.
- **Which build owns the App Store record** — the Capacitor wrapper ships first;
  the native app eventually replaces it. Same bundle ID means an in-place
  upgrade for testers, but you cannot have both in TestFlight under one ID at
  once.
- **How much of the UI moves at once.** The pragmatic path is a native shell
  hosting the native map + PencilKit drawing, while less-critical screens (feed,
  profile, settings) stay web for a while behind a WebView tab. Ugly on paper,
  but it lets the high-value surfaces go native without a months-long rewrite.

## Next pieces, in payoff order

1. Native map layer (rendering the raster overlay + report markers).
2. Push notifications — APNs registration, a `device_tokens` table, and an Edge
   Function to send. `profTogglePush()` in the web app is currently a stub that
   only flips a CSS class.
3. WidgetKit widget — new snow / reported powder at the home area.
4. Watch app — snow depth and aspect on the wrist, mid-tour, with gloves on.
5. Offline region download (tiles + data for a chosen area).
