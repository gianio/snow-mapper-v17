# SLF parity and the two-layer split

Written 14 September 2026. Two decisions, measured rather than guessed.

1. Make the snow forecast behave more like SLF's operational map (radiation,
   wind, grid).
2. Separate the **computed** field from the **displayed** layer, so 250 m
   (basic) and 50 m (pro) are genuinely different products and the paywall is
   enforceable.

---

## Part 1 — what SLF actually does, and where we diverge

SLF's operational snow maps (OSHD) work like this:

> ICON weather input downscaled from **1 km to 250 m**, radiation dynamically
> adjusted using a **25 m** terrain model for shading and slope inclination,
> then smoothed for cartographic representation.

Their climatological grids (SPASS, OSHD-EKF) are 1 km. Alpine3D at 25–100 m is
research-scale on single catchments, never national. **250 m is the operational
resolution** — so "basic 250 m" matches SLF exactly and "pro 50 m" would be
finer than SLF publishes for the country.

### The gap, honestly

| | SLF OSHD | Snowmapper today |
|---|---|---|
| Weather input | ICON-CH1, 1 km | Open-Meteo, 0.03° ≈ 3 km sample grid |
| Model grid | 250 m | 3 km |
| Radiation | drives the mass balance, 25 m terrain correction | **computed, but only drives powder *quality* and the hillshade** |
| Melt | energy balance (SNOWPACK) | **none** |
| Settling | yes | **none** |
| Wind redistribution | full saltation/suspension | curvature + lee heuristic |
| Station assimilation | ~350 IMIS/MeteoSwiss stations | none |

### The single biggest divergence is not resolution

`interactive_export.py:3321`:

```js
for(let t=0;t<T;t++) cum[o1+p] = cum[o0+p] + SNOW[s+p]*sc;
```

Snow depth is a **pure running sum over 264 hours with no ablation at all.**
Nothing melts, nothing settles. Over an 11-day window in April, a south-facing
35° slope at 1800 m keeps every centimetre it ever received, identical to a
north face. That is the thing a Swiss ski tourer notices immediately, and no
amount of extra resolution fixes it.

### The good news

The radiation machinery already exists and is sound. `computeRad(doy)`
(`interactive_export.py:3874`) already does solar declination, an hour-angle
loop, **horizon shading via the 12-azimuth `RHOR` field**, slope/aspect
incidence angle, air mass with atmospheric transmissivity, and beam + diffuse
with a sky-view factor. The `radsun` layer even modulates it by the hourly
`SUN` field for cloud cover (`:3898`).

That is structurally the same correction SLF applies. It simply never reaches
the mass balance.

### Phase 1 — connect radiation to the mass balance (days, high value)

Add an hourly **ablation** field alongside `SNOW`, computed in the pipeline
where the fine terrain already lives, using an enhanced
temperature-radiation-index melt model:

```
melt_mm_we/h = TF·max(0, T)  +  SRF·(1 − α)·I
```

with `I` the terrain-corrected shortwave input (the existing radiation
calculation, cloud-modulated by `SUN`) and `α` a snow albedo that decays with
age. This is the Hock (1999) / Pellicciotti et al. (2005) formulation —
markedly better than a degree-day index because it resolves aspect, far cheaper
than an energy balance, and standard in the literature.

Plus exponential **settling** of fresh snow depth, which is what makes a
90 cm dump read 60 cm three days later.

Why a separate field rather than netting it into `SNOW`: `SNOW` is `uint8` and
cannot carry negatives, and several places legitimately want *new* snow
(`:3478`, `:4577`). So ship `ABL` and change one line:

```js
cum[o1+p] = Math.max(0, cum[o0+p] + SNOW[s+p]*sc - ABL[s+p]*ascale);
```

Cost: one extra `T × cells` cube. **Measured: +116 KB gzipped (+2.3 %)** —
far below the ~0.5 MB I estimated, because ablation is a smooth field and
compresses well. No refactor, and no initialisation-order problem (unlike
deriving it from `radCS`, which is built *after* the `cum` loop).

#### Implemented, and what it actually changed

`model/ablation.py` + `config/model_params.yaml` (`ablation:` block) +
`tools/test_ablation.py` (29 checks). Verified on a real build:

| | Naive running sum | With ablation |
|---|---|---|
| Mean depth after 264 h | 16.0 cm | **6.7 cm** |
| Peak depth | 36 cm | 19 cm |

