# Swiss Snow Model 🇨🇭❄️

Physikalisch inspiriertes Neuschnee-Rastermodell für die Schweiz (10 m × 10 m).
Kombiniert Open-Meteo-Wetterdaten mit hochauflösender Topografie über
transparente, dokumentierte Heuristiken.

**Vollständige wissenschaftliche Dokumentation:** [`docs/README.md`](docs/README.md)

---

## 🎚️ Interaktive Karte (wepowder-Stil): Band, Layer, Mess-Crosscheck

Hält **stündliche** Felder über **heute ±5 Tage** vor; alles wird im Browser
live eingestellt:

```bash
python run_interactive.py                     # heute ±5 Tage (echt) + SLF-Bergstationen
python run_interactive.py --date 2026-02-19   # auf ein Datum zentriert (nur Modell-Layer)
python run_interactive.py --offline --res 3000  # synthetisch (Test)
```

In der HTML (`outputs/interactive_<datum>.html`):

- **Intervall-Band** (Start/Ende, max ±5 Tage) → Werte werden über den Bereich
  **fliessend aufsummiert** (Schnee sofort via Kumulativ-Differenz). Schnellwahl 24/48/72/120 h.
- **Layer:** Neuschnee · Temperatur 2 m · Wind 10 m · Sonnenstunden ·
  **Hangneigung** & **Schummerung** (hochaufgelöste **swisstopo-WMTS**:
  `ch.swisstopo-karto.hangneigung` mit Klassen ≥30°, Reliefschattierung) ·
  **Exposition** & **Rauigkeit** (fein ~250 m gerechnet, als scharfe PNG-Overlays).
- **Über einen Layer-Knopf fahren zeigt dessen Legende** (Hover-Info).
- **Temperatur-Statistik:** Ø / Max / Min / **Stunden < 0 °C** / **Max 0–5 °C**,
  mit **Klassen-Isolinien** (klare Grenzen inkl. 0-°C-Linie).
- **Wind:** dichtes Punktraster (~3 km), **topografisch moduliert** (Grat schneller,
  Mulde/Lee langsamer) → Detail bis auf einzelne Hangbereiche; Pfeile in **km/h**;
  Sub-Layer **„Windschwach/Lee"** hebt konsistent < 10 km/h bzw. geschützte Lagen hervor.
- **Sonnenstunden:** Summe der Sonnenscheindauer über das Fenster.
- **Sonnenstrahlung (NEU):** physikalisches Solarmodell für einen **wählbaren Tag**
  (Tages-Slider) — pro Geländezelle aus Hangneigung, Exposition, Sonnenstand und
  **Bergschatten** (Horizont-Höhenwinkel je Azimutsektor) → Wh/m²/Tag (Klarhimmel).
- **Strahlung × Sonne (NEU):** Klarhimmel-Strahlung × tatsächlicher Sonnenschein
  (Open-Meteo) über das gewählte Intervall → effektive Wh/m²/Tag.
- **Wind** als **Farbfläche** (km/h, wie Temperatur) + Pfeile + Lee-Sublayer +
  gemessene Windstationen (SLF, rot).
- **Mess-Crosscheck (SLF/IMIS-Bergstationen, ~2000–3000 m):** Schnee-Layer →
  gemessener Neuschnee (Σ HS-Zuwächse) bzw. aktuelle Schneehöhe; Temperatur-Layer →
  Icons mit **Luft- (L) und Schneeoberflächentemperatur (O)**.

> Auflösung der interaktiven Ebene: 3 km (Default; viele Layer × Stunden bleiben
> so flüssig, HTML ~10 MB). `--res 2000` für feiner/größer. Die **SLF-Live-API**
> deckt nur die jüngsten Tage ab → Stationen erscheinen nur im **Live-Fall**
> (heute ±5 Tage). Quellen: MeteoSwiss · SLF (WSL, CC BY 4.0) · swisstopo · Copernicus.

---

## 🇨🇭 Schweiz-weiter Lauf mit Datums- & Fenster-Auswahl

Berechnet den Neuschnee für die **ganze Schweiz** und legt das Ergebnis als
georeferenzierte Pixel-Ebene über eine Schweizer Karte. Der Nutzer wählt
**zuerst das Datum**, dann das **Akkumulationsfenster (24 h oder 72 h)**:

