#!/usr/bin/env python3
"""data_connectors/osm_pistes.py without the network: packing, simplifying,
delta encoding, kinds and difficulties."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data_connectors import osm_pistes as op
fails = []
def check(n, c, d=""):
    print(f"  [{'PASS' if c else 'FAIL'}] {n}  {d}")
    if not c: fails.append(n)

def line(n, lon0=9.8, lat0=46.8, step=0.0001, wiggle=0.0):
    return [{"lon": lon0 + i * step, "lat": lat0 + (wiggle if i % 2 else 0)} for i in range(n)]

payload = {"elements": [
    {"type": "way", "tags": {"piste:type": "downhill", "piste:difficulty": "intermediate"}, "geometry": line(50)},
    {"type": "way", "tags": {"piste:type": "downhill", "piste:difficulty": "freeride"}, "geometry": line(3)},
    {"type": "way", "tags": {"piste:type": "nordic"}, "geometry": line(4)},
    {"type": "way", "tags": {"aerialway": "chair_lift", "name": "Sessel Jakobshorn"}, "geometry": line(2)},
    {"type": "way", "tags": {"aerialway": "station"}, "geometry": line(2)},
    {"type": "way", "tags": {"piste:type": "downhill"}, "geometry": line(1)},
    {"type": "node", "lat": 46.8, "lon": 9.8},
]}
d = op.pack(payload)
check("attribution carried", d["attr"].startswith("©") and "OpenStreetMap" in d["attr"])
check("pistes kept, single points dropped", len(d["p"]) == 3, str(len(d["p"])))
check("lift kinds only (stations dropped)", len(d["l"]) == 1 and d["l"][0][0] == 1 and d["l"][0][1] == "Sessel Jakobshorn")
check("difficulty: intermediate = 2, freeride = 4", d["p"][0][1] == 2 and d["p"][1][1] == 4)
check("nordic kind, no difficulty", d["p"][2][0] == 1 and d["p"][2][1] is None)
c = d["p"][0][2]
check("a straight line simplifies to its ends", len(c) == 4, str(len(c)))
check("first point absolute in 1e-5 degrees", c[0] == 980000 and c[1] == 4680000, str(c[:2]))
check("then deltas", c[2] == 490 and c[3] == 0, str(c[2:]))
w = op.pack({"elements": [{"type": "way", "tags": {"piste:type": "downhill"}, "geometry": line(20, wiggle=0.001)}]})
check("a zig-zag of ~110 m keeps its corners", len(w["p"][0][2]) > 10, str(len(w["p"][0][2])))

print("\nFAILED: " + ", ".join(fails) if fails else "\nOSM PISTES OK")
sys.exit(1 if fails else 0)
