"""Offline test for the GeoNames peak parser used to name ski tours."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_connectors.geonames_peaks import parse, parse_places, PeakIndex, name_tours

fails = []
def check(name, ok, detail=""):
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))
    if not ok: fails.append(name)

row = lambda *c: "\t".join(c)
txt = "\n".join([
    row("1", "Piz Buin", "Piz Buin", "", "46.84417", "10.11861", "T", "PK", "CH", "", "", "", "", "", "0", "3312", "3290", "Europe/Zurich", "2020"),
    row("2", "Davos", "", "", "46.8", "9.83", "P", "PPL", "CH", "", "", "", "", "", "0", "", "1560", "", ""),
    row("3", "Pischahorn", "", "", "46.8345", "9.9061", "T", "MT", "CH", "", "", "", "", "", "0", "", "2980", "", ""),
    "short\tline",
])
pk = parse(txt)
check("only peaks/mountains are kept", [p[0] for p in pk] == ["Piz Buin", "Pischahorn"], str(pk))
check("elevation falls back to the DEM column", pk[1][3] == 2980.0)
idx = PeakIndex(pk)
check("nearest peak within range", (idx.nearest(10.1190, 46.8445) or ("",))[0] == "Piz Buin")
check("nothing beyond the radius", idx.nearest(10.3, 46.9) is None)
check("empty index answers None", PeakIndex([]).nearest(9.9, 46.8) is None)
# start places and tour naming
pl = parse_places("\n".join([
    row("10", "Flüela Hospiz", "", "", "46.7480", "9.9470", "S", "HTL", "CH", "", "", "", "", "", "0", "", "2383", "", ""),
    row("11", "Dürrboden", "", "", "46.7200", "9.9000", "P", "PPLL", "CH", "", "", "", "", "", "0", "", "2007", "", ""),
    row("12", "Flüelapass", "", "", "46.7500", "9.9500", "T", "PASS", "CH", "", "", "", "", "", "0", "", "2383", "", ""),
    row("13", "Albula", "", "", "46.5800", "9.8400", "T", "PASS", "CH", "", "", "", "", "", "0", "", "2312", "", ""),
    row("14", "Piz Buin", "", "", "46.84417", "10.11861", "T", "PK", "CH", "", "", "", "", "", "0", "", "3312", "", ""),
]))
check("places: hotels, villages, passes -- not peaks", [p[0] for p in pl] == ["Flüela Hospiz", "Dürrboden", "Flüelapass", "Albula"], str(pl))
peaks = [("Schwarzhorn", 46.7370, 9.9370, 3146.0), ("Schwarzhorn", 46.5700, 9.8300, 2900.0)]
R = lambda i, hi, lo, n=None: {"id": i, "name": n, "hi": [hi[1], hi[0], 3100], "lo": [lo[1], lo[0], 2000], "coords": []}
tours = [R(1, (46.7372, 9.9372), (46.7482, 9.9468)),          # Schwarzhorn A from Flüela Hospiz
         R(2, (46.7369, 9.9369), (46.7202, 9.9002)),          # Schwarzhorn A from Dürrboden
         R(3, (46.5701, 9.8301), (46.5802, 9.8399)),          # the other Schwarzhorn
         R(4, (46.9000, 9.5000), (46.8800, 9.5100), "Ski route")]  # no summit near its top
out = name_tours(tours, peaks, pl)
names = {r["id"]: r["name"] for r in out}
check("tours without a summit are dropped", 4 not in names, str(names))
check("variants of one summit are named by their start",
      names.get(1) == "Flüela Schwarzhorn von Flüela Hospiz" and names.get(2) == "Flüela Schwarzhorn von Dürrboden", str(names))
check("same-named summits get their region", names.get(3) == "Albula Schwarzhorn", str(names))
check("variants share a group", out[0]["group"] == out[1]["group"] != out[2]["group"])
check("no peak data: nothing is dropped", len(name_tours([dict(t) for t in tours], [], pl)) == 4)
print("\nGEONAMES PEAKS OK" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)