```bash
python run_switzerland.py --offline
# == Schweiz-weiter Neuschnee ==
# Datum waehlen (YYYY-MM-DD) [Default heute]: 2026-01-15
# Akkumulationsfenster waehlen - [1] 24h  [2] 72h: 2
```

Nicht-interaktiv / für Automatisierung:

```bash
# Online (Default): echtes Copernicus-Terrain + echtes Open-Meteo-Wetter
python run_switzerland.py --date 2026-02-19 --window 72

# Reproduzierbare Offline-Demo (synthetisch, NICHT geografisch korrekt):
python run_switzerland.py --date 2026-02-19 --window 72 --offline

# Echte 10 m mit eigenem swissALTI3D-GeoTIFF:
python run_switzerland.py --date 2026-02-19 --window 24 --res 100 \
        --dem /pfad/swissalti3d_ch.tif
```

**Datenquellen — Standardverhalten:**

- **Online (Default):** echtes **Copernicus-DEM (30 m)** wird automatisch über
  HTTP geladen (AWS Open Data, kein Login) + echtes Wetter von Open-Meteo.
  Reales Terrain ohne manuellen Download — der Neuschnee folgt damit den echten
  Alpenkämmen (wie bei SLF).
- `--offline`: synthetisches DEM + synthetisches Wetter (sofort, ohne Netz, für
  Tests). **Achtung:** synthetisches Terrain ⇒ Schnee an *fiktiven* Höhen, nicht
  geografisch korrekt — nur zum Testen der Pipeline, nicht zum Vergleich mit SLF.
- `--dem PFAD`: eigenes GeoTIFF (z. B. **swissALTI3D** für echte 10 m).

> Validierung gegen SLF: Die Karte nutzt die **feste SLF-Farbskala** (Klassen
> 1/5/10/20/30/40/60/80/100/150 cm, grün→blau→rot) — unabhängig vom Datum, damit
> jede Karte direkt mit der offiziellen SLF-Neuschneekarte vergleichbar ist. Das
> Verteilungsmuster (Schnee auf den hohen Kämmen, Mittelland/Täler frei)
> reproduziert die SLF-Karten gut. Absolute cm-Werte sind heuristisch und nicht
> event-kalibriert — dafür ist die SLF-Bias-Korrektur (`validation/`) gedacht.
>
> **DEM-Robustheit:** Das Copernicus-DEM wird mit harten HTTP-Timeouts geladen
> (kein Hängen) und nur dezimiert über die COG-Overviews gelesen (schnell, wenig
> Bandbreite). Das fertige AOI-DEM wird unter `data/dem_cache/` zwischengespeichert
> — Folgeläufe (anderes Datum/Fenster, gleiche Region/Auflösung) laden es sofort.

**Datum & Endpoint:** Liegt das Datum mehr als ~80 Tage zurück, wird automatisch
der **ERA5-Archiv-Endpoint** genutzt (der Forecast deckt nur ~92 Tage Vergangen-
heit bis ~16 Tage Zukunft ab). Sommerdaten liefern korrekt 0 cm Schnee — für ein
sinnvolles Ergebnis ein Schneefall-Ereignis wählen (z. B. 19.–21.02.2026).

**Rate-Limit (HTTP 429):** Open-Meteos Gratis-Tier ist limitiert. Der Client
wiederholt automatisch mit Backoff und pausiert zwischen Batches. Bei vielen
Punkten ein gröberes `--weather-step` (z. B. 0.25) wählen — das reicht, weil das
Wettermodell ohnehin grobauflösend ist und die Terraindetails aus dem DEM kommen.

> Hinter einem Firmen-Proxy mit eigener TLS-Inspektion kann das Lesen der
> Copernicus-COGs an der Zertifikatsprüfung scheitern. Dann einmalig
> `export GDAL_HTTP_UNSAFESSL=YES` setzen. Auf einem normalen Rechner nicht nötig.

**Auflösung & Rechenstrategie:** Default ist eine **200-m-Vorschau** (ein
Einzelraster, ~2 Mio. Zellen, wenige Sekunden). Feinere Auflösungen schalten
automatisch auf **Kachelung + Mosaik** um. Echte **10 m** für die ganze Schweiz
(~771 Mio. Zellen) erfordern ein landesweites DEM (`--dem`, z. B. swissALTI3D)
und entsprechend Rechenzeit — die Kachel-Pipeline ist dafür ausgelegt.

