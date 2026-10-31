"""The app's JavaScript libraries, served from the site itself.

Loading Leaflet & Co. from public CDNs sends every visitor's IP address to
those hosts (a GDPR concern) and, in the iOS app, counts as code loaded from
the network (App Store guideline 2.5.2). So the build takes the exact npm
releases, checks them against the integrity hashes pinned here, copies the
few files the app uses to <out>/vendor/ and points index.html and app.js at
them. Without network and without a cached tarball the CDN addresses stay --
the build never fails over this. The licence texts travel along
(vendor/<lib>/LICENSE.txt); the app links them under «Datenquellen & Lizenzen».
"""
from __future__ import annotations

import base64
import hashlib
import io
import tarfile
from pathlib import Path

LIBS = [
    {"name": "leaflet", "ver": "1.9.4",
     "tgz": "https://registry.npmjs.org/leaflet/-/leaflet-1.9.4.tgz",
     "sri": "sha512-nxS1ynzJOmOlHp+iL3FyWqK89GtNL8U8rvlMOsQdTTssxZwCXh8N2NB3GDQOL+YR3XnWyZAxwQixURb+FA74PA==",
     "files": {"package/dist/leaflet.js": "leaflet/leaflet.js",
               "package/dist/leaflet.css": "leaflet/leaflet.css",
               "package/dist/images/layers.png": "leaflet/images/layers.png",
               "package/dist/images/layers-2x.png": "leaflet/images/layers-2x.png",
               "package/dist/images/marker-icon.png": "leaflet/images/marker-icon.png",
               "package/dist/images/marker-icon-2x.png": "leaflet/images/marker-icon-2x.png",
               "package/dist/images/marker-shadow.png": "leaflet/images/marker-shadow.png",
               "package/LICENSE": "leaflet/LICENSE.txt"},
     "rewrite": [("https://unpkg.com/leaflet@1.9.4/dist/", "vendor/leaflet/")]},
    {"name": "supabase-js", "ver": "2.118.0",
     "tgz": "https://registry.npmjs.org/@supabase/supabase-js/-/supabase-js-2.118.0.tgz",
     "sri": "sha512-FxI8UXMVC2UG7IkeiI8fuQZqq2oI3JeSuN8MPIC2RLVaoEAxKS0nqE6kii9Ewpq8f9gNPM6git1Y8f0vN/QLFw==",
     "files": {"package/dist/umd/supabase.js": "supabase/supabase.js",
               "package/LICENSE": "supabase/LICENSE.txt"},
     "rewrite": [("https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.118.0/dist/umd/supabase.min.js",
                  "vendor/supabase/supabase.js")]},
    {"name": "maplibre-gl", "ver": "4.1.2",
     "tgz": "https://registry.npmjs.org/maplibre-gl/-/maplibre-gl-4.1.2.tgz",
     "sri": "sha512-98T+3BesL4w/N39q/rgs9q6HzHLG6pgbS9UaTqg6fMISfzy2WGKokjK205ENFDDmEljj54/LTfdXgqg2XfYU4A==",
     "files": {"package/dist/maplibre-gl.js": "maplibre/maplibre-gl.js",
               "package/dist/maplibre-gl.css": "maplibre/maplibre-gl.css",
               "package/LICENSE.txt": "maplibre/LICENSE.txt"},
     "rewrite": [("https://unpkg.com/maplibre-gl@4.1.2/dist/", "vendor/maplibre/")]},
    {"name": "pmtiles", "ver": "4.5.0",
     "tgz": "https://registry.npmjs.org/pmtiles/-/pmtiles-4.5.0.tgz",
     "sri": "sha512-CBeD4SoUluFziGdy/8k7FOjQxQy486n+929W/tWophauvMICpkZ26vGBWhDt/6b1FZQWNaf4jFdT/5+q0Wii5w==",
     "files": {"package/dist/pmtiles.js": "pmtiles/pmtiles.js"},
     "rewrite": [("https://unpkg.com/pmtiles@4.5.0/dist/pmtiles.js", "vendor/pmtiles/pmtiles.js")]},
]


def _sri_ok(data: bytes, sri: str) -> bool:
    algo, _, want = sri.partition("-")
    return base64.b64encode(hashlib.new(algo, data).digest()).decode() == want


def _tarball(lib, cache_dir: Path):
    """The checked tarball bytes, from the cache or the registry; None if neither."""
    f = cache_dir / f"{lib['name']}-{lib['ver']}.tgz"
    if f.exists():
        data = f.read_bytes()
        if _sri_ok(data, lib["sri"]):
            return data
        f.unlink()
    try:
        import requests
        r = requests.get(lib["tgz"], timeout=60)
        r.raise_for_status()
        data = r.content
    except Exception as e:                      # noqa: BLE001
        print(f"[VENDOR] {lib['name']}: not reachable ({type(e).__name__}) -- CDN stays")
        return None
    if not _sri_ok(data, lib["sri"]):
        print(f"[VENDOR] {lib['name']}: integrity check FAILED -- CDN stays")
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return data


def vendorize(out_dir: Path, cache_dir: Path) -> list:
    """Copy the libraries into out_dir/vendor and point the shell at them.
    Returns the names of the libraries now served from the site."""
    done = []
    for lib in LIBS:
        data = _tarball(lib, cache_dir)
        if data is None:
            continue
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            names = set(tf.getnames())
            if not all(src in names for src in lib["files"]):
                print(f"[VENDOR] {lib['name']}: files missing in the tarball -- CDN stays")
                continue
            for src, dst in lib["files"].items():
                target = out_dir / "vendor" / dst
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tf.extractfile(src).read())
        for fname in ("index.html", "app.js"):
            p = out_dir / fname
            if p.exists():
                t = p.read_text(encoding="utf-8")
                for old, new in lib["rewrite"]:
                    t = t.replace(old, new)
                p.write_text(t, encoding="utf-8")
        done.append(lib["name"])
    # nothing left to warm up a connection to
    p = out_dir / "index.html"
    if p.exists():
        t = p.read_text(encoding="utf-8")
        if "https://unpkg.com/" not in t and "https://cdn.jsdelivr.net/" not in t:
            t = (t.replace('<link rel="preconnect" href="https://unpkg.com" crossorigin>\n', "")
                  .replace('<link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>\n', ""))
            p.write_text(t, encoding="utf-8")
    print(f"[VENDOR] served from the site: {', '.join(done) if done else 'none (CDN)'}")
    return done
