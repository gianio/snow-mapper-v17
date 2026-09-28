#!/usr/bin/env python3
"""tools/ogd_extract.py without the network: array handling and the ICON
conventions (accumulated precipitation, run-averaged radiation, u/v wind).
Needs xarray (the OGD venv has it); skipped cleanly without."""
import datetime as dt, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
fails = []
def check(n, c, d=""):
    print(f"  [{'PASS' if c else 'FAIL'}] {n}  {d}")
    if not c: fails.append(n)
try:
    import xarray as xr
except ImportError:
    print("xarray not installed -- skipped"); sys.exit(0)
import ogd_extract as o

lt = np.array([0, 1, 2, 3], dtype="timedelta64[h]").astype("timedelta64[ns]")
da = xr.DataArray(np.arange(16, dtype=float).reshape(1, 1, 4, 4),
                  dims=("eps", "ref_time", "lead_time", "cell"),
                  coords={"ref_time": [np.datetime64("2026-09-28T12:00", "ns")], "lead_time": lt,
                          "lat": ("cell", np.radians([46.0, 46.5, 47.0, 47.5])),
                          "lon": ("cell", np.radians([8, 9, 10, 11.]))})
v, lead, ref = o._series(da)
check("lead times in hours (timedelta64 is an integer subclass)", lead == [0, 1, 2, 3], str(lead))
check("reference time", ref == dt.datetime(2026, 9, 28, 12), str(ref))
check("(lead, cell) layout", v.shape == (4, 4) and v[1, 0] == 4.0)
lat, lon = o._latlon(da)
check("radians -> degrees", abs(lat[1] - 46.5) < 1e-9 and abs(lon[3] - 11) < 1e-9)
h = o.to_hourly({"T_2M": np.array([273.15, 274.15, 275.15, 276.15]),
                 "TD_2M": np.array([272.15] * 4), "U_10M": np.zeros(4), "V_10M": np.full(4, -5.0),
                 "TOT_PREC": np.array([0, 1, 3, 3.]), "ASWDIR_S": np.array([0, 100, 150, 200.]),
                 "ASWDIFD_S": np.array([0, 50, 50, 50.])}, [0, 1, 2, 3], dt.datetime(2026, 9, 28, 12))
check("hour 0 dropped, times are valid times", h["time"][0] == "2026-09-28T13:00" and len(h["time"]) == 3)
check("accumulated precipitation -> hourly", h["precipitation"] == [1.0, 2.0, 0.0], str(h["precipitation"]))
check("run-averaged radiation -> hourly means", h["shortwave_radiation"] == [150.0, 250.0, 350.0],
      str(h["shortwave_radiation"]))
check("v = -5 is wind FROM the north", h["wind_direction_10m"][0] == 0.0 and h["wind_speed_10m"][0] == 5.0)
check("RH from the dew point", 80 < h["relative_humidity_2m"][0] < 92, str(h["relative_humidity_2m"][0]))
print("\n" + ("OGD EXTRACT OK" if not fails else f"FAILED: {fails}"))
sys.exit(1 if fails else 0)
