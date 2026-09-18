# Variant A (SNOWPACK ski quality) — integration status and plan

Written 18 September 2026. The pipeline lives on `feat/variant-a-ski-quality`
(one commit, ~2,000 lines) and is **not merged**. This is about getting its
output into the app.

---

## What the pipeline already gives us

`variant_a/export.py` writes, into `outputs/variant_a/`:

| Artifact | Contents |
|---|---|
| `manifest.json` | WGS84 `bounds`, `timestamps`, `tags`, per-layer `file` template + **legend** (label + RGB, straight from `classify.SKI_LABELS`/`SKI_RGBA`), density `range`/`unit`, subregion names |
| `layers/{ski18,simple,density}_<tag>.png` | georeferenced RGBA overlays, one set per timestamp |
| `profiles/profiles.json` | 175 representative points (lat/lon/elev/aspect/slope/tile) × timestamps × 28 depth bins of density + grain class, plus the `grain` legend (PP/DF/RG/FC/DH/SH/MF/IF/FCxr) |

Its README is explicit that this is loose coupling: *"No app model code is
modified."* That is the right shape and this integration keeps it.

## What is now built (app side)

Demo-only, and hidden entirely when the artifacts are absent.

* **`vaLoad()`** fetches `data/variant_a/manifest.json` after first paint.
  On live data it returns immediately without touching the network — the
  exported timestamps belong to the demo window, and a ski-quality layer
  showing March snow in December is worse than no layer.
* **Overlay + three views** (`ski18` / `simple` / `density`) as one toggle with
  an inline sub-picker, because they are three views of one product and only
  one can be on top.
* **Legend comes from the manifest**, never from a copy in the client:
  `classify.py` owns those labels and colours, and a second copy would drift
  silently.
* **Follows the timeline.** `vaRefresh()` runs from `renderAll()` and snaps to
  the nearest exported timestamp, so the layer tracks the scrubber like every
  other time-dependent layer.
* **Snow profile in the existing meteo popup** — not a second window. Density
  and grain type are what you look at next after depth, so they belong in the
  same card, with the grain legend from `profiles.json`.

### One real bug the contract test caught

The profile is interpolated with KNN over the representative points. The first
version used a single national metric (`dx² + dy² + dz² + da²`) and it was
wrong: with 175 points spread over Switzerland, horizontal distance dominates
and elevation is swamped. Measured — a point at 2400 m and the same point at
1000 m returned an **identical** profile, because a 1400 m elevation
difference scored about 2 % of the total.

`variant_a`'s own gridding avoids this by being **subregion-local**: the
horizontal spread is already bounded, so elevation and aspect decide. The
client cannot read the subregion raster (`tile_labels.npy` is not shipped), so
it now does the same thing in two stages — take the 24 horizontally nearest
candidates, then rank those by elevation and aspect. After the fix the same
test gives 243 vs 176 kg/m³.

---

## What is still missing

Ordered by what blocks what.

### 1. The artifacts do not exist anywhere the app can reach — the real blocker

`OUTPUT_DIR` is `outputs/variant_a/`, which is git-ignored. Nothing publishes
it. The app fetches `data/variant_a/...` from its own origin, so the export has
to land in `dist/`.

`deploy.yml` now copies `variant_a_export/` into `dist/data/variant_a/` **if
that directory exists**, and says so in the log when it does not. So the
remaining step is a human one: run the pipeline locally, then commit the export
(or publish it to the data origin).

### 2. The compute cannot run in CI, and that is by design

* `SNOWPACK_BIN` defaults to a hardcoded path on one Mac
  (`/Users/gianimorf/…/snowpack/bin/snowpack`). SNOWPACK and MeteoIO are
  external C++ binaries, not pip packages.
* `NATIONAL_DEM` (`variant_a/data/dem/ch_lv03_250m.dem`) is git-ignored.

For a demo-only layer this is fine: generate once, commit the output. It only
becomes a problem for real-time (below).

### 3. Payload size is unmeasured

Three PNGs per timestamp, national at 250 m (1396 × 884). Few colours, so PNG
should compress well, but the totals depend on how many timestamps get
exported, and `dist` has a **1 GB GitHub Pages ceiling** that already has a
size guard in CI. Worth measuring on the first real export and trimming the
timestep if needed.

### 4. Not verified against real artifacts

`tools/test_variant_a.js` is a **contract** test: it pins the key names and
shapes against a synthetic payload built to match `export.py` and
`profiles.py`. It cannot prove the real files parse, for the same reason the
SLF bulletin parser could not be proven until a runner fetched one. The first
local export is the test.

### 5. Small open ends

* Timestamp alignment: the export is coarser than the app's hourly timeline, so
  the client snaps to the nearest. If the exported window does not overlap the
  demo window at all, every frame maps to the same end — visible as a layer
  that ignores the scrubber.
* `simple` and `density` have no sub-layer labels in the manifest; the client
  supplies German names for the three known keys and falls back to the raw key
  for anything new.

---

## Real-time: is it worth it, and what would it take?

**It is worth it, but not by lifting this pipeline into the deploy.** The
honest reason is in the numbers rather than in the engineering.

### Why the current shape cannot just be scheduled

SNOWPACK is a *seasonal* model: each run needs a spin-up from early winter to
the target date, because the layer stratigraphy — the crusts and buried facets
that the 18-class scheme is actually about — is the accumulated history. That
is exactly what the main app model does not have and why Variant A is
interesting. It is also why it cannot be a stateless 4×/day job: throwing away
state and re-spinning 175 points from November every six hours is both wasteful
and slow.

### The shape that does work

1. **A stateful host.** One small always-on VM (or a container with a
   persistent volume) that keeps `WORK_DIR` — the per-point `.sno` state
   files — between runs. This is the same host `docs/launch-architecture.md`
   Teil B already calls for; Variant A and Alpine3D want the same box.
2. **Incremental runs.** Spin up once per season, then advance each point by
   the new forcing window and write a fresh `.pro`. Cost per run scales with
   the *window*, not the season.
3. **Publish artifacts, not compute.** The host runs `export_all()` and pushes
   `manifest.json` + PNGs + `profiles.json` to the data origin. The app side is
   already written and needs no change — which is the payoff of the loose
   coupling.
4. **Then drop the demo gate** (`vaLoad()`'s `demoActive()` early return) and
   the layer works on live data.

### Order I would do it in

| Step | Effort | Why then |
|---|---|---|
| Local export + commit, demo only | hours (yours) | Proves the contract end to end and makes the layer visible with zero infrastructure |
| Measure payload, trim timestep | hours | Before it touches the Pages budget |
| Stateful host, seasonal spin-up | ~1 week | Shared with Alpine3D; the only real blocker to real-time |
| Incremental scheduled runs + publish | ~1 week | Turns it into a live product |
| Remove the demo gate | minutes | One early return |

Against the December launch: **step 1 is safe** — it is demo-only, hidden when
absent, and touches no model code. Steps 3–4 are the Teil B compute project and
belong after launch, alongside Alpine3D.
