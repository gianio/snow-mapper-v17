#!/usr/bin/env python3
"""pipeline/ogd_site.py without network: MeteoSwiss hours (archive + newest
forecast) laid over the site's Open-Meteo point series -- height shift,
precipitation -> new snow, wind, sunshine, untouched points, no data."""
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import ogd_site, wx_archive  # noqa: E402

fails = []


def check(n, c, d=""):
    print(f"  [{'PASS' if c else 'FAIL'}] {n}  {d}")
    if not c:
        fails.append(n)


T0 = dt.datetime(2026, 1, 10, 0, 0)
N = 48
TIMES = [(T0 + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(N)]
LATS, LONS = [46.80, 46.80, 46.60], [9.80, 10.00, 8.00]


def om():
    """Open-Meteo style point series: 3 points x 48 h"""
    z = lambda v: np.full((3, N), v, dtype="float64")
    return z(-2.0), z(0.0), z(0.0), z(5.0), z(180.0), z(1800.0)


def hourly(hours, temp, prec=1.0, sw=0.0):
    t = [(T0 + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in hours]
    n = len(t)
    return {"time": t, "temperature_2m": [temp] * n, "relative_humidity_2m": [90] * n,
            "precipitation": [prec] * n, "wind_speed_10m": [8.0] * n, "wind_direction_10m": [300.0] * n,
            "shortwave_radiation": [sw] * n}


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    site = tmp / "site"
    site.mkdir()
    pts = [{"id": "s0000", "lat": 46.8, "lon": 9.8}, {"id": "s0001", "lat": 46.8, "lon": 10.0},
           {"id": "s0002", "lat": 46.6, "lon": 8.0}]
    (site / "site_points.json").write_text(json.dumps(pts))
    # forecast from hour 24 on for s0000 and s0001 (ICON 1 K colder than OM -> shift +1)
    (site / "s0000.json").write_text(json.dumps({"hourly": hourly(range(24, N), -3.0, prec=2.0)}))
    (site / "s0001.json").write_text(json.dumps({"hourly": hourly(range(24, N), 3.0, prec=1.0)}))

    # 1. forecast only
    temp, prec, snow, ws, wd, sun = om()
    note = ogd_site.overlay(site, None, LATS, LONS, TIMES, temp, prec, snow, ws, wd, sun)
    check("note names the points", bool(note) and "2/3" in note, note)
    check("hours before the forecast stay Open-Meteo", temp[0, 0] == -2.0 and prec[0, 10] == 0.0)
    # s0000: ICON -3, OM -2 over 24 overlap hours -> dT = +1 -> -2
    check("height shift applied (s0000)", abs(temp[0, 30] - (-2.0)) < 1e-9, f"{temp[0, 30]}")
    check("precipitation from MeteoSwiss", prec[0, 30] == 2.0)
    check("cold: all of it snow (0.7 cm/mm)", abs(snow[0, 30] - 1.4) < 1e-9, f"{snow[0, 30]}")
    # s0001: ICON +3, OM -2 -> dT = -5 -> -2 (shift brings it to the point's height)
    check("height shift applied (s0001)", abs(temp[1, 30] - (-2.0)) < 1e-9, f"{temp[1, 30]}")
    check("wind from MeteoSwiss", ws[0, 30] == 8.0 and wd[0, 30] == 300.0)
    check("no sun at night (sw 0)", sun[0, 24] == 0.0)
    check("point without MeteoSwiss untouched", temp[2, 30] == -2.0 and ws[2, 30] == 5.0 and prec[2, 30] == 0.0)

    # 2. warm: rain, no snow; mixed at 1.25 °C -> half
    temp, prec, snow, ws, wd, sun = om()
    temp[:] = 5.0
    (site / "s0000.json").write_text(json.dumps({"hourly": hourly(range(24, 29), 5.0, prec=2.0)}))
    ogd_site.overlay(site, None, LATS, LONS, TIMES, temp, prec, snow, ws, wd, sun)
    check("fewer than 6 overlap hours: no shift", temp[0, 25] == 5.0)
    check("warm: rain, no new snow", prec[0, 25] == 2.0 and snow[0, 25] == 0.0)
    temp, prec, snow, ws, wd, sun = om()
    temp[:] = 1.25
    (site / "s0000.json").write_text(json.dumps({"hourly": hourly(range(24, 29), 1.25, prec=2.0)}))
    ogd_site.overlay(site, None, LATS, LONS, TIMES, temp, prec, snow, ws, wd, sun)
    check("1.25 °C: half snow", abs(snow[0, 25] - 0.7) < 1e-9, f"{snow[0, 25]}")

    # 3. sunshine by day: clear sky -> full hour, overcast -> little
    t = dt.datetime(2026, 1, 10, 11, 0)
    full = ogd_site._sun_seconds(900.0, 46.8, 9.8, t)
    dim = ogd_site._sun_seconds(60.0, 46.8, 9.8, t)
    check("clear midday: ~3600 s", full == 3600.0, f"{full}")
    check("overcast midday: little", 0 < dim < 1200, f"{dim:.0f}")
    check("night: 0", ogd_site._sun_seconds(500.0, 46.8, 9.8, dt.datetime(2026, 1, 10, 22, 0)) == 0.0)

    # 4. archive for the past hours, forecast on top where both have an hour
    arc = tmp / "archive"
    run = tmp / "run"
    run.mkdir()
    (run / "s0000.json").write_text(json.dumps({"model": "ogd-icon-ch1", "ref_time": TIMES[0],
                                                "hourly": hourly(range(1, 20), -4.0, prec=0.5)}))
    filed = wx_archive.add_run(arc, run)
    check("archive filed hours", filed > 0, f"{filed}")
    (site / "s0000.json").write_text(json.dumps({"hourly": hourly(range(10, N), -6.0, prec=3.0)}))
    temp, prec, snow, ws, wd, sun = om()
    note = ogd_site.overlay(site, arc, LATS, LONS, TIMES, temp, prec, snow, ws, wd, sun)
    check("archive hour used", prec[0, 5] == 0.5, f"{prec[0, 5]}")
    check("forecast wins over archive", prec[0, 15] == 3.0, f"{prec[0, 15]}")
    check("hour 0 (in neither) stays Open-Meteo", prec[0, 0] == 0.0)

    # 5. no site points file: ids by order (s0000 = first weather point)
    site2 = tmp / "site2"
    site2.mkdir()
    (site2 / "s0001.json").write_text(json.dumps({"hourly": hourly(range(24, N), -2.0, prec=4.0)}))
    temp, prec, snow, ws, wd, sun = om()
    ogd_site.overlay(site2, None, LATS, LONS, TIMES, temp, prec, snow, ws, wd, sun)
    check("ids by order without site_points.json", prec[1, 30] == 4.0 and prec[0, 30] == 0.0)

    # 6. nothing there: None, series untouched
    temp, prec, snow, ws, wd, sun = om()
    check("no data: None", ogd_site.overlay(tmp / "missing", None, LATS, LONS, TIMES,
                                           temp, prec, snow, ws, wd, sun) is None)
    check("no data: untouched", (temp == -2.0).all() and (prec == 0.0).all())
    empty = tmp / "empty"
    empty.mkdir()
    check("empty archive: None", ogd_site.overlay(None, empty, LATS, LONS, TIMES,
                                                 temp, prec, snow, ws, wd, sun) is None)

print(f"\n{'OK' if not fails else 'FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
