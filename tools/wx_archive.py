#!/usr/bin/env python3
"""The weather archive in the live cycle (pipeline/wx_archive.py).

    python tools/wx_archive.py pull    <archive> [--days 21]   # R2 -> local
    python tools/wx_archive.py add     <archive> <ogd_dir>     # file an OGD extract
    python tools/wx_archive.py push    <archive>               # local -> R2
    python tools/wx_archive.py summary <archive> [--days 14]   # markdown table
    python tools/wx_archive.py prune   <archive> --keep 45     # local copy only

R2 (or any S3-compatible store) through the aws CLI, like tools/publish_tiles.py:
env R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (or S3_ENDPOINT),
R2_ARCHIVE_BUCKET (default: R2_BUCKET, else snowmapper-tiles), objects under
wx-archive/. Without the settings pull/push say so and exit 0: the workflow
then keeps the archive in the Actions cache only.

push refuses when the pull of this run failed, so a partial day file never
overwrites a complete one in R2.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import wx_archive as wa  # noqa: E402

PREFIX = "wx-archive/ogd"
_PULLED_OK = ".pulled_ok"


def _s3():
    acc, key, sec = (os.environ.get(k, "") for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"))
    ep = os.environ.get("S3_ENDPOINT") or (f"https://{acc}.r2.cloudflarestorage.com" if acc else "")
    if not (ep and key and sec):
        return None
    bucket = os.environ.get("R2_ARCHIVE_BUCKET") or os.environ.get("R2_BUCKET") or "snowmapper-tiles"
    env = dict(os.environ, AWS_ACCESS_KEY_ID=key, AWS_SECRET_ACCESS_KEY=sec,
               AWS_DEFAULT_REGION=os.environ.get("AWS_DEFAULT_REGION", "auto"))
    return ep, bucket, env


def _aws(args, s3):
    ep, _, env = s3
    r = subprocess.run(["aws", "s3", *args, "--endpoint-url", ep, "--only-show-errors"],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr.strip()[-800:])
    return r.returncode == 0


def pull(archive: Path, days: int) -> int:
    s3 = _s3()
    (archive / _PULLED_OK).unlink(missing_ok=True)
    if not s3:
        print("archive: no R2 settings -- Actions cache only")
        return 0
    today = dt.datetime.utcnow().date()
    months = sorted({(today - dt.timedelta(days=k)).strftime("%Y/%m") for k in range(days + 1)})
    ok = True
    for m in months:
        ok &= _aws(["sync", f"s3://{s3[1]}/{PREFIX}/{m}/", str(archive / "ogd" / m) + "/"], s3)
    if ok:
        archive.mkdir(parents=True, exist_ok=True)
        (archive / _PULLED_OK).write_text(dt.datetime.utcnow().isoformat())
    print(f"archive: pulled {', '.join(months)} from R2" + ("" if ok else " -- WITH ERRORS (push will be skipped)"))
    return 0


def push(archive: Path) -> int:
    s3 = _s3()
    if not s3:
        print("archive: no R2 settings -- Actions cache only")
        return 0
    if not (archive / _PULLED_OK).exists():
        print("::warning::archive: the pull of this run failed -- not pushing (would overwrite complete days)")
        return 0
    ok = _aws(["sync", str(archive / "ogd") + "/", f"s3://{s3[1]}/{PREFIX}/", "--exclude", "*.tmp"], s3)
    print("archive: pushed to R2" if ok else "::warning::archive: push to R2 failed")
    return 0


def summary(archive: Path, days: int) -> int:
    rows = wa.coverage(archive, days)
    print("| Tag (UTC) | Punkte | Stunden |\n|---|---:|---:|")
    for day, pts, hrs in rows:
        print(f"| {day} | {pts} | {hrs} |")
    if not rows:
        print("| – | 0 | 0 |")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pull", "add", "push", "summary", "prune"])
    ap.add_argument("archive")
    ap.add_argument("ogd_dir", nargs="?")
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--keep", type=int, default=45)
    a = ap.parse_args()
    archive = Path(a.archive)
    if a.cmd == "pull":
        return pull(archive, a.days)
    if a.cmd == "push":
        return push(archive)
    if a.cmd == "summary":
        return summary(archive, a.days)
    if a.cmd == "prune":
        print(f"archive: {wa.prune(archive, a.keep)} local day files older than {a.keep} days removed")
        return 0
    if not a.ogd_dir:
        ap.error("add needs the OGD extract directory")
    n = wa.add_run(archive, Path(a.ogd_dir))
    print(f"archive: {n} point-hours filed from {a.ogd_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