Ablation removes 58 % of what the pure sum reported, and the client's
`max(0, cum + SNOW − ABL)` reproduces the pipeline exactly (verified: no
negatives, cube length exactly `T × cells`).

Unit-test behaviour at 35° in mid-April, which is the point of the exercise:

| Aspect | Melt | Depth left of 150 cm |
|---|---|---|
| South, cloudless | 5.6 cm/day | 38 cm |
| North, cloudless | 2.7 cm/day | 67 cm |
| South, 50 % sun | 3.4 cm/day | 59 cm |

#### The catch — and it reorders the plan

**On the current 3 km grid the aspect signal does not survive.** Measured on
the real gridded output: south/north ablation ratio **0.99×**, i.e. none.

The reason is not the physics — it is that a 3–4 km cell has a maximum slope
of about **8°** after smoothing. There is no steep south face left in the data
for the radiation term to act on. The unit tests show 2× at 35°; the grid
cannot represent 35°.

So Phase 1 alone delivers the thing users would notice first — snow depth that
stops growing monotonically for eleven days, and settles like real snow — but
**not** the aspect differentiation that makes SLF's map look the way it does.
That needs Phase 2. The grid work is therefore not cosmetic refinement; it is
what unlocks the physics that is now in place.

### Phase 2 — grids (weeks)

| Change | From | To | Measured cost |
|---|---|---|---|
| Weather sample grid | 0.03° (~3 km) | 0.01° (~1 km), matching ICON-CH1 | more API calls |
| Model grid | 3 km | 250 m | 500 m took 186 s; 250 m ≈ 10 min, inside the 60 min budget |
| Radiation grid `_RAD_RES` | 1000 m | 250 m | shading starts resolving couloirs |

ICON-CH1 at 1 km is the hard ceiling on *weather*. Below that, extra resolution
buys terrain-driven detail (aspect, slope, shelter, shading) — which is real,
and is exactly what SLF's 25 m radiation correction is for — but not finer
weather. Say so in the UI rather than implying otherwise.

### Out of scope

Full energy balance **is** SNOWPACK, and station assimilation needs IMIS
access. Both belong to the Alpine3D project, not here.

---

## Part 2 — computed truth vs displayed layer, with a real paywall

### The problem with the obvious approach

A 50 m static terrain field is about 6 MB as a PNG (measured: `ELEVPNG` at
`png_w=6980` = 5.64 MB). If that ships to every client and pro is a UI flag,
the paywall lasts exactly as long as it takes to open the network tab.

### The split

```
PIPELINE (server, 4×/day)
  model @ 250 m + fine terrain @ 50 m  ─────►  TRUTH, stays server-side
        │
        ├─► basic: 250 m raster   ──► public data/, cacheable, bundled offline
        └─► pro:   50 m raster    ──► private bucket, tiled, gated per session
```

**Truth** is the pipeline's internal field. It is never shipped whole, at
either tier. What ships is a *rendering* of it.

**Basic (250 m)** stays exactly like today's delivery: public, CDN-cached,
bundled into the binary so the first launch works with no signal. This matches
SLF's own resolution, so the free tier is not a crippled product.

**Pro (50 m)** is served as **static tiles from a private bucket behind a
short-lived signed URL.**

### Why tiling is cheap *here* specifically

Earlier I costed tiling at gigabytes. That was wrong: I multiplied by 264
hours. The fine field is **static terrain** — no time dimension — so it is
about **560 tiles at z11**, generated once per deploy, not a million.

Zoom levels map onto the tiers almost exactly (at 46.8°N):

| Zoom | Ground resolution | Tiles over CH | Tier |
|---|---|---|---|
| z9 | 209 m/px | 40 | basic ≈ 250 m |
| z11 | 52 m/px | 560 | pro ≈ 50 m |

So "basic vs pro" is *which zoom levels your token authorises*. One policy
clause, not a second pipeline.

### Why this makes the paywall actually work

| | One 6 MB PNG | 560 gated tiles |
|---|---|---|
| Requests to scrape everything | 1 | 560, all authenticated |
| Rate-limitable | no | yes |
| Detectable | no | yes — it looks nothing like browsing |
| Revocable mid-abuse | no | yes |

**Gate once, serve many**: an Edge Function checks the tier and returns a
signed URL per session; the CDN serves the bytes. Auth cost scales with
sessions, not tiles — which matters, because Supabase's free tier is 500 K
function invocations and one pan is ~6 tiles.

