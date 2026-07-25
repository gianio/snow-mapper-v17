"""Unit test: verify Horn aspect + 8-way classification on synthetic tilted planes.

Each plane has a known downslope direction; the classifier must return the
matching 8-way sector. Run: python tools/test_aspect.py
"""
import sys, os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.interactive_export import _corrected_aspect, _aspect_classify

RES = 60.0
K = 30.0  # vertical exaggeration so slope >= 5 deg

def sector(zf):
    R, C = np.mgrid[0:40, 0:40].astype(float)
    z = zf(R, C) * K
    a, sl = _corrected_aspect(z, RES)
    cls = _aspect_classify(np.nan_to_num(a), sl)
    core = cls[12:28, 12:28]
    vals, counts = np.unique(core, return_counts=True)
    return int(vals[np.argmax(counts)]), float(np.nanmedian(a[12:28, 12:28]))

# row increases downward = south; col increases rightward = east.
# downslope faces away from the high ground.
CASES = [
    ("N",  1, lambda R, C:  R),        # high in south  -> faces N
    ("NE", 2, lambda R, C:  R - C),    # high in SW     -> faces NE
    ("E",  3, lambda R, C: -C),        # high in west   -> faces E
    ("SE", 4, lambda R, C: -R - C),    # high in NW     -> faces SE
    ("S",  5, lambda R, C: -R),        # high in north  -> faces S
    ("SW", 6, lambda R, C: -R + C),    # high in NE     -> faces SW
    ("W",  7, lambda R, C:  C),        # high in east   -> faces W
    ("NW", 8, lambda R, C:  R + C),    # high in SE     -> faces NW
]

ok = True
for name, expect, zf in CASES:
    cls, deg = sector(zf)
    good = cls == expect
    ok = ok and good
    print(f"{name:2s}: class={cls} expect={expect}  aspect={deg:5.1f}deg  {'OK' if good else 'FAIL'}")
print("ALL", "OK" if ok else "FAIL")
sys.exit(0 if ok else 1)
