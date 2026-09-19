"""Publish the Variant-A export so the app can consume it.

Two sinks (both optional, config via env):
  1. Static copy  -> VARIANT_A_PUBLISH_DIR  (e.g. a web-served folder / CDN mount)
  2. Supabase Storage -> if SUPABASE_URL + SUPABASE_SERVICE_KEY (+ VARIANT_A_BUCKET)
     are set. No-op (with a clear note) when credentials are absent, so the pipeline
     never fails just because it runs without deploy secrets.

This keeps the app loosely coupled: it reads manifest.json + layers + profiles.json
from whichever sink is configured.
"""
from __future__ import annotations
import os, shutil
from pathlib import Path
from . import config


def publish_static(src: Path | None = None, dst: str | None = None):
    src = Path(src or config.OUTPUT_DIR)
    dst = dst or os.environ.get("VARIANT_A_PUBLISH_DIR")
    if not dst:
        return None
    dstp = Path(dst); dstp.mkdir(parents=True, exist_ok=True)
    for item in ("manifest.json", "layers", "profiles", "preview.html"):
        s = src / item
        if not s.exists():
            continue
        d = dstp / item
        if s.is_dir():
            if d.exists():
                shutil.rmtree(d)
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)
    print(f"[publish] static copy -> {dstp}")
    return dstp


def publish_supabase(src: Path | None = None):
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")
    bucket = os.environ.get("VARIANT_A_BUCKET", "variant-a")
    if not (url and key):
        print("[publish] Supabase creds not set (SUPABASE_URL / SUPABASE_SERVICE_KEY) — skipping upload.")
        return False
    try:
        from supabase import create_client
    except Exception:
        print("[publish] `supabase` python client not installed — skipping upload "
              "(pip install supabase).")
        return False
    src = Path(src or config.OUTPUT_DIR)
    client = create_client(url, key)
    store = client.storage.from_(bucket)
    uploaded = 0
    for path in src.rglob("*"):
        if path.is_dir():
            continue
        rel = str(path.relative_to(src))
        try:
            store.upload(rel, str(path), {"upsert": "true"})
            uploaded += 1
        except Exception as e:
            print(f"[publish] upload {rel} failed: {e}")
    print(f"[publish] Supabase bucket '{bucket}': {uploaded} files uploaded.")
    return True


def publish(src: Path | None = None):
    publish_static(src)
    publish_supabase(src)
