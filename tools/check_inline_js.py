#!/usr/bin/env python3
"""Extract inline <script> blocks from a generated HTML file and syntax-check
each with ``node --check``.

The whole app ships as one ~3k-line inline script inside ``index.html``; a
single stray typo there breaks the deployed page with no build step to catch
it. This runs in CI (see ``.github/workflows/deploy.yml``) right after the site
is generated, so a syntax error fails the build instead of the users' phones.

Usage:
    python tools/check_inline_js.py index.html
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Inline scripts only: <script> with no src= attribute. Non-greedy body.
SCRIPT_RE = re.compile(
    r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE
)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python tools/check_inline_js.py <html-file>", file=sys.stderr)
        return 2
    html = Path(argv[1]).read_text(encoding="utf-8")
    blocks = [b for b in SCRIPT_RE.findall(html) if b.strip()]
    if not blocks:
        print("No inline <script> blocks found — nothing to check.", file=sys.stderr)
        return 1

    failures = 0
    for i, body in enumerate(blocks):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(body)
            tmp = fh.name
        res = subprocess.run(
            ["node", "--check", tmp], capture_output=True, text=True
        )
        Path(tmp).unlink(missing_ok=True)
        if res.returncode != 0:
            failures += 1
            print(f"[FAIL] inline script #{i + 1} ({len(body)} chars):")
            print(res.stderr.strip())
        else:
            print(f"[ok]   inline script #{i + 1} ({len(body)} chars)")

    if failures:
        print(f"\n{failures} inline script(s) failed node --check.", file=sys.stderr)
        return 1
    print(f"\nAll {len(blocks)} inline script(s) passed node --check.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
