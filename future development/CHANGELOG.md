# Changelog — Swiss Snow Model

Physikalisch inspiriertes Neuschnee-Raster-Modell für die ganze Schweiz,
ausgegeben als **eine interaktive HTML-Datei** (wepowder-Stil). Kombiniert
Open-Meteo-Wetterdaten, ein swisstopo-Geländemodell und SLF/IMIS-Bergstationen
zu einem Multi-Layer-Dashboard mit ziehbarem Zeitfenster (±5 Tage, fliessend
aufsummiert) und Mess-Crosscheck an realen Bergstationen.

Format orientiert sich an [Keep a Changelog](https://keepachangelog.com).

---

## [v7] — Powder Decision Engine & Exposition-Fix

### Added
- **Powder-Layer (neu):** Per-Zelle binäre Bewertung (POWDERED = TRUE / FALSE)
  über das gewählte Zeitfenster. Regelbasierte Engine mit drei Stufen:
  1. **Zerstörungsregeln** (D1 Regen 48 h, D2 Frost-Tau >4, D3 Solarstrahlung ≥5000 Wh/m²/Tag)
  2. **Erhaltungsregeln** (R1 Tiefkühlung Tmax<−2 °C, R2 Windstille Böe<20 km/h,
     R3 Wind-Umverteilung Lee 20–60 km/h, R4 Frost+klare Nacht Nordhänge)
  3. **Nachfilter:** Solar-Moderation (2000–5000 Wh → S/W entfernt),
     Intermediate Degradation (2–4 Frost-Tau-Zyklen → „reduziert")
- **Powder-Klick-Popup:** Zeigt Powder-Status, Regelflags, gültige Expositionen,
  Qualität, Tmax, Ø Wind und ≈ Böe für jede angeklickte Zelle.
- **Niederschlags-Rasterdaten** an den Browser übergeben (für Regen-Erkennung).
- **Hauptgitter-Exposition & -Neigung** als statische Arrays für die Powder-Engine.
- **Umfassende README.md** mit Datenquellen, Berechnungen, Schwellenwerten,
  Architektur und bekannten Grenzen.

### Changed
- **Exposition diskret:** Kontinuierliche HSV-Farbmischung ersetzt durch harte
  Vier-Quadranten-Einfärbung ohne Interpolation:
  N = Blau (#4A90D9), O = Grün (#66BB6A), S = Rot (#EF5350), W = Gelb (#FFC107).
  Flache Zellen (Neigung <5°) einheitlich grau (#9E9E9E).
- **Expositions-Legende** zeigt die diskreten Quadranten mit Gradangaben.

### Bekannte Grenzen (Powder-Engine)
- **Böen-Näherung:** Böe ≈ Mittelwind × 1.5 (kein echtes Böendatum von Open-Meteo).
- **Klare-Nacht-Proxy:** Sonnenscheindauer = 0 + Temp < 0 °C; unterscheidet nicht
  zwischen bewölkter und klarer Nacht.
- **Lee-Vereinfachung:** Lee = 180° gegenüber dominanter Windrichtung am nächsten
  Windgitterpunkt (~9 km Auflösung); kein topografisches Strömungsmodell.
- **Solarstrahlung:** Klarhimmel-Modell; Schwellenwerte in Wh/m²/Tag (≈ W/m² × 10 h).

---

## [v6] — Feinschliff Wind, Stationen & Exposition

### Fixed
- **Wind-Animation-Bug:** Das frühere weiße Verblassen der Strömungslinien lag als
  eigene Ebene über der Karte und hat sie zunehmend zugedeckt/ausgewaschen. Jetzt
  verblassen Spuren via `globalCompositeOperation = 'destination-out'` **transparent**
  — die Karte darunter bleibt voll sichtbar.

### Changed
- **Stationen als Cluster** mit allen Werten rund ums Icon (unabhängig vom Layer):
  Mitte = Schneehöhe, rechts = Neuschnee (Intervall), links = Wind + Richtung
  (z. B. „NW 20"), oben = Schneeoberflächentemperatur, unten = Lufttemperatur.
  Klick öffnet weiterhin die Detailkarte.
- **Exposition feiner:** swissALTIRegio 60 m + Aspekt-PNG 3000 px, speicher-sorgsam
  gerechnet (Aspekt zuerst, Zwischenarrays freigeben; Rauigkeit/TPI aus dezimiertem
  DEM) → Couloirs und Grate klar aufgelöst.
- **Robustheit:** Open-Meteo-Client mit höherem Timeout (90 s) und kleinerer
  Blockgröße (15 Punkte/Anfrage) gegen serverseitige Überlast.

---

## [v5] — Datenquelle Exposition, Design & Bedienung

### Fixed
- **Aspect-Konventionsfehler behoben:** Ost/West war vertauscht. Jetzt verifiziert
  N = 0°, O = 90°, S = 180°, W = 270° (Richtung, in die der Hang abfällt).

### Added
- **Statistik „max <10 km/h"** im Wind-Layer (zeigt Zellen, deren Maximalwind im
  Intervall unter 10 km/h bleibt).
- **Fliessende Wind-Animation** (halbtransparente, mit dem Wind ziehende Partikel).

### Changed
- **Exposition** neu aus **swissALTIRegio** (nationales swisstopo-10-m-DEM, eine COG
  mit Overviews) statt Copernicus → deutlich sauberer.
- **Strahlung** nimmt den Tag automatisch vom **Fensterstart** (Tages-Slider entfernt).
- **Design-Überarbeitung:** großzügigeres Panel mit Sektionen, größere Touch-Ziele,
  **einklappbar** und **mobiltauglich** (responsive Top-Sheet auf schmalen Screens).

### Removed
- „Windschwach/Lee"-Filter (durch „max <10 km/h" ersetzt).

---

## [v4] — Strahlung, Wind-Raster & Temperatur-Logik

### Added
- **Sonnenstrahlung** (neue Ebene): physikalisches Solarmodell — Sonnenstand über
  den Tag, Hangneigung, Exposition und **Bergschatten** über Horizont-Höhenwinkel
  je Azimutsektor → Wh/m²/Tag (Klarhimmel), im Browser gerechnet.
- **Strahlung × Sonne** (neue Ebene): Klarhimmel-Strahlung × tatsächlicher
  Sonnenschein (Open-Meteo) → effektive Wh/m²/Tag.
- **Wind als farbige Fläche** (km/h, Ø/Max/Min) wie Temperatur, zusätzlich zu den
  Richtungspfeilen.

### Fixed
- Temperatur **„immer <0 °C"** zeigt korrekt nur Zellen, deren **Maximum** im
  Intervall unter 0 bleibt (vorher fälschlich „Anzahl Stunden <0").
- Exposition: Reprojektion auf **Nearest** umgestellt (behebt falsche Mischfarben
  an Graten, wo die Richtung um 180° kippt).

### Changed
- Temperatur **„Max 0–5 °C"**: nur Zellen mit Intervall-Maximum zwischen 0 und 5 °C.
- Feineres DEM für Exposition/Rauigkeit.

---

## [v3 und früher] — Fundament

### Added
- Interaktives Zeitfenster-Band (±5 Tage) mit fliessender Aufsummierung;
  Schnellwahl 24/48/72/120 h.
- **SLF/IMIS-Crosscheck:** gemessener Neuschnee (Σ HS-Zuwächse), aktuelle
  Schneehöhe, Luft- und Schneeoberflächentemperatur, Windgeschwindigkeit.
- Ebenen: Neuschnee, Temperatur, Wind, Sonne, Hangneigung (swisstopo-WMTS),
  Exposition, Rauigkeit, Schummerung/Relief (swisstopo-WMTS).
- Modularer Aufbau: `config/`, `model/`, `data_connectors/`, `pipeline/`;
  Offline-Modus mit synthetischem DEM/Wetter.
- Datenquellen: Open-Meteo (Forecast & Archiv), Copernicus-DEM, swisstopo-WMTS,
  SLF/IMIS-API.

---

## Bekannte Grenzen / offene Punkte

- **Exposition beim Zoom:** Echte 20-m-Grat-Auflösung ist mit *einem* nationalen
  PNG physikalisch nicht möglich (Detail an PNG-Größe gebunden, ~115 m/px bei
  3000 px über die ganze Schweiz). Bräuchte einen gekachelten Dienst oder eine
  **regionale** swissALTI3D-2-m-Version (z. B. Davos/Weissfluhjoch) als fokussiertes PNG.
- **Strahlung** ist ein **Klarhimmel-Näherungsmodell** (Muster stimmen,
  Absolutwerte ungeeicht — kein Messprodukt).
- **Sonne / Strahlung×Sonne / Live-Stationswerte** brauchen den Open-Meteo-Forecast
  bzw. die SLF-Live-API; das Open-Meteo-**Archiv** liefert keine Sonnenscheindauer
  (historische Tage daher ohne Sonnen-/Strahlungs-Ist-Werte).

---

## Build

```bash
python run_interactive.py --date today --days 5 --res 3000
# Optionen: --offline (synthetisch), --weather-step, --stations
```
Hinweis: In der Sandbox braucht es `GDAL_HTTP_UNSAFESSL=YES`; auf dem eigenen Mac nicht.
