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

| # | Change | Effort | Risk | Do it |
|---|---|---|---|---|
| 1 | Ablation: melt + settling from existing radiation | days | medium — changes model output | **done** |
| 2 | **Model grid 3 km → 250 m** | days | medium — build time | **next; #1 is inert without it** |
| 3 | Split static terrain from the time cube | days | low | now — pure win, smaller downloads |
| 4 | `_RAD_RES` → 250 m | hours | low | with #2 |
| 5 | Weather grid → 1 km (ICON-CH1) | days | medium — API volume | after #2 |
| 6 | Pro tiling + gating | ~2 weeks | high — new delivery path | season 2 |

**#2 moved up.** It was "refinement" until the measurement above showed the
aspect signal is zero at 3 km. Ablation is implemented and correct but has
almost nothing to act on until the grid can represent a steep slope, so #2 is
now what converts #1 from plumbing into a visible improvement. Budget: 250 m
measured at roughly 10 minutes of build time, inside the 60 minute deploy
timeout.

Item 6 is only worth building once there is something to sell.

**Against the December plan:** items 1–3 are contained and testable and can
land before submission; 4–6 cannot, and the launch plan's cut stands for them.
