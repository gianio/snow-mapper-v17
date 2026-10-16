#!/usr/bin/env python3
"""Convert a JSON data blob (snowdata-*.json.gz) into the binary SMB1
container the boot loader prefers (pipeline/interactive_export.py,
write_bin_blob). Used for the cached demo dataset.

    python tools/blob_to_bin.py in.json.gz out.bin.gz
"""
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.interactive_export import write_bin_blob  # noqa: E402


def main(src, dst):
    d = json.loads(gzip.decompress(Path(src).read_bytes()))
    write_bin_blob(Path(dst), d["meta"], d["b"])
    print(f"{src} -> {dst} ({Path(dst).stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main(*sys.argv[1:3])
