"""Export Variant-A results in an app-portable form (loose coupling).

Writes, under outputs/variant_a/:
  layers/<layer>_<ts>.png    georeferenced RGBA overlays (ski18 / simple / density)
  profiles/profiles.json     per-point snow profiles for the clickable viewer
  manifest.json              bounds, timestamps, layer list, legends

The app frontend indexes manifest.json and places the PNG overlays on its Leaflet
basemap using the WGS84 bounds; the profile viewer consumes profiles.json.
"""
from __future__ import annotations
import io, json, base64
from pathlib import Path
import numpy as np
from PIL import Image

from rasterio.transform import from_origin, array_bounds
from rasterio.warp import calculate_default_transform, reproject, Resampling

from . import config, classify
from .subregions import NationalGrid, wgs84_bounds

DENS_MIN, DENS_MAX = 100.0, 450.0

# Width the reprojected PNG is capped at. The source grid is 1440 px across,
# and EPSG:4326 comes out near that, so this does not downsample in practice.
_PNG_W = 2000


def _rgba_from_labels(lab, table):
    im = np.zeros((*lab.shape, 4), np.uint8)
    for k, c in table.items():
        im[lab == k] = c
    return im


def _rgba_density(field, valid):
    field = np.nan_to_num(field, nan=0.0)
    x = np.clip((field - DENS_MIN) / (DENS_MAX - DENS_MIN), 0, 1)
    im = np.zeros((*field.shape, 4), np.uint8)
    im[..., 0] = np.clip(255 * np.minimum(1, 2 * x), 0, 255)
    im[..., 1] = np.clip(255 * np.minimum(1, 2 * (1 - x)), 0, 255)
    im[..., 2] = np.clip(120 * (1 - x), 0, 255)
    im[..., 3] = np.where(valid & (field > 0), 190, 0)
    return im


def _to_wgs84(rgba, grid: NationalGrid, nearest: bool):
    """Reproject an LV03 raster to EPSG:4326 and return it with its real bounds.

    This has to happen before the PNG is written. The grid is regular in LV03
    (EPSG:21781), and L.imageOverlay stretches an image LINEARLY between two
    lat/lon corners -- but an LV03 rectangle is not a lat/lon rectangle. Taking
    the bbox of two opposite corners and stretching to it displaced the layer
    by up to 10 km: the true SE corner of this grid lands 10.0 km from where
    that bbox puts it, the NW corner 5.8 km. The app's own rasters go through
    exactly this step (_rgba_to_png_b64 in pipeline/interactive_export.py);
    Variant A was the one product that skipped it.

    `nearest` for the two CLASS layers -- ski18 and simple are category codes,
    and interpolating them would invent classes that are not in the data.
    """
    h, w = rgba.shape[:2]
    src_crs = f"EPSG:{config.DEM_EPSG}"
    src_t = from_origin(grid.xll, grid.yll + h * grid.cs, grid.cs, grid.cs)
    bounds = (grid.xll, grid.yll, grid.xll + w * grid.cs, grid.yll + h * grid.cs)
    dst_t, dw, dh = calculate_default_transform(src_crs, "EPSG:4326", w, h, *bounds)
    scale = max(1.0, dw / _PNG_W)
    dw2, dh2 = int(dw / scale), int(dh / scale)
    dst_t2 = from_origin(dst_t.c, dst_t.f, (dst_t.a * dw) / dw2, (-dst_t.e * dh) / dh2)
    rs = Resampling.nearest if nearest else Resampling.bilinear
    bands = []
    for k in range(4):
        out = np.zeros((dh2, dw2), "float32")
        reproject(source=rgba[:, :, k].astype("float32"), destination=out,
                  src_transform=src_t, src_crs=src_crs,
                  dst_transform=dst_t2, dst_crs="EPSG:4326", resampling=rs)
        bands.append(out)
    left, bottom, right, top = array_bounds(dh2, dw2, dst_t2)
    return (np.clip(np.dstack(bands), 0, 255).astype(np.uint8),
            [[bottom, left], [top, right]])


