# Alpin Grid — Snow Mapper design system

The visual and interaction language for Snow Mapper. It replaces the previous
"alpine glass" style (translucent floating cards, blur, soft shadows, decorative
purple) with a flat, grid-driven, instrument-like system.

The reference implementation is the `<style>` block and markup inside
`pipeline/interactive_export.py`. Tokens are CSS custom properties on `:root`;
**every screen must be built from them.** If you find yourself typing a hex
value outside `:root`, that is the bug.

---

## 1. Why it looks like this

Snow Mapper is read outdoors, on a phone, in glare, often with gloves on, by
someone deciding where to ski. That pushes every decision:

- **Glare** kills low-contrast translucency. Surfaces are opaque.
- **Gloves** need 44 px targets and no precision gestures.
- **Deciding** means the map is the product. Chrome must never compete with it.
- **The map is already colourful.** So the interface must not be.

The result reads like a Swiss instrument panel: neutral, structured by thin
rules and a strict grid, with colour spent only where it carries information.

---

## 2. The six principles

### P1 — One screen, one surface at a time
The map is the application, and at rest it owns the screen: the only permanent
chrome is a four-target dock. Everything else opens in **the same sheet**, one
level at a time. Nothing opens on top of something else that is already on top —
a deeper level *replaces* the content of the sheet, it does not stack a second
surface over it.

Practically: no modal over modal, no flyout that hides the thing it belongs to.
There is exactly one sheet, and it is either showing a level or it is closed.

### P2 — Colour is data. Chrome is neutral.
See §4. This is the load-bearing rule of the whole system.

### P3 — Structure by rules, not by shadows
Hierarchy comes from **hairlines, spacing and type weight**. Not from blur, not
from layered drop shadows, not from tinted glass. Exactly one elevation token
exists, and it is only for surfaces that are temporary.

### P4 — Simple first, deep on demand — and always a way back
Depth costs taps and taps cost gloves, so the *frequency* of a thing decides how
deep it sits. The dock shows live state at zero taps. Any map layer is **two**
(dock → row). Variants and settings (Wind → Max, Reported Powder → Vertrauen)
cost a third, and only if you want them. Nothing routine costs four.

Every level deeper than the first shows a back chevron, and **five gestures pop
exactly one level**: the chevron, a downward drag on the grab handle, the scrim,
Escape, and the phone's back gesture. You can always retreat one step; you are
never trapped and never thrown all the way out by accident.

### P5 — Type carries hierarchy
Size and weight separate levels. Colour does not. A "more important" label is
bigger or heavier, never blue.

### P6 — Show the state, don't imply it
The selected thing is **filled**, not tinted. At a glance, in sun, there is
exactly one filled element per group of choices.

---

## 3. Layout

### The one-screen model

Level 0 — at rest, the map is ~90 % of the screen:

```
┌─────────────────────────────────────┐
│  demo                       ▏rail▕  │   right rail: profile · feed
│                                     │
│                                     │
│              M A P                  │   the map is never boxed in
│                                     │
│                                     │
│ ┌─────────────────────────────────┐ │
│ │ ○ │ A·METEO   │ ZEIT  │   ＋    │ │   the dock: four targets, and
│ │   │ Powder    │ 48 h  │         │ │   two of them are live readouts
│ └─────────────────────────────────┘ │
└─────────────────────────────────────┘
```

Level 1+ — one sheet rises; the map stays visible behind the scrim:

```
┌─────────────────────────────────────┐
│              M A P                  │
│ ┌────────────────────────────────┐  │
│ │            ▬▬▬▬                │  │   grab handle: drag down to pop
│ │  ‹   Reported Powder        ✕  │  │   back chevron from level 2 on
│ │  ──────────────────────────────│  │
│ │  WELCHE REPORTS                │  │   sections by hairline + label
│ │  ● Zeichnen                    │  │
│ │  ○ Zeichnen + Quick        ›   │  │
│ │  VERTRAUEN                     │  │
│ │  Nur ab                   60%  │  │
│ │  ────────●───────────────      │  │
│ └────────────────────────────────┘  │
└─────────────────────────────────────┘
```

