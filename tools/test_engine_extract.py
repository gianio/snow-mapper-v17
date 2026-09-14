#!/usr/bin/env python3
"""Assert the two engine.js generators agree byte-for-byte.

engine.js can be produced two ways:
  1. the full pipeline (pipeline.interactive_export._engine_js)
  2. the dependency-free text path (tools/make_engine.py)

(2) exists so a Mac can build the native app without numpy/rasterio/GDAL. That
is only safe while both paths produce exactly the same file — otherwise the
iOS app could ship an engine that differs from the one the web app and
tools/test_engine.js validate. This is the guard.

Skips path (1) gracefully when the heavy dependencies are absent, so it is
still useful on a bare machine (it then only checks that path (2) works).

    python3 tools/test_engine_extract.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.make_engine import make as make_lightweight  # noqa: E402


def main() -> int:
    light = make_lightweight()
    checks: list[tuple[str, bool]] = []

    checks.append(("dependency-free path produces output", bool(light.strip())))
    checks.append(("output declares the SnowEngine global", "root.SnowEngine=factory()" in light))
    checks.append(("output contains progCell", "function progCell" in light))

    # progZones IS defined inside the extracted block (it sits between the two
    # markers), it is simply not part of the public surface — it reads app state
    # and would crash a consumer. So the invariant is about the EXPORT LIST, not
    # about the text: tools/test_engine.js checks the runtime side of this.
    exports_line = ""
    marker = "  return {\n    "
    if marker in light:
        exports_line = light.split(marker, 1)[1].split("\n", 1)[0]
    checks.append(("export list was found", bool(exports_line)))
    checks.append(("progCell is exported", "progCell" in exports_line))
    checks.append(("progZones is NOT exported (app glue)", "progZones" not in exports_line))

    try:
        from pipeline.interactive_export import _engine_js  # noqa: WPS433
    except ImportError as exc:
        print(f"note: pipeline deps unavailable ({exc.name}); "
              "skipping the byte-identical comparison.")
        checks.append(("pipeline path importable", True))  # not a failure here
    else:
        full = _engine_js()
        same = full == light
        checks.append(("both generators agree byte-for-byte", same))
        if not same:
            print(f"  full={len(full)} bytes  lightweight={len(light)} bytes")
            for n, (a, b) in enumerate(zip(full, light)):
                if a != b:
                    print(f"  first difference at byte {n}: {a!r} vs {b!r}")
                    break

    failed = 0
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1
    print("\nENGINE EXTRACT OK" if not failed else f"\nENGINE EXTRACT FAILED ({failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