class _WGS84Map:
    """The LV03 -> EPSG:4326 pixel mapping, computed ONCE and reused per frame.

    _to_wgs84 runs rasterio's reproject from scratch for every layer of every
    frame -- 132 times for a 44-frame export, ~9 s a frame, 400 s in total --
    although the mapping is identical each time. This builds the same
    destination grid (so bounds and shape are unchanged), transforms its pixel
    centres back to LV03 once, and then each frame is just array indexing.
    """
    def __init__(self, grid: NationalGrid):
        from pyproj import Transformer
        h, w = grid.nr, grid.nc
        src_crs = f"EPSG:{config.DEM_EPSG}"
        bounds = (grid.xll, grid.yll, grid.xll + w * grid.cs, grid.yll + h * grid.cs)
        dst_t, dw, dh = calculate_default_transform(src_crs, "EPSG:4326", w, h, *bounds)
        scale = max(1.0, dw / _PNG_W)
        dw2, dh2 = int(dw / scale), int(dh / scale)
        t = from_origin(dst_t.c, dst_t.f, (dst_t.a * dw) / dw2, (-dst_t.e * dh) / dh2)
        left, bottom, right, top = array_bounds(dh2, dw2, t)
        self.bounds = [[bottom, left], [top, right]]
        self.shape = (dh2, dw2)
        lon = t.c + (np.arange(dw2) + 0.5) * t.a
        lat = t.f + (np.arange(dh2) + 0.5) * t.e
        LON, LAT = np.meshgrid(lon, lat)
        E, N = Transformer.from_crs(4326, config.DEM_EPSG, always_xy=True).transform(LON, LAT)
        fc = (E - grid.xll) / grid.cs - 0.5
        fr = (grid.yll + h * grid.cs - N) / grid.cs - 0.5
        ni, nj = np.rint(fr).astype(np.int64), np.rint(fc).astype(np.int64)
        self._near_ok = (ni >= 0) & (ni < h) & (nj >= 0) & (nj < w)
        self._ni, self._nj = ni[self._near_ok], nj[self._near_ok]
        r0, c0 = np.floor(fr).astype(np.int64), np.floor(fc).astype(np.int64)
        ok = (r0 >= 0) & (r0 < h - 1) & (c0 >= 0) & (c0 < w - 1)
        self._bil_ok = ok
        self._r0, self._c0 = r0[ok], c0[ok]
        tr, tc = (fr - np.floor(fr))[ok], (fc - np.floor(fc))[ok]
        self._w = [((1 - tr) * (1 - tc))[:, None], ((1 - tr) * tc)[:, None],
                   (tr * (1 - tc))[:, None], (tr * tc)[:, None]]

    def nearest(self, rgba):
        out = np.zeros(self.shape + (4,), np.uint8)
        out[self._near_ok] = rgba[self._ni, self._nj]
        return out

    def bilinear(self, rgba):
        a = rgba.astype(np.float32)
        r, c = self._r0, self._c0
        v = (a[r, c] * self._w[0] + a[r, c + 1] * self._w[1]
             + a[r + 1, c] * self._w[2] + a[r + 1, c + 1] * self._w[3])
        out = np.zeros(self.shape + (4,), np.uint8)
        out[self._bil_ok] = np.clip(np.rint(v), 0, 255).astype(np.uint8)
        return out


