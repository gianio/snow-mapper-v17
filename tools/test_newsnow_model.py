"""Checks of the new-snow model's physics (model/factors.py, model/snow_model.py).

    python tools/test_newsnow_model.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config.settings import load_model_params  # noqa: E402
from model import factors  # noqa: E402
from model.snow_model import WeatherGrid, compute_new_snow  # noqa: E402
from model.terrain_features import compute_terrain_features  # noqa: E402

P = load_model_params()
fails = 0


def check(name, ok, info=""):
    global fails
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {info}")
    fails += 0 if ok else 1


tw = lambda t, rh, z=0.0: float(factors.wet_bulb_c(np.array(float(t)), np.array(float(rh)), z))
check("wet bulb 20 degC / 50 % at sea level ~13.7", abs(tw(20, 50) - 13.7) < 0.3, f"{tw(20, 50):.2f}")
check("saturated air: wet bulb = air temperature", abs(tw(2, 100, 2000) - 2.0) < 0.01, f"{tw(2, 100, 2000):.2f}")
check("wet bulb never above the air temperature", tw(-5, 80, 2500) <= -5.0, f"{tw(-5, 80, 2500):.2f}")

T = np.array([2.0])
f_sat = float(factors.temperature_factor(T, P, np.array([100.0]), 2000.0)[0])
f_dry = float(factors.temperature_factor(T, P, np.array([65.0]), 2000.0)[0])
f_old = float(factors.temperature_factor(T, P)[0])
check("dry air snows at +2 degC, saturated air hardly", f_dry > 0.8 and f_sat < 0.25, f"dry {f_dry:.2f}, saturated {f_sat:.2f}")
check("without humidity the split stays on the air temperature", abs(f_old - 0.2) < 0.02, f"{f_old:.2f}")

slr = lambda t, u: float(factors.snow_to_liquid_ratio(np.array([float(t)]), P, np.array([float(u)]))[0])
check("cold calm snow is light", slr(-10, 0) > 12, f"{slr(-10, 0):.1f}:1")
check("wind packs it", slr(-5, 8) < 0.8 * slr(-5, 0), f"{slr(-5, 8):.1f}:1 vs calm {slr(-5, 0):.1f}:1")
check("warm wet snow is heavy", slr(1, 5) < 9.0 and slr(1, 5) < 0.7 * slr(-5, 5), f"{slr(1, 5):.1f}:1")
check("bounded 4.5:1 .. 20:1", 4.5 <= slr(5, 40) and slr(-40, 0) <= 20.0, f"{slr(5, 40):.1f} / {slr(-40, 0):.1f}")
# calibrated against the station boards (27.11.): a day of ~10 mm at -3 degC
# and a light wind is ~10-14 cm on the board, not the 5 cm of the old density
check("10 mm at -3 degC, 2 m/s: 10..14 cm", 10.0 <= slr(-3, 2) <= 14.0, f"{slr(-3, 2):.1f} cm")

rng = np.random.default_rng(1)
z = gaussian_filter(rng.normal(0, 1, (160, 160)), 6) * 4000 + 2000
ter = compute_terrain_features(z, 250.0)
sh = z.shape
wg = WeatherGrid(np.full(sh, 1.0), np.zeros(sh), np.full(sh, -6.0), np.full(sh, 2000.0),
                 np.full(sh, 7.0), np.full(sh, 270.0), np.full(sh, 95.0))
out = compute_new_snow(ter, wg, P)
tf = out["terrain_factor"]
check("wind and Luv/Lee move snow, they do not add it", abs(float(tf.mean()) - 1.0) < 0.03, f"mean {tf.mean():.3f}")
check("but still redistribute it", float(tf.std()) > 0.05, f"spread {tf.std():.3f}")
check("no NaN in the new snow", not np.isnan(out["new_snow_cm"]).any())

print("\nNEWSNOW MODEL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
