#!/usr/bin/env python3
"""Tour-Powder-Score.

Der wichtigste Test hier ist NICHT, dass der Score hoch wird, wenn Powder
liegt -- sondern dass er es NICHT wird, wenn die Route in der Kernzone des
Lawinenbulletins liegt. Ein Powder-Score, der Lawinengelaende attraktiv
macht, waere schlimmer als kein Score.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from model import tour_score as TS     # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


print("geometry")
d = TS.haversine_m(7.0, 46.5, 7.0, 46.6)
check("0.1 deg latitude is ~11.1 km", 11000 < d < 11200, f"{d:.0f} m")
check("zero distance for identical points", TS.haversine_m(7, 46, 7, 46) == 0.0)

print("\nresampling")
route = [(7.0, 46.5), (7.02, 46.5)]          # ~1.53 km due east
segs = TS.resample_route(route, step_m=100.0)
check("route is resampled to even spacing", len(segs) >= 15, f"{len(segs)} segments")
gaps = [round(b.dist_m - a.dist_m) for a, b in zip(segs, segs[1:])][:-1]
check("spacing is uniform", len(set(gaps)) <= 1, f"gaps {set(gaps)}")
check("starts at zero", segs[0].dist_m == 0.0)
check("ends at route length", abs(segs[-1].dist_m - TS.haversine_m(7.0, 46.5, 7.02, 46.5)) < 101,
      f"{segs[-1].dist_m:.0f} m")
# Unevenly digitised input must not skew the statistics.
dense = [(7.0, 46.5), (7.0001, 46.5), (7.0002, 46.5), (7.02, 46.5)]
check("dense digitising does not inflate segment count",
      abs(len(TS.resample_route(dense, 100.0)) - len(segs)) <= 1,
      f"{len(TS.resample_route(dense, 100.0))} vs {len(segs)}")
check("degenerate route does not crash", len(TS.resample_route([(7.0, 46.5)], 100.0)) == 1)
check("empty route does not crash", TS.resample_route([], 100.0) == [])

print("\ndistribution, not a mean")
def mk(qualities, slope=32.0, core=False):
    """Sampler that walks a fixed list of qualities along the route."""
    box = {"i": 0}
    def s(seg):
        q = qualities[box["i"] % len(qualities)]
        box["i"] += 1
        seg.slope_deg = slope
        seg.aspect_deg = 0.0
        seg.elev_m = 2400.0
        seg.quality = q
        seg.powdered = (q == "powder")
        seg.core_zone = core
    return s

long_route = [(7.0, 46.5), (7.1, 46.5)]      # ~7.6 km
r = TS.score_tour(long_route, mk(["powder", "powder", "windpressed", "suncrust"]), 100.0)
check("distribution has all three qualities", set(r.distribution) ==
      {"powder", "windpressed", "suncrust"}, f"{set(r.distribution)}")
check("shares sum to 1", abs(sum(r.distribution.values()) - 1.0) < 1e-6,
      f"{sum(r.distribution.values()):.4f}")
check("powder is the largest share", max(r.distribution, key=r.distribution.get) == "powder")
check("powder share ~50 %", 0.45 < r.powder_share < 0.55, f"{r.powder_share:.2f}")
check("distribution is ordered by share",
      list(r.distribution.values()) == sorted(r.distribution.values(), reverse=True))

print("\nflat approach does not drown the descent")
flat = TS.score_tour(long_route, mk(["powder"], slope=5.0), 100.0)
check("flat terrain yields no descent length", flat.descent_m == 0.0, f"{flat.descent_m}")
check("flat terrain yields no distribution", flat.distribution == {})
check("flat terrain says so", flat.verdict == "keine Abfahrtsbewertung", flat.verdict)
steep = TS.score_tour(long_route, mk(["powder"], slope=60.0), 100.0)
check("very steep terrain is excluded too", steep.descent_m == 0.0, f"{steep.descent_m}")

print("\nverdicts scale with powder share")
for qs, want in [(["powder"], "überwiegend Powder"),
                 (["powder", "windpressed"], "teilweise Powder"),
                 (["powder", "crust", "crust", "crust", "crust"], "vereinzelt Powder"),
                 (["crust"], "kein Powder erwartet")]:
    v = TS.score_tour(long_route, mk(qs), 100.0).verdict
    check(f"{qs[0]}x{len(qs)} -> {want}", v == want, f"got {v}")

print("\nTHE SAFETY PROPERTY: the core zone clamps the verdict")
perfect_but_dangerous = TS.score_tour(long_route, mk(["powder"], core=True), 100.0)
check("perfect powder in the core zone is NOT praised",
      perfect_but_dangerous.verdict == "Kernzone betroffen – Bulletin zuerst",
      f"got '{perfect_but_dangerous.verdict}'")
check("clamped flag is set", perfect_but_dangerous.clamped)
check("core zone share reported as 100 %",
      abs(perfect_but_dangerous.core_zone_share - 1.0) < 1e-6,
      f"{perfect_but_dangerous.core_zone_share:.2f}")
check("powder share is still reported honestly (not hidden)",
      perfect_but_dangerous.powder_share > 0.9,
      f"{perfect_but_dangerous.powder_share:.2f}")
check("caveat names the core zone",
      any("Kernzone" in c for c in perfect_but_dangerous.caveats))
check("caveat always disclaims avalanche assessment",
      any("keine Lawinenbeurteilung" in c for c in perfect_but_dangerous.caveats))

print("\nclamp threshold behaves at the boundary")
def partial(share):
    box = {"i": 0, "n": 0}
    def s(seg):
        box["n"] += 1
        seg.slope_deg = 32.0; seg.aspect_deg = 0.0; seg.elev_m = 2400.0
        seg.quality = "powder"; seg.powdered = True
        seg.core_zone = (box["n"] % 100) < int(share * 100)
    return s
low = TS.score_tour(long_route, partial(0.05), 100.0)
high = TS.score_tour(long_route, partial(0.40), 100.0)
check("5 % core zone does not clamp", not low.clamped, f"share {low.core_zone_share:.2f}")
check("40 % core zone clamps", high.clamped, f"share {high.core_zone_share:.2f}")
check("a non-clamped tour still praises good snow",
      low.verdict == "überwiegend Powder", low.verdict)

print("\nweighting is by length, not segment count")
uneven = [(7.0, 46.5), (7.0005, 46.5), (7.1, 46.5)]
r2 = TS.score_tour(uneven, mk(["powder", "windpressed"]), 100.0)
check("uneven input still sums to 1", abs(sum(r2.distribution.values()) - 1.0) < 1e-6)

print("\nswisstopo route parsing")
from data_connectors import swisstopo_routes as R   # noqa: E402
FC = {"features": [
    {"id": "a", "properties": {"name": "Rothorn"},
     "geometry": {"type": "LineString",
                  "coordinates": [[7.0, 46.5], [7.01, 46.51], [7.02, 46.52], [7.03, 46.53]]}},
    {"id": "b", "properties": {"bezeichnung": "Piz Sol"},
     "geometry": {"type": "MultiLineString", "coordinates": [
         [[8.0, 46.9], [8.01, 46.91], [8.02, 46.92], [8.03, 46.93]],
         [[8.1, 46.9], [8.11, 46.91], [8.12, 46.92], [8.13, 46.93]]]}},
    {"id": "c", "properties": {}, "geometry": {"type": "LineString",
                                               "coordinates": [[7, 46], [7.001, 46]]}},
]}
rt = R.parse_routes(FC)
check("LineString and both MultiLineString parts parsed", len(rt) == 3, f"got {len(rt)}")
check("name read from 'name'", rt[0]["name"] == "Rothorn", f"{rt[0]['name']}")
check("name read from the German key too",
      any(x["name"] == "Piz Sol" for x in rt), [x["name"] for x in rt])
check("multi-part ids are made unique", len({x["id"] for x in rt}) == 3,
      f"{[x['id'] for x in rt]}")
check("map-edge fragments are dropped", all(len(x["coords"]) >= 4 for x in rt))

for name, bad in [("None", None), ("a list", []), ("no features", {}),
                  ("features not a list", {"features": 7}),
                  ("null feature", {"features": [None]}),
                  ("point geometry", {"features": [{"geometry": {"type": "Point",
                                                                 "coordinates": [7, 46]}}]}),
                  ("non-numeric coords", {"features": [{"geometry": {
                      "type": "LineString",
                      "coordinates": [["a", "b"], ["c", "d"], [1, 2], [3, 4]]}}]})]:
    got = R.parse_routes(bad)
    check(f"{name} -> no invented routes", got == [] or all(len(x["coords"]) >= 4 for x in got),
          f"got {got}")
check("missing file returns empty", R.load_routes(Path("/tmp/no-such-routes.geojson")) == [])

# The live GeoPackage is LV95 and puts placeholder strings in the name column.
# Both were found by running the real parser on a runner, not by inspection.
print("\nLV95 reprojection (the live data is EPSG:2056, not WGS84)")
lv95 = [{"id": "x", "name": None,
         "coords": [[2600000.0, 1200000.0], [2601000.0, 1201000.0]]}]
out = R._to_wgs84([dict(r) for r in lv95], "EPSG:2056")
check("LV95 metres become degrees", out and 5 < out[0]["coords"][0][0] < 11
      and 45 < out[0]["coords"][0][1] < 48,
      f"{out[0]['coords'][0] if out else 'none'}")
wgs = [{"id": "y", "name": None, "coords": [[7.5, 46.5], [7.6, 46.6]]}]
same = R._to_wgs84([dict(r) for r in wgs], "EPSG:4326")
check("already-degrees data is left alone", same[0]["coords"][0] == [7.5, 46.5],
      f"{same[0]['coords'][0]}")
check("empty input is safe", R._to_wgs84([], "EPSG:2056") == [])

print("\nplaceholder names are not names")
for bad in ("Keine Routeninfo verfügbar", "keine angabe", "No route info",
            "unbekannt", "n/a"):
    check(f"{bad!r} rejected", R._is_placeholder(bad))
for good in ("Rothorn", "Piz Sol", "Chli Windgällen"):
    check(f"{good!r} kept", not R._is_placeholder(good))
fc_ph = {"features": [{"id": "p", "properties": {"name": "Keine Routeninfo verfügbar"},
                       "geometry": {"type": "LineString",
                                    "coordinates": [[7, 46], [7.01, 46.01],
                                                    [7.02, 46.02], [7.03, 46.03]]}}]}
check("placeholder yields name=None, route still parsed",
      R.parse_routes(fc_ph)[0]["name"] is None,
      f"{R.parse_routes(fc_ph)[0]['name']!r}")
check("attribution names swisstopo", "swisstopo" in R.ATTRIBUTION, R.ATTRIBUTION)

print("\nend to end: a parsed route can be scored")
scored = TS.score_tour(rt[0]["coords"], mk(["powder", "windpressed"]), 50.0)
check("a real parsed route scores without error", scored.length_m > 0, f"{scored.length_m:.0f} m")
check("and still carries the disclaimer",
      any("keine Lawinenbeurteilung" in c for c in scored.caveats))

print("\n" + ("TOUR SCORE OK" if not fails else f"FAILED: {fails}"))
sys.exit(1 if fails else 0)