### The honest limit

Any client-rendered layer is ultimately extractable. A determined pro
subscriber can reassemble the tiles. The realistic goal is *more effort than a
subscription is worth, and visible when it happens* — which is the same
position every map provider is in.

**The genuinely defensible moat is server-side computation.** If what pro buys
is a *derived answer* — the powder-quality score, the ski-quality layer, a
route rating — computed server-side and returned only for the area asked about,
there is no underlying field to steal. That, plus forecast horizon and history,
is a better paid tier than pixels.

### Free win, independent of all the above

Terrain is static but currently bundled into the blob that refreshes 4×/day,
so every refresh re-downloads 3.2 MB of mountains that have not moved. Split
`terrain-<hash>` (immutable, cached forever) from `snowdata-<stamp>`
(4×/day) and the recurring download gets **smaller than today**, even at 50 m.
Worth doing at the current resolution regardless.

---

## Order of work

---

## Measured: what the grid actually costs

`build_interactive_data` used to hold six `T x cells` **float32** cubes plus a
reprojected copy of each. Measured at 250 m, that is 1.30 GB per cube and per
copy — the run was **killed by the OOM killer after 601 s at 12.7 GB peak.**
250 m was not slow, it was impossible.

Fixed by reprojecting and quantising **per hour, straight into uint8**
(`_quantisers()`), so no float32 cube is ever held. Output is unchanged —
same operation order (reproject, then quantise), verified identical (depth
mean 6.7 cm, peak 19 cm, before and after).

| Resolution | Before | After |
|---|---|---|
| 4 km | 96 s | 94 s |
| 500 m | ~186 s, ~8 GB | **216 s, 4.07 GB, 38.5 MB gz** |
| 250 m | **OOM at 601 s / 12.7 GB** | **649 s, 8.12 GB, 93.6 MB gz — completes** |

250 m now finishes inside the 60 minute deploy timeout. 8.12 GB peak leaves
usable headroom on a 16 GB runner but not a lot; if it ever needs more, the
next step is spatial block processing (`pipeline/switzerland.py` already has
`_run_tiled` for the non-interactive path).

The extra ~30 s at 500 m is the new horizon computation, not the refactor.

**Delivery is the limit, not compute.** Measured at 250 m: the blob is
**93.6 MB gzipped** and the intermediate uncompressed JSON is **2.2 GB** on
disk. So 250 m needs the tiled *delivery* from Part 2; **500 m is the highest
resolution that ships as a single blob**, and at 38.5 MB even that wants the
static-terrain split first.

### Does the aspect signal actually appear? Yes — measured on real terrain

The earlier 0.99 x null result was not a synthetic-DEM artifact. Repeated on
**real Copernicus DEM** over a 24 x 24 km box at Davos/Parsenn, 11 days of
mid-April forcing at 70 % sunshine, with horizon shading on:

| Resolution | Slope p50/p90/max | Cells >= 25 deg | S/N ablation | Depth left |
|---|---|---|---|---|
| 3 km | 4 / 6 / **8 deg** | **0 of 64** | immeasurable | — |
| 250 m | 19 / 29 / **42 deg** | **1,984 of 9,216 (22 %)** | **1.20 x** | S 52 cm, N 68 cm |

At 3 km, real Alpine terrain flattens to a maximum of 8 degrees. There is not
a single cell above 25 degrees anywhere in the Davos box -- the aspect physics
has literally nothing to act on, which is why the national run measured 0.99 x.

At 250 m, 22 % of cells are steep, and the south/north difference is real:
**1.20 x more ablation on south faces, 16 cm less snow left after eleven
days.** Less than the 2 x from the idealised 35 degree unit test, and that is
correct rather than disappointing -- this is a real mixed-aspect population
including gentler slopes, with cloud cover and with horizon shading
suppressing radiation on *both* aspects in the valleys.

So the chain works end to end: ablation physics + horizon shading + a 250 m
grid together produce the aspect differentiation that makes SLF's map look the
way it does, and none of the three is sufficient alone.

### Radiation: horizon shading now feeds the mass balance

`_horizon_angles()` was factored out of `_radiation_inputs()` and is now also
computed on the **model grid** and passed into the ablation — so a north face
in a valley that gets no sun in January no longer melts snow it never
received. It is **not shipped**: the blob grows by zero bytes, because only
the mass balance needs it. The shipped `RHOR` field still serves the display
layer alone.