The **dock** is the whole permanent interface: search, the active layer, the
time window, and report. Two of its four targets are *readouts as well as
buttons* — they show what is on the map right now (P6) and open the level that
changes it.

Everything else is a **view** pushed onto a navigation stack and rendered into
that one sheet. A view is a list of full-width rows; a row with a chevron goes
one level deeper, a row with a tick is a choice. Deep functionality is not
hidden, it is *ordered*: `dock → Karte → Reported Powder → Vertrauen`.

Two long-lived controls — the timeline and the search field — are **borrowed**
into the sheet rather than rebuilt, so they keep their wiring, and are handed
back to their own parent when the level is left. Never look one up by `id` after
the sheet body has been cleared; hold the reference (see `svAdopt`/`svReturnAll`).

### Grid and spacing

An 8 px baseline. Use the scale; do not invent values.

| token | value | use |
|---|---|---|
| `--sp1` | 4px | icon-to-label |
| `--sp2` | 8px | inside a control |
| `--sp3` | 12px | between controls |
| `--sp4` | 16px | surface padding |
| `--sp5` | 24px | between sections |
| `--sp6` | 32px | page rhythm |

Gutters are `--sp4`. Sections inside a surface are separated by `--sp4` of space
**or** a hairline, never both.

### Radius

Two steps and a pill. More than that reads as noise.

| token | value | use |
|---|---|---|
| `--r-1` | 8px | controls: buttons, chips, inputs, swatches |
| `--r-2` | 14px | surfaces: console, sheets, cards |
| `--r-full` | 999px | only genuinely pill-shaped things (badges, avatars) |

### Elevation

| token | use |
|---|---|
| `--lift` | the *only* shadow. Temporary surfaces: sheets, toasts, popovers. |

Docked surfaces get a **hairline**, not a shadow. Nothing has more than one
shadow. Nothing has an inset highlight.

### Touch targets

| element | min height |
|---|---|
| primary control (layer chip, button) | **44px** |
| secondary control (variant tab, icon button) | 36px |
| never below | 32px |

Media queries may **not** shrink targets on small screens. Phones are the
primary device; if a control does not fit, wrap the row or shorten the label.

---

## 4. The colour law

> **Colour is data. Chrome is neutral. One accent at a time.**

Three families, and they never mix.

### 4.1 Ink — all chrome, always

Every bar, panel, button, border and label is drawn from this ramp and nothing
else.

| token | value | use |
|---|---|---|
| `--ink-900` | `#0E1116` | primary text, filled buttons |
| `--ink-700` | `#333A45` | secondary text |
| `--ink-500` | `#6B7480` | muted text, inactive icons |
| `--ink-300` | `#AAB2BD` | disabled, placeholder |
| `--ink-150` | `#D6DBE2` | strong borders |
| `--ink-100` | `#E7EAEF` | hairlines |
| `--ink-050` | `#F4F6F9` | recessed fills, tracks |
| `--paper`   | `#FFFFFF` | surfaces |

A chrome element that needs emphasis gets **darker ink or heavier type** — not
a colour.

### 4.2 Accent — exactly one, contextual

One accent is live at a time. It is set by the region of the app you are in, and
it means **"this is the thing you selected"**.

| context | token value | meaning |
|---|---|---|
| Meteo model (A) selected | `--accent-meteo` `#0B6BCB` | forecast-model layers |
| Report model (B) selected | `--accent-report` `#6D3BD1` | community-report layers |

The accent is **global, not per screen**: whichever map layer is selected sets
`--accent` on `:root`, and every surface in the app — feed, sheets, profile,
draw mode — picks it up from there. One accent is live in the entire product at
any moment, which is what makes the sub-applications feel like one app.

