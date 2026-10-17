#!/usr/bin/env python3
"""The binary data container (SMB1) the boot loader reads in a Web Worker.

Checks that it carries exactly the bytes the JSON blob carries as base64,
that every blob starts on an 8-byte boundary (the client views them in
place), and that the boot loader parses the same layout."""
import base64
import gzip
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import interactive_export as ie  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")
    if not cond:
        FAILS.append(name)


def main():
    blobs = {"SNOW": base64.b64encode(bytes(range(256)) * 3).decode(),
             "ODD": base64.b64encode(b"\x01\x02\x03").decode(),
             "EMPTY": "",
             "PNG": base64.b64encode(os.urandom(1001)).decode()}
    meta = {"T": 3, "width": 16, "height": 16, "snow_scale": 0.1, "note": "Schnee ü"}
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "x.bin.gz"
        ie.write_bin_blob(f, meta, blobs)
        m2, b2 = ie.read_bin_blob(f)
        check("meta survives", m2 == meta, str(m2))
        check("every blob is byte-identical to its base64",
              all(b2[k] == base64.b64decode(v) for k, v in blobs.items()))
        raw = gzip.decompress(f.read_bytes())
        n = int.from_bytes(raw[4:8], "little")
        base = 8 + n + ((-(8 + n)) % 8)
        import json
        ix = json.loads(raw[8:8 + n])["blobs"]
        check("data section and every blob start on 8 bytes",
              base % 8 == 0 and all(o % 8 == 0 for o, _ in ix.values()), str(ix))
    # the coarse first-start copy keeps every f-th cell exactly where it was
    import numpy as np
    T, W, H, f = 2, 9, 5, 4
    cube = np.arange(T * H * W, dtype=np.uint8).reshape(T, H, W)
    meta2 = {"T": T, "width": W, "height": H, "bounds": [46.0, 6.0, 47.0, 10.0],
             "rad": {"width": W, "height": H, "K": 2, "bounds": [46.0, 6.0, 47.0, 10.0]}}
    blobs2 = {"SNOW": base64.b64encode(cube.tobytes()).decode(),
              "MELEV": base64.b64encode(cube[0].tobytes()).decode(),
              "RHOR": base64.b64encode(np.zeros((2, H, W), np.uint8).tobytes()).decode(),
              "SPD": base64.b64encode(bytes(7)).decode()}
    lm, lb = ie.coarse_meta_blobs(meta2, blobs2, f)
    sub = np.frombuffer(lb["SNOW"], np.uint8).reshape(T, lm["height"], lm["width"])
    check("coarse grid is every f-th cell", (lm["width"], lm["height"]) == (3, 2)
          and (sub == cube[:, ::f, ::f]).all(), str((lm["width"], lm["height"])))
    la0, lo0, la1, lo1 = lm["bounds"]
    # cell (y=1, x=2) of the coarse grid is cell (4, 8) of the full one
    lat_c = la1 - (la1 - la0) * 1 / (lm["height"] - 1)
    lon_c = lo0 + (lo1 - lo0) * 2 / (lm["width"] - 1)
    lat_f = 47.0 - 1.0 * 4 / (H - 1)
    lon_f = 6.0 + 4.0 * 8 / (W - 1)
    check("... and at the same place", abs(lat_c - lat_f) < 1e-9 and abs(lon_c - lon_f) < 1e-9,
          f"{lat_c},{lon_c} vs {lat_f},{lon_f}")
    check("per-cell and radiation fields follow, wind points stay",
          len(lb["MELEV"]) == 6 and len(lb["RHOR"]) == 12 and len(lb["SPD"]) == 7 and lm["coarse"] == f)
    js = ie._BOOT_SPLIT
    check("the boot loader checks the SMB1 magic", "u8[0]!==83||u8[1]!==77||u8[2]!==66||u8[3]!==49" in js)
    check("... and aligns the data section the same way", "base=8+n+((8-(8+n)%8)%8)" in js)
    check("the boot loader falls back to the JSON blob", "var useGz=!!(lj.gz&&window.DecompressionStream)" in js)
    check("app.js takes the worker's running sum only when it fits",
          re.search(r"__D\.cum instanceof Float32Array&&__D\.cum\.length===\(T\+1\)\*NP", ie._app_js()) is not None)
    print("\nBIN BLOB " + ("OK" if not FAILS else f"FAILED: {FAILS}"))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
