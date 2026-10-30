#!/usr/bin/env python3
"""pipeline/ogd_clouds.py: the ICON cloud fields on the map grid -- nearest
cell, valid times, the finer model first, total cover."""
import sys, tempfile
from datetime import datetime
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import ogd_clouds as oc
fails = []
def check(n, c, d=""):
    print(f"  [{'PASS' if c else 'FAIL'}] {n}  {d}")
    if not c: fails.append(n)

with tempfile.TemporaryDirectory() as d:
    lat = np.array([46.0, 46.0, 47.0, 47.0], np.float32); lon = np.array([8.0, 9.0, 8.0, 9.0], np.float32)
    lead = np.array([0, 1, 2], np.float32)
    low = np.zeros((3, 4), np.uint8); low[1] = [10, 20, 30, 40]
    mid = np.zeros((3, 4), np.uint8); high = np.zeros((3, 4), np.uint8); high[1, 3] = 50
    np.savez_compressed(Path(d) / "clouds_ch1.npz", lat=lat, lon=lon, lead=lead, low=low, mid=mid, high=high,
                        ref=np.array("2026-10-30T06:00"))
    lead2 = np.arange(0, 6, dtype=np.float32)
    z = np.full((6, 4), 99, np.uint8)
    np.savez_compressed(Path(d) / "clouds_ch2.npz", lat=lat, lon=lon, lead=lead2, low=z, mid=z, high=z,
                        ref=np.array("2026-10-30T06:00"))
    f = oc.load(d)
    check("both models load, finest first", [x["model"] for x in f] == ["ch1", "ch2"])
    gl = np.array([[46.02, 46.98], [46.01, 47.1]]); go = np.array([[8.03, 8.97], [8.96, 9.02]])
    cf = oc.CloudFrames(f, gl, go)
    r = cf.at("2026-10-30T07:00")
    check("nearest ICON cell per map cell", r is not None and r[0].tolist() == [[10, 40], [20, 40]], str(r and r[0].tolist()))
    check("frames keep the grid's shape", r[0].shape == (2, 2))
    check("CH1 wins an hour both have", float(r[1].max()) == 0.0)
    r2 = cf.at("2026-10-30T10:00")
    check("CH2 fills the hours after CH1", r2 is not None and float(r2[0][0, 0]) == 99.0)
    check("an hour neither has", cf.at("2026-10-31T10:00") is None)
    check("source names the models used", cf.source() == "MeteoSchweiz ICON-CH1/ICON-CH2", str(cf.source()))
    t = oc.total(np.array(50.0), np.array(50.0), np.array(0.0))
    check("random overlap: 50 % + 50 % = 75 %", abs(float(t) - 75.0) < 1e-6, str(t))
    check("total never above 100", float(oc.total(np.array(100.0), np.array(100.0), np.array(100.0))) == 100.0)

print("\nFAILED: " + ", ".join(fails) if fails else "\nOGD CLOUDS OK")
sys.exit(1 if fails else 0)