Raising `_RAD_RES` to 250 m was the original plan and was **rejected on
measurement**: `RHOR` is `K x cells`, so at 250 m it would add ~16 MB of
base64 to every blob for a display layer. Server-side horizon gets the
physics for free instead.

The horizon search was also resolution-dependent by accident — a fixed 25
*cells*, so 25 km at 1 km resolution and 6 km at 250 m. It is now specified
in **metres** (20 km, denser sampling near, coarser far), which is what
makes it physically meaningful at any grid.

---

## Part 3 — the bulletin, the routes, and scoring a tour

### Licences, checked

| Source | Licence | Obligation |
|---|---|---|
| SLF avalanche bulletin (`aws.slf.ch/api/bulletin`) | **CC BY 4.0** | name SLF; same terms as the IMIS feed already shipping |
| SLF IMIS measurements | CC BY 4.0 | already attributed |
| swisstopo ski/snowshoe routes | **OGD** — free for any purpose, commercial included | "Source: Federal Office of Topography swisstopo" or "© swisstopo" |
| BAFU wildlife rest zones | OGD | "© BAFU" |
| Bulletin archive | since 1 Jan 1998 | via SLF archive |
| Accident data (EnviDat) | free, own terms | read them; not CC BY by default |

Worth mailing `data@slf.ch` to join the "Avalanche Bulletin" list for terms
and outage notices.

### The line we do not cross

The app **displays** SLF's bulletin, attributed, linked to the original. It
does **not** compute a danger level of its own. That distinction is the whole
liability argument: a viewer is defensible, a forecaster is not. It is
enforced in code — `slf_bulletin.py` only parses and renders, and
`tour_score.py` consumes the bulletin's own core zone rather than deriving
anything.

### Why a powder score needs the bulletin first

A powder score is a *quality* metric. Left alone it would light up a 38°
north face at danger level 3 — steering people into exactly the core zone the
bulletin warns about. So `tour_score.py` **clamps its verdict** once
`CORE_ZONE_CLAMP_SHARE` (15 %) of a route lies in the core zone: the powder
share is still reported honestly, but the verdict becomes "Kernzone betroffen
– Bulletin zuerst" and never praises the snow. That property has its own test.

Scoring follows Skitourenguru's geometry (SLABS: non-linear in slope, linear
in danger level, elevation and aspect; 57,800 km of tracks, 1,250 accidents)
but keeps our own objective — snow quality, not risk. Results are a
**distribution**, not a mean: "40 % cold powder, 35 % wind-pressed, 25 % sun
crust" is the useful answer, because a mean hides the one good couloir.
Weighting is by segment *length* and only over 22–50° terrain, so a flat
approach track cannot dominate.

Routes are *cartographic* lines from the 1:50,000 snow-sport maps, not GPS
tracks — fine for a statistic along a route, wrong as navigation, and the UI
must not imply otherwise.

---

| # | Change | Effort | Risk | Do it |
|---|---|---|---|---|
| 1 | Ablation: melt + settling from existing radiation | days | medium — changes model output | **done** |
| 2 | **Model grid 3 km → 250 m** | days | medium — build time | **next; #1 is inert without it** |
| 3 | Split static terrain from the time cube | days | low | now — pure win, smaller downloads |
| 4 | `_RAD_RES` → 250 m | hours | low | with #2 |
| 5 | Weather grid → 1 km (ICON-CH1) | days | medium — API volume | after #2 |
| 6 | Pro tiling + gating | ~2 weeks | high — new delivery path | season 2 |
| 7 | Bulletin layer (CC BY 4.0) | days | low — degrades to hidden | **done** |
| 8 | Ski-route + wildlife overlays (OGD) | hours | low | **done** |
| 9 | Tour powder score, core-zone clamped | days | low — pure functions, tested | **done (engine)** |

**#2 moved up.** It was "refinement" until the measurement above showed the
aspect signal is zero at 3 km. Ablation is implemented and correct but has
almost nothing to act on until the grid can represent a steep slope, so #2 is
now what converts #1 from plumbing into a visible improvement. Budget: 250 m
measured at roughly 10 minutes of build time, inside the 60 minute deploy
timeout.

Item 6 is only worth building once there is something to sell.

**Against the December plan:** items 1–3 are contained and testable and can
land before submission; 4–6 cannot, and the launch plan's cut stands for them.