Rules:

1. The accent is exposed as `--accent` and `--accent-soft` (its 12 % tint).
   Components reference those, never a literal hue.
2. **At most one accented element per group of choices.** If two things are
   accented, one of them is wrong.
3. The accent is never used for decoration — no accented headings, dividers,
   icons-for-fun, or gradients.
4. Chrome that is not "the selected thing" stays ink, even on an accented
   screen. Avatars, category badges and icons are ink; the danger categories
   are the one exception, because there the colour is the meaning.

### 4.3 Semantic — fixed meanings, never decorative

| token | value | meaning |
|---|---|---|
| `--danger` | `#C62828` | destructive action, avalanche/danger reports |
| `--warn` | `#E08A00` | caution, degraded confidence |
| `--ok` | `#1B7F4F` | confirmed, success |

If it is not that meaning, it does not get that colour.

### 4.4 Data palettes — the map only

These live on the map and in legends. They must **never** appear in chrome.

| palette | use |
|---|---|
| SLF new-snow scale | snow depth, everywhere depth is shown |
| Confidence ramp (`PROG_CONF_STOPS`) | 0–100 % model confidence |
| Snow-type textures (`SNOW_PATTERNS`) | every snow type **except** powder |

And the reciprocal rule: **Powder is the only snow type with a solid colour.**
Every other type is told apart by texture, so the map stays readable for
colour-blind users and in flat light. Depth owns colour; type owns pattern.

### 4.5 Quick test

Screenshot any screen and desaturate it. If you can still tell what is selected,
what is a button and what is a warning — it passes. If the screen collapses into
grey mush, hierarchy was being carried by colour and needs fixing.

---

## 5. Type

One family (system UI stack). Hierarchy by size and weight only.

| role | size / weight | notes |
|---|---|---|
| Display | 22 / 800 | screen titles, one per screen |
| Title | 17 / 800 | sheet headers |
| Body-strong | 15 / 700 | button labels, list titles |
| Body | 14 / 500 | prose |
| Meta | 12.5 / 600 | timestamps, captions |
| Micro-label | 10 / 800, `letter-spacing:.08em`, UPPERCASE, `--ink-500` | section labels in the console, form field labels |

Numbers that a user compares (cm, %, °C, km/h) use
`font-variant-numeric: tabular-nums`.

`letter-spacing: -.01em` on 14 px and above; never on the micro-label.

---

## 6. Components

Recipes, not suggestions.

### Surface
```
background: var(--paper);
border: 1px solid var(--ink-100);
border-radius: var(--r-2);
```
Temporary surfaces add `box-shadow: var(--lift)`. Docked surfaces do not.

### Section label
Micro-label, `--sp2` below it, hairline above it when it follows another
section.

### Chip / selectable (layer chips, filters, segmented options)
```
min-height: 44px; padding: 0 15px; border-radius: var(--r-1);
background: var(--paper); border: 1px solid var(--ink-100); color: var(--ink-700);
```
Selected:
```
background: var(--accent); border-color: var(--accent); color: var(--paper);
```
Filled, not tinted (P6). No shadow on the selected state.

### Button
- **Primary** — `--ink-900` fill, `--paper` text. One per screen.
- **Secondary** — `--paper` fill, `--ink-100` border, `--ink-900` text.
- **Quiet** — no fill, no border, `--ink-700` text.
- **Destructive** — `--danger` text; fills only on the final confirm step.

All: `min-height 44px`, `--r-1`, `:active { transform: scale(.97) }`.

### Input
```
background: var(--ink-050); border: 1px solid var(--ink-100);
border-radius: var(--r-1); min-height: 44px;
```
Focus: `border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft)`.

### Sheet
Docks to the bottom edge, `--r-2` on the top corners only, grab handle
(`36×4`, `--ink-150`), `--sp4` padding, `--lift`. Backdrop is
`rgba(14,17,22,.32)` — a scrim, not a blur. Max height `82vh`, so the map is
always still visible behind it. It animates on `transform` only.

