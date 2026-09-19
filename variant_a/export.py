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

from . import config, classify
from .subregions import NationalGrid, wgs84_bounds

DENS_MIN, DENS_MAX = 100.0, 450.0


def _rgba_from_labels(lab, table):
    im = np.zeros((*lab.shape, 4), np.uint8)
    for k, c in table.items():
        im[lab == k] = c
    return im


def _rgba_density(field, valid):
    x = np.clip((field - DENS_MIN) / (DENS_MAX - DENS_MIN), 0, 1)
    im = np.zeros((*field.shape, 4), np.uint8)
    im[..., 0] = np.clip(255 * np.minimum(1, 2 * x), 0, 255)
    im[..., 1] = np.clip(255 * np.minimum(1, 2 * (1 - x)), 0, 255)
    im[..., 2] = np.clip(120 * (1 - x), 0, 255)
    im[..., 3] = np.where(valid & (field > 0), 190, 0)
    return im


def _save_png(im, path):
    Image.fromarray(im, "RGBA").save(path, "PNG", optimize=True)


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
    la0, lo0, la1, lo1 = wgs84_bounds(grid)
    bounds = [[la0, lo0], [la1, lo1]]
    valid = ~np.isnan(grid.elevation)
    ts_list = sorted(grids_by_ts)
    tags = [dt.strftime("%Y-%m-%dT%H%M") for dt in ts_list]
    for dt, tag in zip(ts_list, tags):
        g = grids_by_ts[dt]
        _save_png(_rgba_from_labels(g["ski18"], classify.SKI_RGBA), out_dir / "layers" / f"ski18_{tag}.png")
        _save_png(_rgba_from_labels(g["simple"], classify.SIMPLE_RGBA), out_dir / "layers" / f"simple_{tag}.png")
        _save_png(_rgba_density(g["density"], valid), out_dir / "layers" / f"density_{tag}.png")
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
