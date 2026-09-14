# Launch plan — App Store, 1 December 2026

Written 14 September 2026. **11 weeks.** Builds on `launch-architecture.md`
(architecture) and `launch-checklist.md` (the earlier test-group scope). This
one is specifically about shipping publicly on the App Store for the start of
the ski season.

---

## The one date that actually matters

**Submit to App Review by 3 November.**

Not 25 November. App Review is now usually 24–48 h, but a *first* app from a
*new* developer account, in a **safety-adjacent category**, is exactly the
profile that draws questions. Budget for **two or three rejection cycles**, each
costing a round trip. Submitting four weeks early is what makes 1 December safe;
submitting two weeks early is a gamble.

Use **Manual release** in App Store Connect, not automatic. Then an approved
build simply waits until you press the button on 1 December.

---

## What to cut — decide this first

Cutting is the whole game at 11 weeks.

| Cut | Why |
|---|---|
| **The native Swift rewrite** | Not remotely feasible by December, and attempting it sinks the launch. Ship the Capacitor build. The native work is season 2. |
| **SNOWPACK / Alpine3D ski quality** | This is the real differentiator, and precisely why it deserves a proper run rather than a rushed one. Needs the stateful compute host (Teil B). Target next season, or as a mid-season update. |
| **Push notifications** | High retention value, real work (plugin + APNs + server + a "powder near your home area" trigger). Best first update *after* launch, in December. |
| **Messaging** | Fully built and one migration away, but a DM feature adds moderation surface and user-generated-content risk for no launch-day value. Leave hidden. |
| **Raster resolution** | The 3 km grid looks coarse, but improving it touches the compute budget and the deploy pipeline. Don't destabilise the data path weeks before launch. |

Everything cut here is an *update*, not a loss. Shipping in December and
improving through the season beats missing the season entirely.

---

## P0 — launch blockers

Nothing ships without all of these.

### 1. Run `supabase/migrations/20260901000000_privacy_moderation_ratings.sql` — today
Every user's email is currently readable by anyone with the anon key. It is a
DSG/GDPR exposure, it will be a lie in your App Privacy labels, and the fix is
already written and verified idempotent. Two minutes in the SQL editor.

### 2. Apple Developer Program enrolment — start today
Longest lead time on the whole list. Individual enrolment is usually 24–48 h;
enrolling as a **company** needs a D-U-N-S number and can take **two weeks or
more**. If you want the app published under a business name, start now or accept
publishing as an individual.

### 3. Live data must be the default
**This is a real launch blocker that is easy to miss.** The boot loader
currently sets `isDemo = true`, so the app opens on the **1 April 2026** demo
dataset; live weather is opt-in via Settings or `?live=1`. Shipping a snow app
on 1 December that opens showing April snow is indefensible — and a reviewer
would see it too. Flip the default, keep demo mode as an explicit Settings
toggle.

### 4. Deploy the account-deletion Edge Function
`supabase functions deploy delete-account`. Guideline 5.1.1(v) requires real
deletion; without it the login row survives and the app is rejectable. The
client already falls back gracefully, so this is purely a deploy step.

### 5. Privacy policy + support URL, hosted
Both are required fields in App Store Connect. Must cover: email, precise
location, photos, user content, EU/Swiss hosting (Supabase eu-central-1), and
the deletion/export rights the app already implements.

### 6. App Privacy labels
Mirror `apple-app/ios-config/PrivacyInfo.xcprivacy`. Declare: email, precise
location, photos, user content. **Nothing for tracking.** They must match what
the app actually does, which is why §1 comes first.

### 7. Demo account for App Review
A working email + password reviewers can log in with, plus a note explaining the
draw tool — a reviewer who cannot work out how to post a report may reject for
incompleteness (2.1).

### 8. Store metadata
Screenshots at 6.7" and 6.5" (required sizes), description, keywords, category
(**Weather**, or Sports), age rating, support URL. Screenshots are worth real
effort — they are the entire conversion funnel.

### 9. Real-device testing
At least two physical iPhones and two iOS versions, including the oldest you
support (**iOS 16.2**, the floor set by `color-mix`). Simulators will not
surface the things that matter: GPS drift, camera, haptics, thermals, mountain
connectivity.

---

## P1 — strongly worth doing

### 10. Mitigate Guideline 4.2 ("minimum functionality")
A wrapped website is the classic 4.2 rejection. Make the native integrations
*visible and working*: haptics, native share, location, offline launch with
bundled data. Mention them in the review notes. This is the single most likely
technical rejection reason.

### 11. Sentry
There is already a stub in the HTML awaiting a DSN. Without it you are blind to
crashes on other people's phones and debugging by screenshot — which is exactly
how this project has been debugged so far, and it does not scale past ten users.

### 12. Data-freshness monitoring
The deploy runs 4×/day. If it silently breaks, the app serves stale snow and
nobody notices — the pipeline has *already* had a cache bug of exactly this
shape. Alert on `data/latest.json` being older than ~12 h.

### 13. First-run onboarding
Thirteen layers, a draw tool and a terrain-similarity model is a lot to meet
cold. A three-screen intro (what the layers mean, how to report, the
disclaimer) is probably the highest-leverage retention work available before
launch.

### 14. Capacity and cost
Supabase free tier: 500 MB database, 1 GB storage, 5 GB egress. Photos are the
risk even with the 1600 px downscale already in place. Know your upgrade
trigger before launch rather than during it.

### 15. Re-read the liability gate
It exists and it is good. Read it once more as a lawyer would, not as a
developer.

---

## Positioning — worth deciding deliberately

Frame the app as **snow conditions and powder finding**, not avalanche safety.

This is not cosmetic. It:

- reduces App Review scrutiny (safety-of-life apps get held to a higher bar)
- reduces your personal liability
- is more honest — the model is explicitly an estimation tool, the resolution is
  3 km, and the docs already say it replaces neither IMIS nor SNOWPACK nor a
  bulletin

Keep the disclaimer and the SLF link prominent. Avoid any wording that implies
avalanche-risk assessment.

---

## Week by week

| Weeks | Focus |
|---|---|
| **1** (15–21 Sep) | §1 migration, §2 enrolment, §4 deletion function, build on a real phone |
| **2–3** (22 Sep – 5 Oct) | §3 live-data default, §11 Sentry, §13 onboarding, §5 privacy policy, fix what device testing finds |
| **4–5** (6–19 Oct) | TestFlight internal → 5–10 real ski touring friends. Real feedback beats more features. |
| **6–7** (20 Oct – 2 Nov) | Act on tester feedback. §8 screenshots and metadata. §6 privacy labels. §12 monitoring. §7 demo account. |
| **8** (3 Nov) | **SUBMIT TO APP REVIEW** |
| **9–11** (4–30 Nov) | Rejection cycles and fixes. Approved build held on Manual release. |
| **1 Dec** | Press the button. |

If week 8 slips past ~10 November, seriously consider launching in January
instead of shipping something rushed into the busiest part of the season.

---

## Biggest risks, honestly

1. **App Review rejection** on a safety-adjacent webview wrapper. Mitigated by
   submitting early, §10, and the positioning section.
2. **Enrolment delay** if going the company route. Mitigated by starting today.
3. **Scope creep.** Every item in the Cut list will feel tempting in October.
   The launch date is the constraint; the features are not.
4. **Launching without observability.** If §11 and §12 slip, you will not know
   the app is broken for users until someone tells you.
