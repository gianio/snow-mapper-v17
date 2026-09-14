#!/usr/bin/env python3
"""SLF-Bulletin-Parser gegen Fixtures.

Der Grund fuer Fixtures statt echter Requests: aws.slf.ch ist aus der
Build-Umgebung nicht erreichbar, und ein Test, der vom Netz abhaengt, ist
in CI ohnehin wertlos. Getestet wird also der Parser -- und vor allem,
dass er bei unerwarteten Formen NICHTS erfindet.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data_connectors import slf_bulletin as B    # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# --- CAAML-JSON, wie es die EAWS-Struktur vorgibt --------------------------
CAAML = {"bulletins": [{
    "validTime": {"startTime": "2026-01-15T17:00:00+00:00"},
    "dangerRatings": [
        {"mainValue": "3", "elevation": {"lowerBound": 2200}},
        {"mainValue": "2", "elevation": {"upperBound": 2200}},
    ],
    "avalancheProblems": [{
        "problemType": "wind_slab",
        "aspects": ["NORTH", "NORTH_EAST", "EAST"],
        "elevation": {"lowerBound": 2200, "upperBound": 3200},
    }],
    "regions": [{"regionID": "CH-7121"}, {"regionID": "CH-7122"}],
}]}

print("CAAML JSON")
r = B.parse_bulletin(CAAML)
check("both regions parsed", len(r) == 2, f"got {len(r)}")
check("max danger wins", r[0]["danger"] == 3, f"got {r[0]['danger']}")
check("lower band kept separately", r[0]["danger_lo"] == 2, f"got {r[0]['danger_lo']}")
check("aspects normalised to compass codes", r[0]["aspects"] == ["N", "NE", "E"],
      f"got {r[0]['aspects']}")
check("core-zone elevation band captured",
      (r[0]["elev_lo"], r[0]["elev_hi"]) == (2200, 3200),
      f"got {r[0]['elev_lo']}-{r[0]['elev_hi']}")
check("problem type kept", r[0]["problems"] == ["wind_slab"], f"got {r[0]['problems']}")
check("valid time kept", str(r[0]["valid"]).startswith("2026-01-15"))
check("region ids kept", {x["id"] for x in r} == {"CH-7121", "CH-7122"})

print("\nsnake_case variant (the other spelling)")
snake = {"bulletins": [{
    "danger_ratings": [{"main_value": 4}],
    "avalanche_problems": [{"problem_type": "new_snow", "aspect": ["S", "SW"],
                            "elevation": {"lower_bound": 1800}}],
    "regions": [{"region_id": "CH-2011"}],
}]}
r2 = B.parse_bulletin(snake)
check("snake_case parses", len(r2) == 1 and r2[0]["danger"] == 4, f"got {r2}")
check("snake_case aspects", r2[0]["aspects"] == ["S", "SW"], f"got {r2[0]['aspects']}")

print("\nGeoJSON variant")
gj = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"region_id": "CH-1301", "danger_level": 3,
                                       "aspects": ["N", "NW"]},
     "geometry": {"type": "Polygon", "coordinates": [[[7, 46], [8, 46], [8, 47], [7, 46]]]}},
    {"type": "Feature", "properties": {"region_id": "CH-1302"}},   # no danger -> dropped
]}
r3 = B.parse_bulletin(gj)
check("valid feature parsed", len(r3) == 1, f"got {len(r3)}")
check("geometry retained for rendering", r3[0].get("geometry", {}).get("type") == "Polygon")
check("feature without a danger level is dropped", all(x["id"] != "CH-1302" for x in r3))

print("\nrefuses to invent things")
for name, bad in [
    ("empty dict", {}),
    ("empty list", []),
    ("None", None),
    ("a string", "not json"),
    ("wrong shape", {"bulletins": "nope"}),
    ("region but no danger", {"bulletins": [{"regions": [{"regionID": "CH-1"}]}]}),
    ("danger but no region", {"bulletins": [{"dangerRatings": [{"mainValue": 3}]}]}),
    ("out-of-range danger", {"bulletins": [{"dangerRatings": [{"mainValue": 9}],
                                            "regions": [{"regionID": "CH-1"}]}]}),
    ("non-dict entries", {"bulletins": [None, 5, "x"]}),
    ("geojson with non-list features", {"type": "FeatureCollection", "features": 7}),
    ("geojson with null feature", {"type": "FeatureCollection", "features": [None]}),
]:
    check(f"{name} -> empty, no crash", B.parse_bulletin(bad) == [])

print("\nunknown aspect spellings are dropped, not guessed")
weird = {"bulletins": [{"dangerRatings": [{"mainValue": 2}],
                        "avalancheProblems": [{"aspects": ["UPWARD", "N", "banana", 7]}],
                        "regions": [{"regionID": "CH-1"}]}]}
check("only real compass codes survive",
      B.parse_bulletin(weird)[0]["aspects"] == ["N"],
      f"got {B.parse_bulletin(weird)[0]['aspects']}")

print("\nEAWS colours and labels are complete")
check("all five danger levels have a colour", set(B.DANGER_COLORS) == {1, 2, 3, 4, 5})
check("all five have a label", set(B.DANGER_LABELS) == {1, 2, 3, 4, 5})
check("level 3 is the EAWS orange", B.DANGER_COLORS[3] == "#FF9900")

print("\nfetch degrades instead of raising")
import data_connectors.slf_bulletin as mod       # noqa: E402
missing = Path("/tmp/definitely-not-here-bulletin.json")
if missing.exists():
    missing.unlink()
got = mod.fetch_bulletin(timeout=0.01, cache_path=missing)
check("unreachable API returns None, does not raise", got is None, f"got {type(got)}")

print("\n" + ("SLF BULLETIN OK" if not fails else f"FAILED: {fails}"))
sys.exit(1 if fails else 0)
