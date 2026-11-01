"""MeteoSwiss weather for the web app's own layers (temperature, wind,
precipitation -> new snow, sunshine), instead of Open-Meteo.

The live cycle (variant-a-live.yml) runs tools/ogd_extract.py also for the
site's weather points (tools/site_points.py, ids "s0000" …) and files their
hours in our archive (pipeline/wx_archive.py). The site build then lays, per
point:

  1. the archived MeteoSwiss hours (the past days),
  2. the newest MeteoSwiss forecast on top (ICON-CH1 33 h, then CH2),

over the Open-Meteo series it fetched. Open-Meteo stays underneath only for
hours MeteoSwiss does not have (before the archive starts, beyond CH2's
range) and for the variables MeteoSwiss does not deliver here (wind on
pressure levels).

Temperature: ICON's value is at the model cell's height, the series is at
the weather point's reference height. The shift is measured per point as the
mean difference over the hours both have (as in variant_a/forcing.py).

    note = overlay(site_dir, archive_dir, lats, lons, times,
                   temp, prec, snow, wind_speed, wind_dir, sun)   # arrays changed in place
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

SNOW_CM_PER_MM = 0.7         # Open-Meteo's snowfall convention (7:1 by depth)
_FMT = "%Y-%m-%dT%H:%M"


def _load_points(site_dir: Optional[Path]) -> Dict[str, dict]:
    """{point id: {"lat", "lon", "hourly"}} from the extract's s*.json."""
    out = {}
    if not site_dir or not Path(site_dir).is_dir():
        return out
    meta = {}
    mf = Path(site_dir) / "site_points.json"
    if mf.exists():
        meta = {p["id"]: p for p in json.loads(mf.read_text())}
    for f in sorted(Path(site_dir).glob("s*.json")):
        if f.name == "site_points.json":
            continue
        try:
            d = json.loads(f.read_text())
        except Exception:                            # noqa: BLE001
            continue
        p = meta.get(f.stem, {})
        out[f.stem] = {"lat": p.get("lat"), "lon": p.get("lon"), "hourly": d.get("hourly") or {}}
    return out


def _sun_seconds(sw: float, lat: float, lon: float, t: dt.datetime) -> float:
    """Sunshine seconds in the hour from incoming shortwave (W/m²): its share
    of a clear-sky estimate. 0 at night."""
    doy = t.timetuple().tm_yday
    decl = math.radians(23.44) * math.sin(math.radians(360.0 * (284 + doy) / 365.0))
    solar_time = t.hour + 0.5 + lon / 15.0                 # UTC + longitude, middle of the hour
    ha = math.radians(15.0 * (solar_time - 12.0))
    la = math.radians(lat)
    sin_el = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    if sin_el <= 0.02 or sw is None:
        return 0.0
    clear = 1000.0 * sin_el ** 1.15
    return float(3600.0 * min(1.0, max(0.0, sw / clear)))


def _series_for(point: dict, archive_dir, pid: str, start: str, end: str) -> Dict[str, list]:
    """Archive first, then the forecast over it: {time: [T, RH, P, ws, wd, sw]}."""
    rows = {}
    if archive_dir:
        try:
            from pipeline import wx_archive
            a = wx_archive.series(Path(archive_dir), pid, start, end)
            if a:
                for i, t in enumerate(a["time"]):
                    rows[t] = [a[v][i] for v in wx_archive.VARS]
        except Exception as e:                       # noqa: BLE001
            print(f"[INT] Archiv {pid} nicht lesbar: {e}")
    h = point.get("hourly") or {}
    from pipeline.wx_archive import VARS
    for i, t in enumerate(h.get("time") or []):
        vals = [(h.get(v) or [None] * (i + 1))[i] for v in VARS]
        if vals[0] is not None:
            rows[t] = vals
    return rows


def overlay(site_dir, archive_dir, lats: List[float], lons: List[float], times: List[str],
            temp_m: np.ndarray, prec_m: np.ndarray, snow_m: np.ndarray,
            wspd_m: np.ndarray, wdir_m: np.ndarray, sun_m: np.ndarray) -> Optional[str]:
    """Put MeteoSwiss hours over the point series, in place. Returns a short
    note for the log and the data source label, or None when nothing was laid."""
    pts = _load_points(site_dir)
    if not pts and not archive_dir:
        return None
    ids = list(pts)
    # match each weather point to a MeteoSwiss point by position (within ~1 km)
    if pts and all(pts[i]["lat"] is not None for i in ids):
        P = np.array([[pts[i]["lat"], pts[i]["lon"]] for i in ids])
    else:
        P = None
    tkey = [str(t)[:16].replace(" ", "T") for t in times]   # Open-Meteo: "YYYY-MM-DDTHH:MM", UTC
    start, end = tkey[0], tkey[-1]
    n_pts = n_hours = 0
    shifts = []
    for k, (la, lo) in enumerate(zip(lats, lons)):
        pid = None
        if P is not None:
            d = (P[:, 0] - la) ** 2 + ((P[:, 1] - lo) * math.cos(math.radians(la))) ** 2
            j = int(np.argmin(d))
            if d[j] <= (0.01 ** 2):
                pid = ids[j]
        if pid is None:
            pid = f"s{k:04d}"                        # same order as tools/site_points.py
        rows = _series_for(pts.get(pid, {}), archive_dir, pid, start, end)
        if not rows:
            continue
        diffs = [temp_m[k, t] - rows[s][0] for t, s in enumerate(tkey)
                 if s in rows and rows[s][0] is not None and np.isfinite(temp_m[k, t])]
        dT = float(np.mean(diffs)) if len(diffs) >= 6 else 0.0
        used = 0
        for t, s in enumerate(tkey):
            r = rows.get(s)
            if not r or r[0] is None:
                continue
            T, _rh, P_, ws, wd, sw = r
            temp_m[k, t] = T + dT
            if P_ is not None:
                prec_m[k, t] = max(0.0, P_)
                ft = temp_m[k, t]
                frac = 1.0 if ft <= 0.5 else 0.0 if ft >= 2.0 else (2.0 - ft) / 1.5
                snow_m[k, t] = prec_m[k, t] * frac * SNOW_CM_PER_MM
            if ws is not None:
                wspd_m[k, t] = ws
            if wd is not None:
                wdir_m[k, t] = wd
            if sw is not None:
                sun_m[k, t] = _sun_seconds(sw, la, lo, dt.datetime.strptime(s, _FMT))
            used += 1
        if used:
            n_pts += 1
            n_hours += used
            shifts.append(dT)
    if not n_pts:
        return None
    return (f"MeteoSchweiz {n_pts}/{len(lats)} Punkte, {n_hours // max(1, n_pts)} h je Punkt, "
            f"Hoehenkorrektur Ø {np.mean(shifts):+.1f} K")
