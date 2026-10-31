#!/usr/bin/env python3
"""pipeline/wx_archive.py without network: filing two OGD runs (shorter lead
wins), reading a window back, the overlay in variant_a/forcing.py, pruning."""
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import wx_archive as wa  # noqa: E402

fails = []


def check(n, c, d=""):
    print(f"  [{'PASS' if c else 'FAIL'}] {n}  {d}")
    if not c:
        fails.append(n)


def run(dirp, ref, hours, temp):
    """an OGD extract like tools/ogd_extract.py writes it: hours 1..n after ref"""
    dirp.mkdir(parents=True, exist_ok=True)
    r = dt.datetime.fromisoformat(ref)
    t = [(r + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(1, hours + 1)]
    n = len(t)
    for pid in ("w000", "w001"):
        (dirp / f"{pid}.json").write_text(json.dumps({"model": "ogd-icon-ch1", "ref_time": r.strftime("%Y-%m-%dT%H:%M"),
            "hourly": {"time": t, "temperature_2m": [temp] * n, "relative_humidity_2m": [80] * n,
                       "precipitation": [0.5] * n, "wind_speed_10m": [3] * n, "wind_direction_10m": [270] * n,
                       "shortwave_radiation": [100] * n}}))
    (dirp / "clouds_ch1.npz").write_bytes(b"not a point file")


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    arc = tmp / "archive"
    run(tmp / "r1", "2026-01-10T00:00", 30, -5.0)          # lead 1..30, keeps 1..24
    n1 = wa.add_run(arc, tmp / "r1")
    check("first run: 24 lead hours per point", n1 == 48, str(n1))
    check("day files per UTC day", (arc / "ogd/2026/01/10.json.gz").exists() and (arc / "ogd/2026/01/11.json.gz").exists())
    run(tmp / "r2", "2026-01-10T06:00", 30, -2.0)          # newer run: 07-24 h get shorter leads (its hour 0 is not filed)
    n2 = wa.add_run(arc, tmp / "r2")
    s = wa.series(arc, "w000", "2026-01-10", "2026-01-10")
    check("window read back", s and s["time"][0] == "2026-01-10T01:00" and s["time"][-1] == "2026-01-10T23:00", str(s and (s["time"][0], s["time"][-1])))
    i5, i7 = s["time"].index("2026-01-10T05:00"), s["time"].index("2026-01-10T07:00")
    check("older run kept where it has the shorter lead", s["temperature_2m"][i5] == -5.0)
    check("newer run wins with the shorter lead", s["temperature_2m"][i7] == -2.0, str(s["temperature_2m"][i7]))
    again = wa.add_run(arc, tmp / "r1")                     # the old run again: nothing it may replace
    s2 = wa.series(arc, "w000", "2026-01-10", "2026-01-10")
    check("a longer lead never overwrites", s2["temperature_2m"][i7] == -2.0)
    check("unknown point -> None", wa.series(arc, "w999", "2026-01-10", "2026-01-10") is None)
    # the overlay in the forcing: archive hours over an Open-Meteo base, height-corrected
    from variant_a.forcing import _overlay_ogd
    base_t = [f"2026-01-10T{h:02d}:00" for h in range(24)]
    base = {"time": base_t, **{v: [1.0] * 24 for v in wa.VARS}}
    base["temperature_2m"] = [0.0] * 24                     # 2 K warmer than the archive (-2) after 06 h
    merged, n, dT = _overlay_ogd(base, s2)
    check("overlay fills the archived hours", n == 23, str(n))
    check("height shift measured from the overlap", abs(dT - (0 - (6 * -5.0 + 17 * -2.0) / 23)) < 1e-9, f"{dT:+.2f}")
    check("hour 0 (not archived) stays Open-Meteo", merged["temperature_2m"][0] == 0.0)
    cov = wa.coverage(arc, 3, today=dt.date(2026, 1, 11))
    check("coverage rows", [r[0].isoformat() for r in cov] == ["2026-01-10", "2026-01-11"], str(cov))
    check("prune keeps recent days", wa.prune(arc, 5, today=dt.date(2026, 1, 12)) == 0)
    check("prune drops old days", wa.prune(arc, 5, today=dt.date(2026, 2, 1)) == 2)

print("\nFAILED: " + ", ".join(fails) if fails else "\nWX ARCHIVE OK")
sys.exit(1 if fails else 0)
