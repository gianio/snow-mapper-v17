#!/usr/bin/env python3
"""Compare MeteoSwiss OGD point series with Open-Meteo's for the same hours."""
from __future__ import annotations
import json, sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from variant_a import forcing

pts = json.loads(Path(sys.argv[1]).read_text())
ogd = Path(sys.argv[2])
print("### OGD vs Open-Meteo\n")
print("| point | model | hours | T bias K | T r | precip OGD / OM mm | wind bias m/s | SW mean OGD / OM W/m² |")
print("|---|---|---|---|---|---|---|---|")
for p in pts:
    f = ogd / f"{p['id']}.json"
    if not f.exists():
        print(f"| {p['id']} | missing | | | | | | |"); continue
    o = json.loads(f.read_text())
    h = o["hourly"]
    s, e = h["time"][0][:10], h["time"][-1][:10]
    om, m, notes = forcing.fetch_weather(p["lat"], p["lon"], p.get("ref_elev", 2000), s, e,
                                         models=["meteoswiss_icon_ch1", "meteoswiss_icon_ch2"])
    if not om:
        print(f"| {p['id']} | {o['model']} | OM failed {notes} | | | | | |"); continue
    pos = {t: i for i, t in enumerate(om["time"])}
    rows = []
    for j, t in enumerate(h["time"]):
        i = pos.get(t)
        if i is None:
            continue
        v = [h["temperature_2m"][j], om["temperature_2m"][i], h["precipitation"][j],
             om["precipitation"][i], h["wind_speed_10m"][j], om["wind_speed_10m"][i],
             h["shortwave_radiation"][j], om["shortwave_radiation"][i]]
        if all(x is not None for x in v):
            rows.append(v)
    if not rows:
        print(f"| {p['id']} | {o['model']} | 0 common hours | | | | | |"); continue
    import numpy as np
    a = np.array(rows, float)
    r = np.corrcoef(a[:, 0], a[:, 1])[0, 1]
    print(f"| {p['id']} | {o['model']} | {len(a)} | {np.mean(a[:,0]-a[:,1]):+.2f} | {r:.2f} | "
          f"{a[:,2].sum():.1f} / {a[:,3].sum():.1f} | {np.mean(a[:,4]-a[:,5]):+.2f} | "
          f"{a[:,6].mean():.0f} / {a[:,7].mean():.0f} |")
print("\nT bias includes the height difference between the ICON cell and Open-Meteo's "
      "grid point; the pipeline removes it by aligning on overlapping hours. What must "
      "match is the correlation, precipitation totals, wind and radiation.")