### Sheet row (`.sv-item`)
The one navigation element. Full width, `min-height 60px`, hairline underneath,
title 16/700 with an optional 13 px sub-line in `--ink-500`.
- **Chevron** on the right → goes one level deeper.
- **Tick circle** on the left → it is a choice; filled `--accent` when chosen,
  and the title turns accent too.
A row is never both. `:active` fills `--ink-050`, nothing else.

### Sheet slider (`.sv-slide`)
For continuous settings. Label left, live value right in `--accent` with
tabular numerals, optional explanatory line, then a 34 px-high range input.
The value updates while dragging — the map does too.

### Icon
1.75 px stroke, `currentColor`, 20 px in controls and 18 px inline. Custom SVG
only — **no emoji anywhere in the UI**.

---

## 7. Motion

| token | value | use |
|---|---|---|
| `--dur-1` | 120ms | state changes (selection, hover, press) |
| `--dur-2` | 220ms | surfaces entering and leaving |
| `--ease` | `cubic-bezier(.2,0,0,1)` | everything |

One easing curve. No springs, no bounce — this is an instrument. Everything is
wrapped by `prefers-reduced-motion`.

Two coordinated moves, both on `--dur-2` and both on `transform`/`opacity` so
they stay on the compositor: the dock drops away as the sheet rises, and the
scrim fades with them. Arriving content is staggered — rows fade up 9 px with
30 ms between the first three (`.sv-in`), which reads as the level *unfolding*
rather than flashing in. Going back does not re-stagger a level you have
already seen within the same gesture.

---

## 8. Per-screen application

| screen | how the language applies |
|---|---|
| **Map** | Full bleed and ~90 % of the screen. Abstract at country view, detail fading in with zoom. Chrome docks to edges only. |
| **Dock** | The only permanent chrome. Four targets; two of them read out the live state. Hidden while a sheet or the drawing canvas is open. |
| **Sheet views** (layers, variants, prognosis settings, time, search, map options, legend, report) | One recipe: grab handle, back chevron + title + close, then full-width rows. A chevron means deeper; a tick means chosen. Sections are hairline + accent label. |
| **Feed** | White cards on `--ink-050`, hairline separated, photo full-bleed inside the card. Action row is quiet buttons; only counts are dark. |
| **Sheets** (comments, profile, condition, location) | Identical sheet recipe, inheriting whatever accent is currently live. |
| **Report / draw** | The drawing canvas is the surface; controls dock left (brush) and right (depth) and along the bottom. Pen swatches show the actual texture. |
| **Inspect panel** | Docked card, tabular numbers, charts in ink with the data palettes only for the values themselves. |
| **Auth / intro / disclaimer** | Centred single-column, one primary button, no decoration. |

---

## 9. Accessibility

- Contrast: body text ≥ 4.5:1, large/heavy text ≥ 3:1 against its surface. The
  ink ramp is built so `--ink-500` on `--paper` passes.
- Never signal by colour alone — the snow-type textures exist for exactly this
  reason, and selected states are filled rather than tinted.
- `prefers-reduced-motion`, `prefers-contrast: more` and
  `prefers-reduced-transparency` are all honoured.
- Every icon-only control has an `aria-label`.
- Focus is always visible: `box-shadow: 0 0 0 3px var(--accent-soft)`.

---

## 10. Adding something new

1. Can it be a row in a sheet view that already exists? Put it there. (P1)
   If it needs its own level, add it to `NAV_VIEWS` — never a new modal.
2. Which of the three colour families does it belong to? If chrome — it is ink
   plus, at most, the accent. (P2)
3. How often is it used? That decides its depth — and every level below the
   first must show the back chevron. (P4)
4. Does it need a shadow? Only if it is temporary. (P3)
5. Are the targets 44 px? (§3)
6. Desaturate it. Does it still work? (§4.5)
