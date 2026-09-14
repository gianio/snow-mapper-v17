#!/usr/bin/env python3
"""Emit engine.js WITHOUT installing the pipeline's dependencies.

The full pipeline needs numpy, rasterio, pyproj, scipy and GDAL. Installing all
of that on a Mac just to produce a 10 KB JavaScript file would make the native
iOS setup needlessly painful, so this reads pipeline/interactive_export.py as
plain text and reuses the same wrapper from pipeline/engine_extract.py. Stdlib
only — it runs on a stock macOS python3.

    python3 tools/make_engine.py                      # -> stdout
    python3 tools/make_engine.py -o path/engine.js    # -> file

tools/test_engine_extract.py asserts this produces byte-identical output to the
full pipeline path, so the two cannot drift.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline.engine_extract import app_js_from_source, build_engine_js  # noqa: E402

SOURCE = REPO / "pipeline" / "interactive_export.py"


def make() -> str:
    if not SOURCE.exists():
        raise SystemExit(f"error: {SOURCE} not found")
    return build_engine_js(app_js_from_source(SOURCE.read_text(encoding="utf-8")))


def main() -> int:
    ap = argparse.ArgumentParser(description="Emit the standalone scoring engine (engine.js)")
    ap.add_argument("-o", "--out", type=Path, help="write here instead of stdout")
    args = ap.parse_args()
    js = make()
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(js, encoding="utf-8")
        print(f"wrote {args.out} ({len(js)} bytes)", file=sys.stderr)
    else:
        sys.stdout.write(js)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
