#!/usr/bin/env python3
"""Tour graph: merging and splitting routes give one climb per possibility.

    python tools/test_tour_graph.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.tour_graph import climbs_from_lines  # noqa: E402

# a synthetic valley: height rises to the north-east, two summits
D = 0.009                                     # ~1 km north, ~700 m east


def elev(lon, lat):
    return 1000 + (lat - 46.0) / D * 300 + (lon - 9.0) / D * 100


def line(a, b, n=20):
    return [[a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n] for i in range(n + 1)]


V1 = (9.0, 46.0)           # valley start 1
V2 = (9.02, 46.0)          # valley start 2
J = (9.01, 46.02)          # junction where both valleys meet
S = (9.01, 46.04)          # fork point
TOP_A = (9.0, 46.06)
TOP_B = (9.03, 46.07)
MID = (9.01, 46.03)        # a T: the spur below joins the S line in its middle
SPUR = (9.03, 46.012)

raw = [
    {"id": 1, "name": "Weg 1", "coords": line(V1, J)},
    # drawn top-down: orientation must not matter
    {"id": 2, "name": "Weg 2", "coords": line(J, V2)[::-1][::-1][::-1]},
    {"id": 3, "name": "Stamm", "coords": line(J, S, 40)},
    {"id": 4, "name": "Gipfel A", "coords": line(S, TOP_A)},
    {"id": 5, "name": "Gipfel B", "coords": line(TOP_B, S)},
    {"id": 6, "name": "Zustieg", "coords": line(SPUR, (MID[0] + 0.0001, MID[1]))},
]
climbs = climbs_from_lines(raw, elev)


def ends(c):
    lo, hi = c["coords"][0], c["coords"][-1]
    return (round(lo[0], 3), round(lo[1], 3)), (round(hi[0], 3), round(hi[1], 3))


got = sorted(ends(c) for c in climbs)
fails = []
# every climb runs low -> high
for c in climbs:
    if elev(*c["coords"][0]) >= elev(*c["coords"][-1]):
        fails.append(f"not uphill: {ends(c)}")
# 3 low ends (V1, V2, the T spur) x 2 tops = 6 climbs
starts = {(round(V1[0], 3), round(V1[1], 3)), (round(V2[0], 3), round(V2[1], 3)), (round(SPUR[0], 3), round(SPUR[1], 3))}
tops = {(round(TOP_A[0], 3), round(TOP_A[1], 3)), (round(TOP_B[0], 3), round(TOP_B[1], 3))}
want = sorted((s, t) for s in starts for t in tops)
if got != want:
    fails.append(f"climbs {got} != {want}")
ids = [c["id"] for c in climbs]
if len(set(ids)) != len(ids):
    fails.append("ids not unique")
# deterministic ids
if sorted(ids) != sorted(c["id"] for c in climbs_from_lines(raw, elev)):
    fails.append("ids not stable")

# a short parallel double (two lines side by side over 100 m) stays one climb
raw2 = [{"id": 1, "name": "x", "coords": line(V1, J)},
        {"id": 2, "name": "x", "coords": line(J, (9.011, 46.025), 5)},
        {"id": 3, "name": "x", "coords": line(J, (9.0102, 46.0251), 5)[:-1] + [[9.011, 46.025]]},
        {"id": 4, "name": "x", "coords": line((9.011, 46.025), TOP_A)}]
c2 = climbs_from_lines(raw2, elev)
if len(c2) != 1:
    fails.append(f"parallel double gave {len(c2)} climbs")

if fails:
    print("TOUR GRAPH FAIL\n  " + "\n  ".join(fails))
    sys.exit(1)
print(f"TOUR GRAPH OK ({len(climbs)} climbs)")
