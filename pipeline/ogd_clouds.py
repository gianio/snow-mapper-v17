"""MeteoSwiss ICON cloud cover on the app's map grid.

tools/ogd_extract.py writes clouds_ch1.npz (1 km, the first 33 h) and
clouds_ch2.npz (2 km, longer): low, mid and high cloud cover in percent on
every ICON cell over Switzerland, hourly. Each map cell takes its nearest ICON
cell, so the layer shows the clouds at the model's own resolution instead of
a spline between weather points 20 km apart. For an hour both models have,
the finer one wins; an hour neither has keeps the Open-Meteo clouds.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

MODELS = ("ch1", "ch2")
LABEL = {"ch1": "ICON-CH1", "ch2": "ICON-CH2"}


def load(field_dir):
    """The cloud fields found in field_dir, finest model first."""
    out = []
    for model in MODELS:
        f = Path(field_dir) / f"clouds_{model}.npz"
        if not f.exists():
            continue
        z = np.load(f)
        out.append({"model": model, "lat": z["lat"].astype("float64"), "lon": z["lon"].astype("float64"),
                    "lead": z["lead"], "ref": datetime.strptime(str(z["ref"]), "%Y-%m-%dT%H:%M"),
                    "low": z["low"], "mid": z["mid"], "high": z["high"]})
    return out


def total(low, mid, high):
    """Total cover from the three layers, overlapping at random."""
    return 100.0 * (1.0 - (1.0 - low / 100.0) * (1.0 - mid / 100.0) * (1.0 - high / 100.0))


class CloudFrames:
    def __init__(self, fields, lat_grid, lon_grid):
        from scipy.spatial import cKDTree
        self.shape = np.shape(lat_grid)
        q_lat, q_lon = np.ravel(lat_grid), np.ravel(lon_grid)
        self.fields = []
        for f in fields:
            k = np.cos(np.radians(float(np.nanmean(f["lat"]))))      # degrees of longitude are shorter
            tree = cKDTree(np.c_[f["lat"], f["lon"] * k])
            _, idx = tree.query(np.c_[q_lat, q_lon * k])
            times = {(f["ref"] + timedelta(hours=float(h))).strftime("%Y-%m-%dT%H:%M"): i
                     for i, h in enumerate(f["lead"])}
            self.fields.append((f, idx, times))
        self.used = {}

    def at(self, time_str):
        """(low, mid, high) in percent on the grid for this hour (UTC), or None."""
        key = str(time_str)[:16]
        for f, idx, times in self.fields:
            i = times.get(key)
            if i is not None:
                self.used[f["model"]] = self.used.get(f["model"], 0) + 1
                return tuple(np.asarray(f[k][i], "float32")[idx].reshape(self.shape) for k in ("low", "mid", "high"))
        return None

    def source(self):
        """'MeteoSchweiz ICON-CH1/ICON-CH2' for the models that served an hour."""
        m = [LABEL[k] for k in MODELS if self.used.get(k)]
        return ("MeteoSchweiz " + "/".join(m)) if m else None