Output (`outputs/`): `new_snow_switzerland_<24|72>h.tif` (LV95),
`*_wgs84.tif`, `*_overlay.png` (transparente Pixel-Ebene) und
`map_switzerland_<24|72>h.html` (Leaflet-Karte mit swisstopo-Basemap).

---

## Schnellstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Reproduzierbare Offline-Demo (synthetisches DEM + Wetter, kein API-Call):
python main.py --offline --plot

# Echter Lauf (Open-Meteo + eigenes swissALTI3D-GeoTIFF):
python main.py --dem /pfad/swissalti3d_davos.tif --hours 24 --plot
```

Output landet in `outputs/`: GeoTIFF (`new_snow_davos.tif`), CSV-Zusammenfassung
und optional eine PNG-Karte.

## CLI

| Flag | Bedeutung | Default |
|---|---|---|
| `--dem PATH` | DEM-GeoTIFF (sonst synthetisch) | – |
| `--hours N` | Akkumulationsfenster [h] | 24 |
| `--offline` | synthetisches Wetter (kein API-Call) | aus |
| `--step-deg X` | Wetter-Gitterweite [Grad] | 0.03 |
| `--idw-power X` | IDW-Exponent | 2.0 |
| `--plot` | Diagnose-Panel als PNG | aus |
| `--overlay` | 10-m-Overlay-Ebene über Schweizer Karte | aus |
| `--no-html` | Overlay ohne Leaflet-HTML | aus |

## Overlay-Ebene über der Schweizer Karte

```bash
python main.py --offline --overlay        # bzw. mit echtem --dem
```

Erzeugt in `outputs/` drei georeferenzierte Artefakte für die AOI an ihrer realen
Lage auf der Schweizer Karte:

- `new_snow_<aoi>_wgs84.tif` — reprojiziertes GeoTIFF (EPSG:4326) zum direkten
  Einladen in QGIS über eine swisstopo-Hintergrundkarte.
- `new_snow_<aoi>_overlay.png` — RGBA-Pixel-Ebene (schneefrei = transparent),
  inkl. `.json`-Sidecar mit den Lat/Lon-Eckkoordinaten.
- `map_<aoi>.html` — fertige **Leaflet/folium-Karte** mit Schweizer Basemap
  (swisstopo Pixelkarte + OpenStreetMap, umschaltbar) und der überlagerten
  10-m-Neuschnee-Ebene samt Farb-Legende. Einfach im Browser öffnen.

## Architektur

```
config/           Parameter & AOI (keine Logik)
data_connectors/  Datenzugriff: Open-Meteo, DEM, SLF, synthetisch (keine Modelllogik)
model/            Formeln: Terrain, Faktoren, Interpolation, Kernmodell (keine API-Calls)
pipeline/         Orchestrierung, Geo-Transform, Export
outputs/          GeoTIFF / CSV / PNG
viz/              matplotlib-Karten
validation/       SLF-Vergleich & Bias-Korrektur
docs/             wissenschaftliche Dokumentation
```

## Kernformel

```
Snow_new = base_precipitation
         × temperature_factor    # Regen/Schnee-Phase (Sigmoid um ~1 °C)
         × altitude_factor       # +5 %/100 m Niederschlags-Enhancement
         × orographic_factor     # Luv-Verstärkung / Lee-Abschwächung
         × wind_factor           # Erosion an Kämmen / Lee-Akkumulation
```

Konfiguration aller Koeffizienten: [`config/model_params.yaml`](config/model_params.yaml).

## AOI ändern

In `config/settings.py` die `AOI`-Datenklasse anpassen (Bounds in LV95/EPSG:2056).
Für die ganze Schweiz über mehrere Kacheln iterieren — siehe Doku, Abschnitt 3.

## Konfiguration der Wetterquelle

Umgebungsvariablen (optional):
```bash
export OPEN_METEO_MODEL=meteoswiss_icon_ch1   # statt best_match
export OPEN_METEO_URL=https://api.open-meteo.com/v1/forecast
```

---

> ⚠️ Schätz- und Visualisierungswerkzeug, **kein** Mess- oder Lawinenwarnsystem.
> Limitationen in `docs/README.md`, Abschnitt 7.
