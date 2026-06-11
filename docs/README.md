# Swiss Snow Model — Wissenschaftliche Dokumentation

Physikalisch inspiriertes, pragmatisch implementiertes System zur Schätzung der
**Neuschneeverteilung** im alpinen Gelände der Schweiz auf einem **10 m × 10 m
Raster**. Das Modell verbindet numerische Wettervorhersagedaten mit hochauflösender
Topografie über eine Kette transparenter, dokumentierter Heuristiken.

> ⚠️ **Einordnung:** Dies ist ein *Schätz-* und *Visualisierungswerkzeug*, kein
> Messsystem. Es ersetzt weder die IMIS-Stationen des SLF noch operationelle
> Schneedeckenmodelle (SNOWPACK/Alpine3D) oder Lawinenwarnungen. Siehe
> [Limitationen](#limitationen).

---

## 1. Modellüberblick

Für jede Rasterzelle wird der Neuschnee über ein Zeitfenster (Standard 24 h) als
Produkt einer Niederschlagsbasis mit vier dimensionslosen Faktoren geschätzt:

```
Snow_new(cell) = base_precipitation
               × temperature_factor      (Phase: Regen vs. Schnee)
               × altitude_factor         (orograf. Niederschlags-Enhancement)
               × orographic_factor       (Luv-Verstärkung / Lee-Abschwächung)
               × wind_factor             (Erosion an Kämmen / Lee-Akkumulation)
```

Im Standardmodus `precip` ist die Basis der **Flüssigniederschlag** (mm) aus
Open-Meteo; Phase und Schneedichte werden im Modell berechnet (siehe SLR unten).
Im Modus `snowfall` dient direkt die von Open-Meteo gelieferte `snowfall`-Größe
(cm) als Basis, die bereits eine Phasentrennung enthält.

Alle Koeffizienten liegen zentral in `config/model_params.yaml`. Jede Zwischen-
schicht (`snow_fraction`, `altitude_factor`, …) wird vom Modell zurückgegeben und
ist exportier-/visualisierbar — das Modell ist **keine Blackbox**.

---

## 2. Die Faktoren im Detail

### 2.1 Temperatur-Faktor — Schneephase

Anteil des Niederschlags, der als Schnee fällt, modelliert als logistische
(sigmoide) Funktion um eine Schwellentemperatur:

```
snow_fraction = 1 / (1 + exp(k · (T_cell − T_center)))        ∈ [0, 1]
```

- `T_center ≈ 1 °C`: Nassschnee fällt häufig auch leicht über 0 °C Lufttemperatur,
  da der relevante Übergang näher an der **Feuchtkugeltemperatur** liegt.
- `k ≈ 1.4 / °C`: Steilheit des Übergangsbereichs.

`T_cell` ist die auf die DEM-Zellenhöhe korrigierte Temperatur (siehe 2.2).
*Referenzrahmen:* US Army Corps of Engineers (1956); Dai (2008).

### 2.2 Höhen-Faktor — Lapse-Rate & Niederschlags-Enhancement

Zwei getrennte Höheneffekte:

**(a) Temperatur** über die Standard-Lapse-Rate:
```
T_cell = T_ref + Γ · (z_cell − z_ref),     Γ = −0.0065 K/m
```
`z_ref` ist die Modellhöhe der Wetterquelle (Open-Meteo liefert sie pro Punkt mit).

**(b) Niederschlag** über einen linearen orografischen Gradienten:
```
altitude_factor = clip(1 + γ · (z_cell − z_ref),  0.6 … 2.5),   γ = 0.0005 /m
```
d. h. ≈ +5 % Niederschlag pro 100 m Höhengewinn — eine konservative Näherung des
in den Alpen gut belegten Höhen-Niederschlags-Zusammenhangs.

### 2.3 Schnee-zu-Wasser-Verhältnis (SLR)

Im Modus `precip` wird Flüssigniederschlag in Schneehöhe umgerechnet. Das
Verhältnis hängt stark von der Temperatur ab (kältere Luft → lockerer Schnee):

```
SLR = clip(10 + (−0.8) · T_cell,  6 … 25)     [cm Schnee / cm Wasseräquivalent]
snow_pre = P_cell [mm] · snow_fraction · SLR / 10     [cm]
```

Bei 0 °C ≈ 10:1, bei −10 °C ≈ 18:1. *Referenzrahmen:* Roebber et al. (2003).

### 2.4 Orografischer Faktor — Luv/Lee

Bei Anströmung wird Luft an windzugewandten Hängen gehoben (Niederschlags-
verstärkung), an Lee-Hängen sinkt sie ab (Abschattung):

```
alignment       = cos(aspect − wind_from)          (+1 Luv, −1 Lee)
orographic_factor = clip(1 + k_oro · sin(slope) · wind_norm · alignment, 0.4 … 1.8)
```

`wind_from` ist die meteorologische Richtung, *aus der* der Wind weht;
`aspect` die Richtung, in die der Hang abfällt. `wind_norm = wind_speed / 8 m/s`.

### 2.5 Wind-Faktor — Verfrachtung

Vereinfachte Umverteilung bereits gefallenen Schnees nach der Geländekrümmung
und der Lee-Lage:

```
erosion    = k_erode   · wind_norm · max(curvature, 0)             (konvexe Kämme)
deposition = k_deposit · wind_norm · (max(−curvature,0)
             + 0.5 · lee · sin(slope))                              (Mulden / Lee)
wind_factor = clip(1 − erosion + deposition,  0.2 … 2.5)
```

`curvature` ist die z-normierte negative Laplace-Krümmung des DEM
(positiv = konvex/exponiert). Dies ist eine **Heuristik**, kein massenerhaltendes
Transportmodell (vgl. SnowTran-3D, Winstral Sx-Index als spätere Erweiterung).

---

## 3. Rasterlogik (10 m × 10 m)

- Das DEM definiert Gitter, Auflösung (10 m) und Koordinatensystem (LV95 /
  EPSG:2056, Einheit Meter). Zeile 0 liegt im Norden (north-up).
- Aus dem DEM werden **Slope**, **Aspect** und **Curvature** über finite
  Differenzen (`numpy.gradient`) abgeleitet.
- Wetterdaten sind viel grobgranularer (≈ 1–11 km). Sie werden auf einem dünnen
  Lat/Lon-Gitter abgefragt, nach LV95 transformiert und per **IDW** auf jede
  10-m-Zelle interpoliert (Windrichtung über sin/cos-Komponenten).
- Das Kernmodell ist vollständig **vektorisiert** und arbeitet zellweise.

**Skalierung auf die ganze Schweiz:** ein 10-m-Raster der Schweiz hat
≈ 770 Mio. Zellen — zu groß für einen Lauf. Der Orchestrator
`pipeline/switzerland.py` (Entry-Point `run_switzerland.py`) wählt daher
automatisch:

- **Einzelraster**, wenn die Zellzahl unter `MAX_SINGLE_GRID_CELLS` liegt
  (z. B. 200-m-Vorschau, ~2 Mio. Zellen) — schnell, für nationale Übersichten.
- **Kachelung + Mosaik**, sobald es feiner wird: die Landesfläche wird in
  Kacheln (Default 20 km) zerlegt, je Kachel läuft die Pipeline, anschließend
  werden die Kachel-GeoTIFFs mit `rasterio.merge` zu einem nahtlosen
  Landesraster zusammengeführt. Echte 10-m-Läufe nutzen diesen Weg mit einem
  landesweiten DEM (swissALTI3D) als `dem_path`; `load_dem_geotiff` schneidet
  pro Kachel das passende Fenster aus.

Das synthetische Demo-DEM ist bewusst eine **deterministische Funktion der
absoluten LV95-Koordinaten** (ohne Zufalls-Mikrorelief), damit benachbarte
Kacheln nahtlos aneinanderpassen und das Mosaik konsistent ist.

**Datums- und Fensterwahl:** `run_switzerland.py` fragt zuerst das Datum
(YYYY-MM-DD) und dann das Akkumulationsfenster (24 h oder 72 h) ab. Daraus
werden `start_date`/`end_date` für Open-Meteo abgeleitet (24 h → ein Tag,
72 h → drei Tage); die stündlichen Werte werden über das Fenster aggregiert
(Niederschlag/Schnee summiert, Temperatur/Wind gemittelt, Richtung als
Vektormittel). Für viele Abfragepunkte batcht der Client die API-Requests.

---

## 4. Datenquellen

| Quelle | Rolle | Auflösung | Zugang |
|---|---|---|---|
| **Open-Meteo Forecast API** | Wetter (precip, snowfall, T, Wind) | ≈ 1–11 km, stündlich | HTTP-GET, kein Key (nicht-kommerziell), Multi-Location |
| **swissALTI3D (swisstopo)** | DEM, primär | 0.5 / 2 m → 10 m aggregiert | swisstopo Download (LV95/EPSG:2056) |
| **Copernicus DEM (GLO-30)** | DEM, Alternative | 30 m | Copernicus Data Space (EPSG:4326) |
| **SLF / IMIS** | Validierung & Bias-Korrektur (nur indirekt) | Stationspunkte | SLF Open Data / measurement-api.slf.ch |

**Open-Meteo:** `https://api.open-meteo.com/v1/forecast`. Variablen
`temperature_2m` [°C], `precipitation` [mm, Stundensumme], `snowfall` [cm],
`wind_speed_10m` [m/s], `wind_direction_10m` [°, *woher*]. Modellwahl über
`models=` (z. B. `best_match`, `meteoswiss_icon_ch1`).

**DEM beziehen:** Drei Wege. (1) `--offline` nutzt ein synthetisches Demo-DEM.
(2) `--real-dem` lädt automatisch das **Copernicus DEM (GLO-30, 30 m)** als
Cloud-Optimized-GeoTIFF direkt über HTTP (AWS Open Data, ohne Login) und
reprojiziert/mosaikiert die nötigen 1°-Kacheln auf das AOI-Raster (LV95) -
reales Terrain ohne manuellen Download. (3) Für echte 10 m swissALTI3D als
GeoTIFF herunterladen, auf 10 m aggregieren und via `--dem` übergeben.

**SLF:** gemessene Neuschneewerte (HN) der IMIS-Stationen als CSV mit Spalten
`station_id, longitude, latitude, elevation, new_snow_cm`. Diese Daten gehen
**nicht** ins Raster ein, sondern dienen ausschließlich der Bias-Korrektur
(`validation/`).

---

## 5. Architektur

```
swiss_snow_model/
├── config/            Parameter, AOI, Pfade — KEINE Logik
├── data_connectors/   Datenzugriff (Open-Meteo, DEM, SLF, synthetisch) — KEINE Modelllogik
├── model/             ALLE Formeln (Terrain, Faktoren, Interpolation, Kern) — KEINE API-Calls
├── pipeline/          Orchestrierung + Geo-Transform + Export
├── outputs/           GeoTIFF / CSV / PNG
├── viz/               matplotlib-Karten
├── validation/        SLF-Vergleich & Bias-Korrektur
└── docs/              diese Dokumentation
```

Strikte Trennung: Connectors holen nur Daten, das Modell rechnet nur (reine
numpy-Funktionen), die Pipeline verbindet beides und schreibt Output.

---

## 6. Output

- **GeoTIFF** (Hauptoutput): einbandig, Float32, georeferenziert in LV95
  (`new_snow_<aoi>.tif`).
- **CSV**: ausgedünnte Rasterzusammenfassung (Koordinaten + Schlüsselschichten).
- **PNG** (optional, `--plot`): Mehrfach-Panel-Diagnosekarte.

### Overlay-Ebene über der Schweizer Karte (`--overlay`)

Für die Darstellung als **10-m-Pixel-Layer über einer Schweizer Karte** werden
drei zusätzliche, georeferenzierte Artefakte erzeugt (`pipeline/overlay_export.py`):

1. **WGS84-GeoTIFF** (`*_wgs84.tif`, EPSG:4326): das nach Lat/Lon reprojizierte
   Raster (bilinear) als Standard-Layer für GIS — direkt über eine swisstopo-
   Hintergrundkarte in QGIS legbar.
2. **RGBA-PNG-Kachel** (`*_overlay.png`): kolorierte Pixel-Ebene, schneefreie
   Flächen (< 0.5 cm) und NoData vollständig transparent; ein `.json`-Sidecar
   hält die Lat/Lon-Eckkoordinaten für beliebige Web-Karten.
3. **Leaflet/folium-Karte** (`map_<aoi>.html`): interaktive Karte mit Schweizer
   Basemap (swisstopo Pixelkarte + OpenStreetMap, per LayerControl umschaltbar)
   und der exakt georeferenziert überlagerten Neuschnee-Ebene inkl. Farb-Legende.

Die LV95→WGS84-Reprojektion erzeugt ein leicht gedrehtes Rechteck (korrektes
Warp-Verhalten); die Ebene liegt damit lagerichtig auf der realen AOI-Position.
Für eine **schweizweite** Ebene werden mehrere AOI-Kacheln berechnet und die
WGS84-GeoTIFFs zu einem Mosaik zusammengeführt (siehe Abschnitt 3, Tiling).

---

## 7. Limitationen

- **Heuristisch, nicht prognostisch validiert:** Faktoren sind plausibel, aber die
  Defaults sind nicht gegen Messreihen kalibriert. Ohne Bias-Korrektur sind
  Absolutwerte als *relative* Verteilungsmuster zu lesen.
- **Wettereingang ist grob:** ≈ 1–11 km Modelldaten, per IDW auf 10 m gebracht —
  feinskalige konvektive Zellen oder lokale Stauzonen werden nicht aufgelöst.
- **Keine Massenerhaltung bei der Windverfrachtung:** Erosion und Deposition sind
  entkoppelte Heuristiken; echte Saltation/Suspension fehlt.
- **Keine Setzung/Schmelze/Altschnee-Wechselwirkung:** reiner Neuschnee-Input,
  keine Schneedeckenentwicklung.
- **Keine Wald-/Vegetationsinterzeption, keine Strahlung.**
- **Kein Lawinenprodukt:** liefert keine Gefahrenstufen und keine
  sicherheitsrelevanten Aussagen.

**Warum es kein Messsystem ersetzt:** Es misst nichts. Es kombiniert Vorhersage-
und Geländedaten zu einer physikalisch *plausiblen* Schätzung. Für belastbare
Werte braucht es IMIS-Messungen, Radar/Disdrometer und validierte
Schneedeckenmodelle.

---

## 8. Mögliche Erweiterungen

1. **SLF-Integration zur Live-Kalibrierung:** `correction_factor` aus
   `validation/` als zeit-/höhenabhängiges Feld zurückspielen.
2. **Echte Windverfrachtung:** Winstral-Sx-Sheltering-Index oder gekoppeltes
   Transportmodell (SnowTran-3D-Ansatz) statt reiner Krümmungsheuristik.
3. **Höhenabhängige SLR und Feuchtkugel-Phasentrennung** (rel. Feuchte aus API).
4. **Machine Learning Post-Processing:** Random-Forest/GBM-Bias-Korrektur auf
   IMIS-Targets, mit den hier berechneten Faktoren als interpretierbaren Features.
5. **NetCDF/xarray-Zeitstapel** für Mehrtages- und Ensemble-Läufe.
6. **Tiling-Orchestrator** für flächendeckende Schweiz-Läufe.

---

## 9. Literatur (Rahmen)

- US Army Corps of Engineers (1956): *Snow Hydrology.*
- Dai, A. (2008): Temperature and precipitation phase. *Geophys. Res. Lett.*
- Roebber, P. et al. (2003): Snow-to-liquid ratio. *Wea. Forecasting.*
- Winstral, A. et al. (2002): Terrain-based wind redistribution parameters.
- Liston & Elder (2006): SnowModel / SnowTran-3D.
