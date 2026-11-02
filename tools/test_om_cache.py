#!/usr/bin/env python3
"""pipeline/interactive_export.py _om_fetch_cached without network: a good
Open-Meteo answer is stored, a failed fetch falls back to it -- only for the
same points and only while it is fresh; otherwise the error stands."""
import gzip
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import interactive_export as ie  # noqa: E402
from data_connectors.open_meteo_client import PointForecast  # noqa: E402

fails = []


def check(n, c, d=""):
    print(f"  [{'PASS' if c else 'FAIL'}] {n}  {d}")
    if not c:
        fails.append(n)


def pf(la, lo, t=-2.0):
    return PointForecast(la, lo, 2000.0, ["2026-11-01T00:00"], [t], [0.0], [0.0], [3.0], [180.0], [0.0])


def boom():
    raise RuntimeError("Open-Meteo Rate-Limit")


def raises(fn):
    try:
        fn()
        return False
    except RuntimeError:
        return True


lats, lons = [46.8, 46.9], [9.8, 9.9]
with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "om" / "openmeteo.json.gz"
    os.environ["SNOWMAPPER_OM_CACHE"] = str(path)
    ok = ie._om_fetch_cached(lambda: [pf(46.8, 9.8, -3.0), pf(46.9, 9.9)], lats, lons)
    check("good answer passed through", len(ok) == 2 and ok[0].temperature_2m == [-3.0])
    check("good answer stored", path.exists())
    fb = ie._om_fetch_cached(boom, lats, lons)
    check("failed fetch -> stored answer", len(fb) == 2 and fb[0].temperature_2m == [-3.0] and fb[1].latitude == 46.9)
    check("other points -> error stands", raises(lambda: ie._om_fetch_cached(boom, [46.8, 40.0], lons)))
    check("other number of points -> error stands", raises(lambda: ie._om_fetch_cached(boom, [46.8], [9.8])))
    x = json.loads(gzip.decompress(path.read_bytes()))
    x["saved"] = time.time() - 60 * 3600
    path.write_bytes(gzip.compress(json.dumps(x).encode()))
    check("older than 48 h -> error stands", raises(lambda: ie._om_fetch_cached(boom, lats, lons)))
    path.write_bytes(b"not gzip")
    check("broken store -> the fetch's own error", raises(lambda: ie._om_fetch_cached(boom, lats, lons)))
    del os.environ["SNOWMAPPER_OM_CACHE"]
    check("no store configured -> error stands", raises(lambda: ie._om_fetch_cached(boom, lats, lons)))

print("\nFAILED: " + ", ".join(fails) if fails else "\nOM CACHE OK")
sys.exit(1 if fails else 0)
