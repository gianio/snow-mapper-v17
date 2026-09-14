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

## Getting it running on a Mac

One command:

```bash
cd ios-native && ./bootstrap.sh
```

That checks your toolchain (and tells you exactly what to install if something
is missing), generates `Resources/engine.js`, generates `Snowmapper.xcodeproj`
from `project.yml`, and opens Xcode. **No Apple Developer account needed** —
simulator builds require no signing; only shipping to a device or TestFlight
does.

Prerequisites it will check for you: full **Xcode** (not just Command Line
Tools — those have no iOS SDK), **XcodeGen** (`brew install xcodegen`, it offers
to do this), and **python3** (macOS ships one).

Or run the tests straight from the command line:

```bash
xcodebuild test -project ios-native/Snowmapper.xcodeproj -scheme Snowmapper \
  -destination 'platform=iOS Simulator,name=iPhone 16'
```

### Why there is no `.xcodeproj` in git

It is generated from `project.yml` by XcodeGen. A checked-in `.xcodeproj` is a
large, merge-hostile bundle meant to be edited through a GUI; ~60 lines of YAML
is reviewable in a diff, cannot drift from the repo, and lets targets, build
settings, Info.plist keys and entitlements be changed as ordinary code. It also
means no regex-patching of `project.pbxproj` — compare `apple-app/scripts/
apply-ios-config.py`, which has to do exactly that for the Capacitor path.

The tradeoff: **changes made in Xcode's project-settings GUI are overwritten on
the next generate.** Edit `project.yml` instead.

### `engine.js` is generated, not committed

`tools/make_engine.py` emits it using **stdlib python only** — deliberately, so
a Mac does not need numpy, rasterio, pyproj, scipy and GDAL installed just to
produce a 10 KB JavaScript file. `tools/test_engine_extract.py` asserts that
path produces byte-identical output to the full pipeline, so the iOS app can
never ship an engine that differs from the one the web app uses.

## What the first screen is

A deliberate **dev harness**, not the product — it exercises the three things
that cannot be verified without real hardware:

| Section | What it proves | Expected |
|---|---|---|
| **Engine** | The JavaScriptCore bridge loads `engine.js` on-device and agrees with node | `0.982` / `96%` — the values `tools/test_engine.js` pins. A mismatch means the *bridge* is wrong, not the model. |
| **Haptics** | Every CoreHaptics pattern, so it can be felt | Nothing — the intensity/sharpness values are **untuned guesses**. This is where you fix them. |
| **Draw** | PencilKit with a real Apple Pencil | Needs an **iPad + Pencil**. No simulator produces pressure or tilt. |

The real UI comes after the MapKit-vs-Mapbox decision, which shapes the whole
map layer.

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
