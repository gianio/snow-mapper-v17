#!/usr/bin/env python3
"""Tour variants: forks of one climb collapse, huts give hut variants.

    python tools/test_tour_variants.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.tour_variants import prune_variants, huts_from_places  # noqa: E402

D = 0.009


def elev(lon, lat):
    return 1000 + (lat - 46.0) / D * 300


def line(a, b, n=40):
    return [[a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n] for i in range(n + 1)]


TOP = (9.0, 46.06)
V1, V2 = (9.0, 46.0), (9.05, 46.0)
HUT = (9.0, 46.03)
# three ways from V1 that differ only by a short fork near the top, one from V2
a = line(V1, TOP)
F = (9.0, 46.05)
b = line(V1, F, 33) + line(F, (9.003, 46.055), 4)[1:] + line((9.003, 46.055), TOP, 4)[1:]
c = line(V1, F, 33) + line(F, (8.997, 46.055), 4)[1:] + line((8.997, 46.055), TOP, 4)[1:]
d = line(V2, HUT, 30) + line(HUT, TOP, 20)[1:]


def r(i, cs, frm):
    return {"id": i, "coords": cs, "group": "G", "pname": "Piz Test", "from": frm,
            "lo": [cs[0][0], cs[0][1], elev(*cs[0])], "len": 1}


out = prune_variants([r(1, a, "Dorf"), r(2, b, None), r(3, c, None), r(4, d, "Alp")], elev,
                     huts_from_places([("Testhütte", HUT[1], HUT[0], "HUT"), ("Dorf", 46.0, 9.0, "LCTY")]))
fails = []
g = [x for x in out if x["group"] == "G"]
froms = sorted(x.get("from") or "" for x in g)
if froms != ["Alp", "Dorf", "Testhütte"]:
    fails.append(f"summit variants {froms}")
z = [x for x in out if x.get("hut")]
if len(z) != 1 or not z[0]["name"].startswith("Zustieg Testhütte"):
    fails.append(f"approach {[x.get('name') for x in z]}")
if not all(x["coords"][0][1] < x["coords"][-1][1] for x in out):
    fails.append("not uphill")
if len({x["id"] for x in out}) != len(out):
    fails.append("ids not unique")
if fails:
    print("TOUR VARIANTS FAIL\n  " + "\n  ".join(fails))
    sys.exit(1)
print(f"TOUR VARIANTS OK ({len(g)} variants, {len(z)} approach)")
