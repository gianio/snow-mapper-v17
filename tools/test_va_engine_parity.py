#!/usr/bin/env python3
"""The app draws Variant A itself from the per-frame metric packs; the
pipeline draws the 250 m PNGs from the same runs. Both must mean the same
thing. This builds a synthetic matrix, exports a real pack, classifies sample
cells in Python (build_weights + grid_frames on the pack's quantised values)
and has the app's own engine (vaHiEngine, pulled out of app.js) classify the
same cells. They must agree.

    python tools/test_va_engine_parity.py [dist/app.js]
"""
from __future__ import annotations
import json, subprocess, sys, tempfile
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from variant_a import matrix, export, classify
from variant_a.gridding import METS
from variant_a.subregions import NationalGrid


def synth_grid(nr=80, nc=80, seed=5):
    rng = np.random.default_rng(seed)
    z = rng.normal(0, 1, (nr, nc))
    for _ in range(6):
        z = (z + np.roll(z, 1, 0) + np.roll(z, -1, 0) + np.roll(z, 1, 1) + np.roll(z, -1, 1)) / 5
    dem = 1200 + 2200 * (z - z.min()) / (z.max() - z.min())
    gy, gx = np.gradient(dem, 250.0)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    aspect = (np.degrees(np.arctan2(-gx, gy)) + 360) % 360
    return NationalGrid(elevation=dem, slope=slope, aspect=aspect,
                        tile=np.full((nr, nc), 2, np.int32), nr=nr, nc=nc,
                        xll=600000.0, yll=150000.0, cs=250.0, tile_names={2: "t"})


def fake_results(runs, frames, seed=9):
    """Metric blocks spanning every class boundary, with some failed runs."""
    rng = np.random.default_rng(seed)
    res = {}
    for i, r in enumerate(runs):
        if i % 97 == 13:
            continue                                   # a failed run
        m = np.zeros((frames, len(METS)), np.float32)
        for f in range(frames):
            v = dict(total_hs_cm=rng.choice([0, 10, 40, 150, 300]) + rng.uniform(0, 30),
                     powder_depth_cm=rng.choice([0, 0, 3, 12, 25, 45]) * rng.uniform(0.5, 1.2),
                     crust_thick_cm=rng.choice([0, 0, 0.22, 0.8, 3.0]),
                     powder_dd=rng.uniform(0, 1), powder_lw=rng.choice([0, 0, 2.0]),
                     surface_density=rng.choice([80, 180, 300, 420, 760]),
                     surface_hardness=rng.choice([0, 2, 4.5]),
                     surface_lw=rng.choice([0, 0, 0, 2.5]),
                     weak_below_cm=rng.choice([0, 0, 5]), sh_surface=rng.choice([0, 0, 0, 1]),
                     weak_layer_depth_cm=rng.choice([0, 0, 20]),
                     drift_load=rng.choice([0, 0, 0.3, 0.7]), wind_scour=rng.choice([0, 0, 0.3, 0.7]))
            m[f] = [v.get(k, 0.0) for k in METS]
        res[r["id"]] = (m, None, None, None)
    return res


def main():
    app = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "dist" / "app.js")
    g = synth_grid()
    wps = matrix.weather_points(g, spacing_km=5.0, min_top_m=1800.0)
    runs = matrix.matrix_runs(wps)
    from datetime import datetime
    frames = [datetime(2026, 3, 30, 12), datetime(2026, 3, 30, 15)]
    res = fake_results(runs, len(frames))
    rng = np.random.default_rng(3)
    shade = np.clip(rng.uniform(-0.3, 0.8, (g.nr, g.nc)), 0, 1).astype(np.float32)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        pk = export.export_pack(td, runs, res, frames, wps, g, shade, METS)
        # what the app will actually see: the quantised pack and shade bytes
        img = np.asarray(Image.open(td / "pack" / f"f_{frames[0]:%Y-%m-%dT%H%M}.png"))
        per, n, nm = pk["px_per_run"], len(runs), len(pk["mets"])
        q = img.reshape(-1, per * 3)[:n]
        vals = q[:, :nm] / np.array(pk["mul"], np.float32)
        ok = q[:, nm]
        sb = np.asarray(Image.open(td / "terrain" / "shade.png"))
        shade_q = sb.astype(np.float32) / 254.0
        # Python reference on the same quantised numbers
        stack = np.zeros((n, len(frames), len(METS)), np.float32)
        for k, mname in enumerate(pk["mets"]):
            stack[:, 0, METS.index(mname)] = vals[:, k]
        res_q = {r["id"]: (stack[i], None, None, None) for i, r in enumerate(runs) if ok[i]}
        W, ci = matrix.build_weights(g, wps, runs, shade=shade_q)
        _, fr = next(matrix.grid_frames(g, runs, res_q, frames[:1], W, ci))
        pick = rng.choice(len(ci), size=min(1500, len(ci)), replace=False)
        samples = []
        for c in ci[pick]:
            r_, c_ = divmod(int(c), g.nc)
            samples.append([g.xll + (c_ + 0.5) * g.cs, g.yll + (g.nr - 1 - r_ + 0.5) * g.cs,
                            float(g.elevation[r_, c_]), float(g.slope[r_, c_]),
                            float(g.aspect[r_, c_]), float(shade_q[r_, c_]),
                            int(fr["ski18"][r_, c_]), int(fr["simple"][r_, c_]),
                            int(fr["ski6"][r_, c_]), int(fr["wind"][r_, c_])])
        fx = {"pack": pk, "vals": vals.round(4).ravel().tolist(), "ok": ok.tolist(),
              "shade": {"w": int(sb.shape[1]), "h": int(sb.shape[0]), "data": sb.ravel().tolist()},
              "samples": samples}
        fp = td / "fixture.json"
        fp.write_text(json.dumps(fx))
        p = subprocess.run(["node", str(ROOT / "tools" / "test_va_engine.js"), str(fp), app],
                           capture_output=True, text=True)
        print(p.stdout.strip())
        if p.returncode:
            print(p.stderr[-2000:])
        sys.exit(p.returncode)


if __name__ == "__main__":
    main()