def _save_png(im, path):
    """Write RGBA, but as an INDEXED PNG when it has few enough colours.

    The class layers are nine colours plus transparency, yet as RGBA they cost
    176 kB a frame -- the size is the speckle of a 250 m classification, not
    the palette. Indexing drops that to ~102 kB, and with the export now
    spanning the app's whole timeline instead of 27% of it there are 45 frames
    per layer rather than 7, so 42% a frame is worth having.

    8-bit, not 4: PNG's filters work on byte-aligned rows, and 4-bit actually
    came out LARGER (121 kB) than 8-bit (102 kB) on real frames.
    """
    # Pack each RGBA pixel into one uint32 and unique THAT. np.unique(axis=0)
    # sorts the 1.1 M rows lexicographically and took 2.2 s per image --
    # 132 images, ~290 s of a 400 s export. On a flat uint32 view it is a
    # plain 1-D sort, and viewing the result back as bytes recovers the RGBA.
    packed = np.ascontiguousarray(im, dtype=np.uint8).view(np.uint32).reshape(-1)
    uniq_p, inv = np.unique(packed, return_inverse=True)
    uniq = uniq_p.view(np.uint8).reshape(-1, 4)
    if len(uniq) > 256:
        Image.fromarray(im, "RGBA").save(path, "PNG", optimize=True)
        return
    idx = Image.fromarray(inv.reshape(im.shape[:2]).astype(np.uint8), "P")
    pal, alpha = [], []
    for c in uniq:
        pal += [int(c[0]), int(c[1]), int(c[2])]
        alpha.append(int(c[3]))
    idx.putpalette(pal + [0] * (768 - len(pal)))
    # tRNS as a PER-INDEX alpha array, not a single transparent index. The
    # class palettes use partial alpha throughout -- SKI_RGBA alone spans 150
    # to 240 -- so naming one index transparent would force every other class
    # to fully opaque and change how the layer composites over the map. The
    # round-trip test asserts this is byte-exact.
    idx.save(path, "PNG", optimize=True, transparency=bytes(alpha))


def _jsonable(o):
    """Coerce numpy scalars/arrays that leaked into the payload.

    Defence in depth for the export boundary. A single np.int64 anywhere in
    the payload makes json.dump raise, and it raises at the END of the
    pipeline -- after the forcing, after the model, after the PNGs. Losing a
    40-minute national run to a type that prints as "2" is not a reasonable
    failure mode, so numpy scalars are converted rather than fatal.
    """
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"{type(o).__name__} is not JSON serializable")


def export_all(grid: NationalGrid, grids_by_ts, profile_payload, out_dir: Path | None = None):
    out_dir = out_dir or config.OUTPUT_DIR
    (out_dir / "layers").mkdir(parents=True, exist_ok=True)
    (out_dir / "profiles").mkdir(parents=True, exist_ok=True)
    valid = ~np.isnan(grid.elevation)
    ts_list = sorted(grids_by_ts)
    tags = [dt.strftime("%Y-%m-%dT%H%M") for dt in ts_list]
    # Every layer shares the grid, so the reprojected bounds are identical for
    # all of them; the last one out is the one the manifest publishes.
    bounds = None
    for dt, tag in zip(ts_list, tags):
        g = grids_by_ts[dt]
        for key, rgba, nearest in (
            ("ski18", _rgba_from_labels(g["ski18"], classify.SKI_RGBA), True),
            ("simple", _rgba_from_labels(g["simple"], classify.SIMPLE_RGBA), True),
            ("density", _rgba_density(g["density"], valid), False),
        ):
            img, bounds = _to_wgs84(rgba, grid, nearest)
            _save_png(img, out_dir / "layers" / f"{key}_{tag}.png")
    if bounds is None:                      # no timestamps: fall back to the bbox
        la0, lo0, la1, lo1 = wgs84_bounds(grid)
        bounds = [[la0, lo0], [la1, lo1]]
    json.dump(profile_payload, open(out_dir / "profiles" / "profiles.json", "w"),
              default=_jsonable)
    manifest = {
        "product": "variant_a_ski_quality",
        "crs": "EPSG:4326",
        "bounds": bounds,
        "timestamps": [dt.strftime("%Y-%m-%dT%H:%M") for dt in ts_list],
        "tags": tags,
        "layers": {
            "ski18": {"file": "layers/ski18_{tag}.png",
                      "legend": {i: [classify.SKI_LABELS[i], list(classify.SKI_RGBA[i][:3])]
                                 for i in range(1, len(classify.SKI_LABELS))}},
            "simple": {"file": "layers/simple_{tag}.png",
                       "legend": {i: [classify.SIMPLE_LABELS[i], list(classify.SIMPLE_RGBA[i][:3])]
                                  for i in range(1, len(classify.SIMPLE_LABELS))}},
            "density": {"file": "layers/density_{tag}.png",
                        "range": [DENS_MIN, DENS_MAX], "unit": "kg/m3"},
        },
        "profiles": "profiles/profiles.json",
        # The profiles ride a COARSER axis than the layers: the raster is what
        # gets scrubbed, a profile is a point read, and profiles.json costs
        # ~300 kB per step against ~100 kB for a frame. The client snaps to
        # the nearest entry here rather than reusing the layer index.
        "profile_timestamps": list(profile_payload.get("labels") or []),
        "subregions": grid.tile_names,
    }
    json.dump(manifest, open(out_dir / "manifest.json", "w"), indent=2,
              default=_jsonable)
    return out_dir, manifest


