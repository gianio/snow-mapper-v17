#!/usr/bin/env python3
"""Ablation: prueft die Eigenschaften, auf die sich der Client verlaesst.

Der Client rechnet max(0, cum + SNOW - ABL). Das ist nur dann dasselbe wie
die Pipeline, wenn ABL nie mehr ist als die vorhandene Hoehe. Ausserdem
prueft dieser Test die Aussage, um derer willen das Modul existiert: dass
Sued- und Nordhang sich unterschiedlich verhalten.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import load_model_params          # noqa: E402
from model import ablation as A                        # noqa: E402

P = load_model_params()
fails = []


def check(name, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


print("solar geometry")
# 21 June, midday, 47 deg N -> sun high and roughly south.
sin_el, az = A.solar_geometry(172, 12, np.array([47.0]))
el = np.degrees(np.arcsin(sin_el))[0]
check("midsummer noon elevation ~66 deg", 60 < el < 70, f"got {el:.1f}")
check("noon azimuth ~south (180 deg)", abs(np.degrees(az)[0] - 180) < 15,
      f"got {np.degrees(az)[0]:.0f}")
# 21 December, midday -> much lower.
sin_el_w, _ = A.solar_geometry(355, 12, np.array([47.0]))
el_w = np.degrees(np.arcsin(sin_el_w))[0]
check("midwinter noon lower than midsummer", el_w < el - 30, f"{el_w:.1f} vs {el:.1f}")
# Night.
sin_el_n, _ = A.solar_geometry(172, 1, np.array([47.0]))
check("night gives zero elevation", sin_el_n[0] == 0.0)

print("\nslope irradiance -- the aspect signal")
slope = np.radians(np.array([35.0, 35.0, 0.0]))
aspect = np.radians(np.array([180.0, 0.0, 0.0]))       # S, N, flat
sin_el, az = A.solar_geometry(80, 12, np.array([47.0, 47.0, 47.0]))   # 21 March
irr = A.slope_irradiance(sin_el, az, slope, aspect, P)
check("south-facing beats north-facing at noon", irr[0] > irr[1] * 1.5,
      f"S={irr[0]:.0f} N={irr[1]:.0f} W/m2")
check("south-facing beats flat in spring", irr[0] > irr[2], f"S={irr[0]:.0f} flat={irr[2]:.0f}")
check("all irradiance non-negative", bool(np.all(irr >= 0)))
irr_n = A.slope_irradiance(*A.solar_geometry(80, 2, np.array([47.0] * 3)), slope, aspect, P)
check("no irradiance at night", bool(np.all(irr_n == 0)), f"max {irr_n.max():.2f}")

print("\nalbedo")
a0, a5, a30 = (A.snow_albedo(np.array([h]), P)[0] for h in (0.0, 120.0, 720.0))
check("fresh snow albedo = albedo_fresh", abs(a0 - P["ablation"]["albedo_fresh"]) < 1e-9)
check("albedo decays monotonically", a0 > a5 > a30, f"{a0:.2f} > {a5:.2f} > {a30:.2f}")
check("albedo stays above albedo_old", a30 > P["ablation"]["albedo_old"] - 1e-9)

print("\nmelt")
z = np.zeros(1)
check("no melt when cold and dark",
      A.melt_depth_cm(np.array([-8.0]), z, z, np.array([0.85]), P)[0] == 0.0)
warm = A.melt_depth_cm(np.array([6.0]), z, z, np.array([0.85]), P)[0]
check("warm air alone melts", warm > 0, f"{warm:.3f} cm/h")
sunny = A.melt_depth_cm(np.array([-2.0]), np.array([700.0]), np.array([1.0]),
                        np.array([0.55]), P)[0]
check("sun melts below freezing (radiation-driven)", sunny > 0, f"{sunny:.3f} cm/h")
dirty = A.melt_depth_cm(np.array([0.0]), np.array([700.0]), np.array([1.0]),
                        np.array([0.55]), P)[0]
clean = A.melt_depth_cm(np.array([0.0]), np.array([700.0]), np.array([1.0]),
                        np.array([0.85]), P)[0]
check("lower albedo melts faster", dirty > clean, f"{dirty:.3f} vs {clean:.3f}")

print("\nsettling rate")
r0, r3, r30 = (A.settling_rate(np.array([h]), P)[0] for h in (0.0, 72.0, 720.0))
check("fresh snow settles fastest", r0 > r3 > r30, f"{r0:.5f} > {r3:.5f} > {r30:.5f}")
check("old pack nearly stops settling", r30 < 2 * P["ablation"]["settling_frac_min_per_h"],
      f"{r30:.5f}/h")

print("\nstep_snowpack -- the invariant the client depends on")
# Bare ground, no new snow, blazing sun: ablation must not go negative.
d, hs, ab = A.step_snowpack(np.zeros(1), np.zeros(1), np.full(1, 500.0),
                            np.array([15.0]), np.array([900.0]), np.ones(1), P)
check("no ablation from an empty snowpack", ab[0] == 0.0, f"got {ab[0]:.4f}")
check("depth stays at zero", d[0] == 0.0)

# 1 cm of snow, extreme melt forcing: ablation capped at what exists.
d, hs, ab = A.step_snowpack(np.array([1.0]), np.zeros(1), np.full(1, 500.0),
                            np.array([25.0]), np.array([1000.0]), np.ones(1), P)
check("ablation never exceeds available depth", ab[0] <= 1.0 + 1e-12, f"got {ab[0]:.4f}")
check("depth never goes negative", d[0] >= 0.0, f"got {d[0]:.4f}")

# Fresh snow resets the surface age.
d, hs, ab = A.step_snowpack(np.array([50.0]), np.array([5.0]), np.full(1, 400.0),
                            np.array([-5.0]), np.zeros(1), np.zeros(1), P)
check("fresh snow resets surface age", hs[0] == 0.0, f"got {hs[0]}")
check("cold dark hour still settles a little", 0 < ab[0] < 1.0, f"{ab[0]:.4f} cm")

# Settling acts on the old pack, not on this hour's new snow.
_, _, ab_new = A.step_snowpack(np.zeros(1), np.array([100.0]), np.zeros(1),
                               np.array([-10.0]), np.zeros(1), np.zeros(1), P)
check("this hour's new snow is not settled", ab_new[0] == 0.0, f"got {ab_new[0]:.4f}")

print("\n11-day integration -- north vs south, April")
# Tracks melt and settling SEPARATELY. They are different physics: melt
# removes mass, settling only reduces depth. A 150 cm fresh dump genuinely
# settles towards ~90 cm in a few days, so lumping the two together and
# calling the sum a "melt rate" would be meaningless.
def run(aspect_deg, temps, sun=1.0):
    depth = np.zeros(1); since = np.full(1, 200.0)
    melt_tot = 0.0; abl_tot = 0.0
    sl = np.radians(np.array([35.0])); asp = np.radians(np.array([float(aspect_deg)]))
    for day in range(11):
        for hour in range(24):
            se, az = A.solar_geometry(105, hour, np.array([46.8]))
            irr = A.slope_irradiance(se, az, sl, asp, P)
            new = np.array([150.0]) if (day == 0 and hour == 3) else np.zeros(1)
            age = 0.0 if new[0] >= P["ablation"]["fresh_snow_threshold_cm"] else since[0] + 1
            melt_tot += float(A.melt_depth_cm(np.array([temps[hour]]), irr,
                                              np.full(1, sun),
                                              A.snow_albedo(np.array([age]), P), P)[0])
            depth, since, ab = A.step_snowpack(depth, new, since,
                                               np.array([temps[hour]]), irr,
                                               np.full(1, sun), P)
            abl_tot += float(ab[0])
    return float(depth[0]), abl_tot, melt_tot

diurnal = [(-4 + 8 * max(0.0, np.sin(np.pi * (h - 6) / 12))) for h in range(24)]
d_s, a_s, m_s = run(180, diurnal)
d_n, a_n, m_n = run(0, diurnal)
print(f"    south: {d_s:5.1f} cm left | melt {m_s:5.1f} cm ({m_s/11:.1f}/day) | total loss {a_s:.1f}")
print(f"    north: {d_n:5.1f} cm left | melt {m_n:5.1f} cm ({m_n/11:.1f}/day) | total loss {a_n:.1f}")

check("south face melts more than north", m_s > m_n * 1.5, f"{m_s:.1f} vs {m_n:.1f}")
check("both faces still hold snow (test not saturated)", d_s > 0 and d_n > 0,
      f"S={d_s:.1f} N={d_n:.1f}")
check("north keeps more depth than south", d_n > d_s * 1.2, f"N={d_n:.1f} S={d_s:.1f}")
check("nothing accumulates from nowhere", d_s <= 150.0 and d_n <= 150.0)

# Cloudless mid-April on a 35 deg south face is near the worst case the Alps
# offer. Pellicciotti's coefficients are literature values, not tuned here, so
# assert the plausible envelope rather than a point.
check("south melt within plausible envelope (2-8 cm/day)", 2.0 < m_s / 11 < 8.0,
      f"{m_s/11:.1f} cm/day")
check("north melt clearly lower (<4 cm/day)", m_n / 11 < 4.0, f"{m_n/11:.1f} cm/day")

print("\ntypical April weather -- half sunshine, not 11 cloudless days")
d_c, a_c, m_c = run(180, diurnal, sun=0.5)
print(f"    south, 50 % sun: {d_c:.1f} cm left | melt {m_c:.1f} cm ({m_c/11:.1f}/day)")
check("cloud cover reduces melt", m_c < m_s, f"{m_c:.1f} vs {m_s:.1f}")
check("typical-weather melt is modest (<5 cm/day)", m_c / 11 < 5.0, f"{m_c/11:.1f} cm/day")

print("\nsettling dominates early, melt dominates late")
# First 24 h after a big dump: almost all depth change is compaction.
early = A.settling_rate(np.array([0.0]), P)[0] * 150.0
late = A.settling_rate(np.array([600.0]), P)[0] * 150.0
check("fresh dump settles fast", early > 0.5, f"{early:.2f} cm/h")
check("settled pack barely compacts", late < 0.1, f"{late:.3f} cm/h")

print("\n" + ("ABLATION OK" if not fails else f"FAILED: {fails}"))
sys.exit(1 if fails else 0)
