# SNOWPACK tile service (Cloudflare R2 + Worker)

The phone no longer renders the SNOWPACK map itself. It loads ordinary map
tiles:

| Zoom | Source | Made by |
|---|---|---|
| 5–9 | one PMTiles archive per view and frame | pipeline (`tools/make_overview_pmtiles.py`), uploaded by `tools/publish_tiles.py` |
| 10–12 (deeper: scaled) | `/v1/<run>/<view>/<tag>/<z>/<x>/<y>.png` | this Worker, on the first request; then R2 + edge cache |

The Worker renders with **the app's own engine** (`vaHiEngine`, cut out of the
app build by `tools/make_va_engine.py`), so server tiles and the old on-device
rendering are identical byte for byte (`test/test_worker.mjs` checks it).
Without the service (no `tiles` block in the manifest, or it does not answer)
the app renders on the device as before.

## One-time setup

1. **Cloudflare account** (free). Ideally a domain on Cloudflare, e.g.
   `snowmapper.ch` → Worker on `tiles.snowmapper.ch`. Without a domain the
   Worker runs on `snowmapper-tiles.<you>.workers.dev`.
2. **R2 bucket** named `snowmapper-tiles` (Dashboard → R2 → Create bucket).
3. **R2 API token** (R2 → Manage API tokens → "Object Read & Write" on that
   bucket). Note *Access Key ID*, *Secret Access Key* and your *Account ID*.
4. **Cloudflare API token** for deploying (My Profile → API Tokens → template
   "Edit Cloudflare Workers"; add *R2 Storage: Edit*).
5. **GitHub → Settings → Secrets and variables → Actions**
   - Secrets: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
     `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`
   - Variables: `TILES_BASE_URL` (e.g. `https://tiles.snowmapper.ch` or the
     `workers.dev` URL), optional `R2_BUCKET` (default `snowmapper-tiles`)
6. Run the workflow **Tiles worker** once (or push to `main`): it tests and
   deploys. The next live cycle / published demo uploads its tiles and the
   app switches over by itself.
7. **Plan:** rendering a tile takes ~0.1–0.3 s of CPU. The Workers *Free*
   plan allows 10 ms per request, so on Free only tiles already in R2 / the
   cache are served and the app renders the rest on the device. **Workers
   Paid (~5 USD/month)** lifts this to 30 s. R2's free tier (10 GB, egress
   free) covers the storage.

## What lives where (bucket)

```
runs/<run>/pack/index.json, pack/f_<tag>.png   metric packs (from the export)
runs/<run>/terrain/shade.png, forest.png, precip.png
runs/<run>/overview/<view>/<tag>.pmtiles        zoom 5-9
runs/<run>/cache/<view>/<tag>/<z>/<x>/<y>.png   rendered tiles (Worker)
```

`<run>` is `live-<github run id>` or `demo-<github run id>`; old runs are
removed per kind on publish (`TILES_KEEP_RUNS`, live 3 / demo 2).

## Local development

```bash
python run_interactive.py --split --offline --date 2026-04-01 --res 4000 --out-dir dist
python tools/make_va_engine.py dist/app.js tiles/worker/src/va_engine.js
cd tiles/worker && npm ci
node test/test_worker.mjs ../../variant_a_export      # test
node test/dev_server.mjs ../../variant_a_export        # local service on :8787
```

## Moving to another provider

`src/render.js` knows nothing about Cloudflare: it gets two functions
(read a file of the export, fetch a terrain tile). `src/index.js` is the
Cloudflare adapter; `test/dev_server.mjs` is a plain Node one -- the same
shape runs on Cloud Run or a VM. The upload uses the S3 protocol
(`S3_ENDPOINT` for GCS/S3/MinIO), and the app only knows `TILES_BASE_URL`.