def preview_html(out_dir: Path, manifest, grid: NationalGrid):
    """Self-contained interactive viewer = the app-facing map: layer toggle + time
    slider + click-anywhere snow-profile (client-side KNN over the profile points)."""
    la = manifest["bounds"]
    tags = manifest["tags"]
    def b64(path):
        return base64.b64encode(open(path, "rb").read()).decode()
    frames = {lyr: [b64(out_dir / "layers" / f"{lyr}_{t}.png") for t in tags]
              for lyr in ("ski18", "simple", "density")}
    leg18 = "".join(f'<div><span style="background:rgb({",".join(map(str,v[1]))})"></span>{v[0]}</div>'
                    for v in manifest["layers"]["ski18"]["legend"].values())
    legS = "".join(f'<div><span style="background:rgb({",".join(map(str,v[1]))})"></span>{v[0]}</div>'
                   for v in manifest["layers"]["simple"]["legend"].values())
    prof = json.load(open(out_dir / "profiles" / "profiles.json"))
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Variant A — Ski-Qualität</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{{height:100%;margin:0}}.lg{{position:absolute;bottom:40px;left:6px;z-index:1000;background:rgba(255,255,255,.92);padding:6px 8px;border-radius:5px;font:10px sans-serif;max-height:55%;overflow:auto}}.lg span{{display:inline-block;width:12px;height:9px;margin-right:4px;border:1px solid #999}}
#c{{position:absolute;bottom:0;left:0;right:0;height:34px;background:#1c1c1c;color:#eee;z-index:1000;display:flex;gap:10px;align-items:center;padding:0 10px;font:12px sans-serif}}#sl{{flex:1}}</style></head><body>
<div id="map"></div><div class="lg" id="lg"></div>
<div id="c"><select id="ly"><option value="ski18">Ski (18)</option><option value="simple">Vereinfacht</option><option value="density">Dichte</option></select>
<span id="ts"></span><input type="range" id="sl" min="0" max="{max(len(tags)-1,0)}" value="0"><span style="font-size:10px">Klick = Schneeprofil</span></div>
<script>
var F={json.dumps(frames)},TAGS={json.dumps(tags)},B={json.dumps(la)},PROF={json.dumps(prof)};
var GR=PROF.grain,PTS=PROF.points,NB=PROF.nb;
var LEG={{"ski18":`{leg18}`,"simple":`{legS}`,"density":"Dichte 100–450 kg/m³ (blau→rot)"}};
var map=L.map('map').setView([46.8,8.3],8);
L.tileLayer('https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe-winter/default/current/3857/{{z}}/{{x}}/{{y}}.jpeg',{{maxZoom:17}}).addTo(map);
var ov=L.imageOverlay('data:image/png;base64,'+F['ski18'][0],B,{{opacity:.8}}).addTo(map);
var CUR=0,POP=null,CLL=null;
function knn(lat,lon,k){{var d=PTS.map(function(p,i){{var dy=(p.lat-lat)*111,dx=(p.lon-lon)*78;return [dx*dx+dy*dy,i];}});
 d.sort(function(a,b){{return a[0]-b[0];}});return d.slice(0,k);}}
function interp(lat,lon){{var nn=knn(lat,lon,4),ws=0,hs=0,db=new Array(NB).fill(0),step=PROF.profiles[Math.min(CUR,PROF.profiles.length-1)];
 nn.forEach(function(x){{var w=1/(x[0]+0.5);ws+=w;var pr=step[x[1]];hs+=w*pr.hs;for(var b=0;b<NB;b++)db[b]+=w*pr.db[b];}});
 for(var b=0;b<NB;b++)db[b]/=ws; hs/=ws; var gb=step[nn[0][1]].gb; return {{hs:Math.round(hs),db:db,gb:gb}};}}
function svg(pf){{if(!pf||pf.hs<1)return '<i>kein/kaum Schnee</i>';var H=150,W=130,s='<svg width="'+(W+90)+'" height="'+(H+30)+'">';
 for(var i=0;i<NB;i++){{var y0=i/NB*H,y1=(i+1)/NB*H,gc=GR[pf.gb[i]]||GR[0];s+='<rect x="0" y="'+y0.toFixed(1)+'" width="20" height="'+(y1-y0+0.5).toFixed(1)+'" fill="rgb('+gc[1].join(',')+')"/>';}}
 var pts='';for(var i=0;i<NB;i++){{var x=26+Math.max(0,Math.min(1,(pf.db[i]-100)/350))*W,y=(i+0.5)/NB*H;pts+=x.toFixed(1)+','+y.toFixed(1)+' ';}}
 s+='<polyline points="'+pts+'" fill="none" stroke="#036" stroke-width="2"/><line x1="26" y1="0" x2="26" y2="'+H+'" stroke="#999"/>';
 s+='<text x="26" y="'+(H+14)+'" font-size="9">100</text><text x="'+(26+W-20)+'" y="'+(H+14)+'" font-size="9">450 kg/m³</text></svg>';
 var g='<div style="margin-top:3px;font-size:9px">';for(var k=1;k<=9;k++){{if(!GR[k])continue;g+='<span style="display:inline-block;width:9px;height:8px;margin:0 2px;background:rgb('+GR[k][1].join(',')+')"></span>'+GR[k][0];}}
 return '<b>HS '+pf.hs+' cm · interpoliert</b>'+s+g+'</div>';}}
map.on('click',function(e){{CLL=e.latlng;if(!POP)POP=L.popup({{maxWidth:290,autoClose:false,closeOnClick:false}});
 POP.setLatLng(e.latlng).setContent('<b>'+TAGS[CUR]+'</b>'+svg(interp(e.latlng.lat,e.latlng.lng))).openOn(map);}});
function draw(){{var ly=document.getElementById('ly').value;CUR=+document.getElementById('sl').value;
 ov.setUrl('data:image/png;base64,'+F[ly][CUR]);document.getElementById('ts').textContent=TAGS[CUR];document.getElementById('lg').innerHTML=LEG[ly];
 if(CLL&&POP&&POP.isOpen())POP.setContent('<b>'+TAGS[CUR]+'</b>'+svg(interp(CLL.lat,CLL.lng)));}}
document.getElementById('ly').onchange=draw;document.getElementById('sl').oninput=draw;draw();
</script></body></html>"""
    (out_dir / "preview.html").write_text(html)
    return out_dir / "preview.html"


def export_matrix(grid: NationalGrid, frames, wps, runs, results, prof_ts,
                  forcing_models=None, out_dir: Path | None = None):
    """Export the weather-point matrix: streamed layer frames + per-point profiles.

    `frames` is consumed lazily -- each frame is reprojected and written before
    the next is made, so the national grids are never all in memory at once.

    Profiles go to one file per weather point instead of one profiles.json:
    14k runs x 22 steps x 57 numbers in a single file is far too much to fetch
    for one tap on the map. The client reads profiles/index.json (small) up
    front and loads only the nearest weather point's file on a tap.
    """
    from collections import Counter, defaultdict
    from . import matrix, profiles
    out_dir = out_dir or config.OUTPUT_DIR
    (out_dir / "layers").mkdir(parents=True, exist_ok=True)
    (out_dir / "profiles").mkdir(parents=True, exist_ok=True)
    valid = ~np.isnan(grid.elevation)
    tags, stamps = [], []
    proj = _WGS84Map(grid)
    bounds = proj.bounds
    for dt, g in frames:
        tag = dt.strftime("%Y-%m-%dT%H%M")
        for key, rgba, nearest in (
            ("ski18", _rgba_from_labels(g["ski18"], classify.SKI_RGBA), True),
            ("simple", _rgba_from_labels(g["simple"], classify.SIMPLE_RGBA), True),
            ("density", _rgba_density(g["density"], valid), False),
        ):
            img = proj.nearest(rgba) if nearest else proj.bilinear(rgba)
            _save_png(img, out_dir / "layers" / f"{key}_{tag}.png")
        tags.append(tag)
        stamps.append(dt.strftime("%Y-%m-%dT%H:%M"))
    if bounds is None:
        la0, lo0, la1, lo1 = wgs84_bounds(grid)
        bounds = [[la0, lo0], [la1, lo1]]

    forcing_models = forcing_models or {}
    by_wp = defaultdict(list)
    for r in runs:
        if r["id"] in results:
            by_wp[r["wp"]].append(r)
    wmeta = []
    for w in wps:
        rs = by_wp.get(w["id"])
        if not rs:
            continue
        payload = {
            # [elevation m, slope deg, aspect deg] -- aspect is meaningless on
            # the flat run (slope 0) and the client ignores it there.
            "runs": [[int(r["elev"]), int(r["slope"]), int(r["aspect"])] for r in rs],
            "hs": [results[r["id"]][1].tolist() for r in rs],
            "db": [results[r["id"]][2].tolist() for r in rs],
            "gb": [results[r["id"]][3].tolist() for r in rs],
        }
        with open(out_dir / "profiles" / f"{w['id']}.json", "w") as f:
            json.dump(payload, f, separators=(",", ":"), default=_jsonable)
        wmeta.append({"id": w["id"], "lat": w["lat"], "lon": w["lon"],
                      "ref_elev": w["ref_elev"], "bands": [int(b) for b in w["bands"]],
                      "model": forcing_models.get(w["id"])})
    index = {
        "nb": profiles.NB,
        "grain": {k: [v[0], list(v[1])] for k, v in profiles.GRAIN.items()},
        "labels": [dt.strftime("%Y-%m-%dT%H:%M") for dt in prof_ts],
        "top_cm": config.PROFILE_TOP_CM,
        "slopes": list(matrix.slope_nodes()),
        "aspects": config.MATRIX_ASPECTS,
        "weather_points": wmeta,
    }
    with open(out_dir / "profiles" / "index.json", "w") as f:
        json.dump(index, f, separators=(",", ":"), default=_jsonable)

    manifest = {
        "product": "variant_a_ski_quality",
        "mode": "matrix",
        "crs": "EPSG:4326",
        "bounds": bounds,
        "timestamps": stamps,
        "tags": tags,
        "layers": {
            "ski18": {"file": "layers/ski18_{tag}.png",
                      "legend": {i: [classify.SKI_LABELS[i], list(classify.SKI_RGBA[i][:3])]
                                 for i in range(1, len(classify.SKI_LABELS))}},
            "simple": {"file": "layers/simple_{tag}.png",
                       "legend": {i: [classify.SIMPLE_LABELS[i], list(classify.SIMPLE_RGBA[i][:3])]
                                  for i in range(1, len(classify.SIMPLE_LABELS))}},
            "density": {"file": "layers/density_{tag}.png",
                        "range": [DENS_MIN, DENS_MAX], "unit": "kg/m3"},
        },
        "profiles": "profiles/index.json",
        "profile_mode": "matrix",
        "profile_timestamps": index["labels"],
        "spinup_days": config.SPINUP_DAYS,
        "weather_points": len(wmeta),
        "runs": sum(len(v) for v in by_wp.values()),
        "forcing_models": dict(Counter(v for v in forcing_models.values() if v)),
        "subregions": grid.tile_names,
    }
    with open(out_dir / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2, default=_jsonable)
    return out_dir, manifest
