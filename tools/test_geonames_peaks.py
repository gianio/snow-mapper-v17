"""Offline test for the GeoNames peak parser used to name ski tours."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_connectors.geonames_peaks import parse, PeakIndex

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
print("\nGEONAMES PEAKS OK" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)
