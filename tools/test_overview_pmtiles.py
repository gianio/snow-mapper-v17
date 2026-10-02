#!/usr/bin/env python3
"""Overview PMTiles: a synthetic frame survives the trip into web-map tiles."""
import io, json, sys, tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_overview_pmtiles as m

FAILS = []
def check(name, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def main():
    from pmtiles.reader import Reader, MmapSource, all_tiles
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        exp = td / "exp"; (exp / "layers").mkdir(parents=True)
        bounds = [[45.8, 5.9], [47.8, 10.5]]
        # left half red, right half blue, a transparent strip at the top
        img = np.zeros((200, 460, 4), np.uint8)
        img[20:, :230] = (200, 30, 30, 255); img[20:, 230:] = (30, 30, 200, 255)
        Image.fromarray(img, "RGBA").save(exp / "layers" / "ski6_2026-03-30T1200.png")
        (exp / "manifest.json").write_text(json.dumps({
            "bounds": bounds, "tags": ["2026-03-30T1200"],
            "layers": {"ski6": {"file": "layers/ski6_{tag}.png"}}}))
        n = m.build(exp, td / "out", zmax=8)
        f = td / "out" / "ski6" / "2026-03-30T1200.pmtiles"
        check("one archive per view and frame", n == 1 and f.exists())
        with open(f, "rb") as fh:
            r = Reader(MmapSource(fh))
            h = r.header()
            tiles = dict(all_tiles(r.get_bytes))
            check("zoom 5..8 present", h["min_zoom"] == 5 and h["max_zoom"] == 8,
                  f"{h['min_zoom']}..{h['max_zoom']}")
            # Bern (46.95, 7.45) is in the red (west) half; Chur (46.85, 9.53) in the blue
            def px(lat, lon, z=8):
                import math
                k = 2 ** z; x = (lon + 180) / 360 * k
                y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * k
                t = tiles.get((z, int(x), int(y)))
                if t is None:
                    return None
                a = np.asarray(Image.open(io.BytesIO(t)).convert("RGBA"))
                return tuple(a[int((y % 1) * 256), int((x % 1) * 256)])
            check("west of the middle stays red", px(46.95, 7.45)[:3] == (200, 30, 30), str(px(46.95, 7.45)))
            check("east of the middle stays blue", px(46.85, 9.53)[:3] == (30, 30, 200), str(px(46.85, 9.53)))
            top = px(47.75, 8.0)
            check("transparent stays transparent", top is None or top[3] == 0, str(top))
    print("\nOVERVIEW PMTILES " + ("OK" if not FAILS else f"FAILED: {FAILS}"))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
