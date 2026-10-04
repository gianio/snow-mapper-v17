# Snowmapper – App-Übersicht

Kurzfassung der ganzen App: was sie macht, wie sie rechnet, welche Daten,
Modelle, Schnittstellen und Bibliotheken sie nutzt. Dazu: was rechtlich
geklärt sein muss, bevor die App kommerziell angeboten wird, und was es für
den App Store braucht.

> Die technischen Details stehen in [`README.md`](../README.md) (Basis-Modell,
> Datenquellen, Schwellwerte) und [`variant_a/README.md`](../variant_a/README.md)
> (SNOWPACK-Pipeline). Dieses Dokument fasst zusammen und verweist dorthin.

> **Kein Rechtsrat.** Die Abschnitte 7 und 8 sind eine technische
> Bestandsaufnahme der Punkte, die zu klären sind. Lizenzbedingungen ändern
> sich; vor einem kommerziellen Start mit einer Fachperson (Recht/Datenschutz)
> prüfen. Wo etwas unsicher ist, steht **«prüfen»**.

---

## 1. Was die App macht

Eine Karte der Schweizer Alpen, die zeigt, **wo und wann es guten Schnee zum
Skifahren gibt**: Neuschnee, Pulver, Harsch, Sulz, Wind- und Sonneneinfluss –
für einen frei wählbaren Zeitpunkt zwischen etwa 5 Tagen zurück und 5 Tagen
voraus.

| Bereich | Was der Nutzer sieht |
|---|---|
| **Basis-Ebenen** | Neuschnee, Schneehöhe, Temperatur, Wind (animiert), Sonne/Strahlung, Oberflächentemperatur, «befahrbar», Hangneigung, Exposition, Relief |
| **Powder finden / Prognose** | Pulver-Entscheid pro Zelle (Regelwerk) und eine Prognose aus Community-Meldungen (ähnliche Hänge in der Nähe) |
| **Skiqualität (SNOWPACK)** | physikalisch simulierte Schneedecke: 20+ Klassen (Pulver, Harsch, Bruchharsch, Sulz, Triebschnee, Oberflächenreif …), Dichte, Schneeprofil beim Antippen |
| **Bewölkung** | Gesamtbewölkung (Open-Meteo `cloud_cover`, %), gemittelt über das Zeitfenster |
| **Overlays** | SLF-Lawinenbulletin, Skitourenrouten (swisstopo) mit Powder-Score aus SNOWPACK (standardmässig an), IMIS-Messstationen |
| **Community** | Konto, Meldungen mit Foto/Zonen, Feed, Folgen, Nachrichten, Moderation (Melden), Konto löschen |
| **Werkzeuge** | 3D-Ansicht, Ortssuche, Gipfelerkennung per Texterkennung (OCR), Zeichnen eigener Schnee-Karten, offline nutzbar (PWA) |

---

## 2. Architektur

```
            GitHub Actions (Server-Seite, kein eigener Server)
 ┌─────────────────────────────────────────────────────────────────────┐
 │ deploy.yml           nach Merge / von Hand: App-Shell + Datenblob   │
 │                      (Wetter, Gelände, Stationen, Bulletin, Routen) │
 │ variant-a-live.yml   alle 6 h: SNOWPACK-Matrix → Frames + Packs     │
 │                      Zustand (Schneedecke) als Artefakt weitergeben │
 │ variant-a.yml        Demo-/Testläufe (fixes Datum)                  │
 │ cloudflare.yml       nur von Hand: Worker-Deploy + Kacheln nach R2  │
 │ validate.yml         Tests (Python + JS), Parität Engine↔Pipeline   │
 └───────────────┬─────────────────────────────────────────────────────┘
                 │ statische Dateien
                 ▼
        GitHub Pages (index.html, app.js, data/…, sw.js)
                 │
                 ▼
 ┌──────────────────────── Browser / iOS-WebView ──────────────────────┐
 │ Leaflet-Karte · Powder-Engine (JS) · Strahlungsmodell (JS)          │
 │ SNOWPACK-Darstellung: grobe Frames (PNG) + scharfe Kacheln im       │
 │ Web Worker aus Gelände-Kacheln · Service Worker (offline)           │
 └───────────────┬─────────────────────────────────────────────────────┘
                 │ Konto, Meldungen, Fotos, Feed
                 ▼
          Supabase (EU): Postgres + Auth + Storage + Edge Function
```

- **Ein Generator für alles:** `pipeline/interactive_export.py` erzeugt die
  komplette Web-App (HTML/CSS/JS in einer Datei bzw. Shell + `app.js`).
- **iOS:** `apple-app/` packt dieselbe Web-App mit **Capacitor** in eine
  native Hülle (WKWebView). `ios-native/` ist der Keim einer echten
  Swift-App, die die JS-Engine über JavaScriptCore nutzt (noch nicht kompiliert).
- **`web/`** (React/Vite) ist ein nicht ausgeliefertes Experiment.

---

## 3. Physik und Mathematik

### 3.1 Basis-Schneemodell (Python, `model/`)
Pro Rasterzelle, stündlich (Details: `README.md` §2):

| Schritt | Formel | Quelle/Idee |
|---|---|---|
| Temperatur auf Zellhöhe | `T = T_ref − 6.5 K/km · Δz` | Standardatmosphäre |
| Niederschlag mit Höhe | `P · (1 + 0.0004·Δz)`, begrenzt 0.6–1.8 | orografische Verstärkung |
| Regen/Schnee | logistische Funktion, Mitte 1 °C, Steilheit 1.4 | Dai (2008) |
| Schnee-Wasser-Verhältnis | `clip(10 − 0.8·T, 6, 25)` | Roebber et al. (2003) |
| Luv/Lee | `1 + 0.6·sin(Neigung)·Wind·cos(Exposition − Windrichtung)` | Luv-Lee-Heuristik |
| Windverfrachtung | Erosion auf Kuppen, Ablagerung in Mulden (TPI), 0.2–2.5 | vereinfachte Saltation |
| Frost-Tau-Zyklen | Nulldurchgänge zählen (×0.5) | – |
| Schmelze/Setzung | `melt = TF·max(0, T−T_melt) + SRF·(1−α)·I` mit Einstrahlung `I` auf den geneigten Hang (`model/ablation.py`) | Temperatur-Strahlungs-Index, Hock (1999), Pellicciotti et al. (2005) |

### 3.2 Strahlung (im Browser)
Sonnenstand aus Deklination und Stundenwinkel; Horizontabschattung aus
12 Sektoren; Direktstrahlung `1361 · 0.72^AM · cos(Einfallswinkel)`, diffus
`0.13 · 1361 · sin(h) · (1+cos Neigung)/2`; mit gemessener Sonnenscheindauer
skaliert.

### 3.3 Powder-Engine (im Browser, Regelwerk)
1. **Zerstörung** (eine Regel genügt → kein Pulver): Regen in 48 h, mehr als
   4 Frost-Tau-Zyklen, Strahlung ≥ 5000 Wh/m²/Tag.
2. **Erhaltung**: tiefe Kälte, wenig Wind, Windverfrachtung (nur Lee), Kälte +
   klare Nacht (nur Nord).
3. **Sonne** streicht Süd/West bei 2000–5000 Wh/m²/Tag.
4. Exposition der Zelle muss in der gültigen Menge liegen.

### 3.4 Skiqualität mit SNOWPACK («Variante A», `variant_a/`)
Das Herzstück für die Schneequalität.

- **SNOWPACK** (SLF) rechnet für jeden virtuellen Hang eine 1-D-Schneedecke:
  Energiebilanz an der Oberfläche (Strahlung, fühlbare/latente Wärme),
  Wärmeleitung in der Schneedecke, Phasenwechsel (Schmelzen/Gefrieren),
  Wassertransport, Setzung (viskose Kompaktion) und Kornmetamorphose
  (Rundkörner, Kanten, Becher, Schmelzformen, Oberflächenreif).
- **Matrix statt Fläche** (wie die Disentis-Läufe):
  - 135 Wetterpunkte (Abstand ~15 km) über den Schweizer Alpen.
  - Pro Punkt: Höhenstufen alle 300 m (1200–3300 m) × 8 Expositionen ×
    Neigungen 0°/20°/38°/45° → ~21 000 SNOWPACK-Läufe.
  - Temperatur pro Stufe mit −6.5 K/km angepasst.
- **Auf die Karte bringen (Interpolation):**
  - Gewichte der 3 nächsten Wetterpunkte: `w = 1/(d + d0)²`, `d0 = 7.5 km`.
  - Innerhalb eines Punkts **trilinear** zwischen Höhe, Neigung und Exposition.
  - **Horizontschatten:** Anteil verdeckter Sonne (16 Richtungen, Strahl bis
    15 km, nach Einfallswinkel gewichtet) mischt die Zelle Richtung Nordhang.
  - **«Gated» Grössen** (Harsch, Schwachschicht, Reif) zählen nur, wenn ≥ 50 %
    des Gewichts aus Läufen kommt, die sie wirklich haben.
- **Korrekturen und Zusatzphysik:**
  - **Wind:** Transport ~ `(U − 5 m/s)³` über 24 h → Triebschnee (Lee) und
    Abtrag (Luv) je nach Hangrichtung.
  - **Wald:** ESA-WorldCover-Maske dämpft die Darstellung.
  - **Niederschlagsmuster 1 km** (live): Neuschnee pro Zelle × `R =
    P(Zelle)/P(Mischung der Wetterpunkte)` aus ICON-CH1, R ∈ [0.5, 2].
  - **IMIS-Abgleich** (live): Stationen ≤ 12 km vom Wetterpunkt; Niederschlagsfaktor
    × r^0.5 mit r = (Messung+20 cm)/(Modell+20 cm), r auf 0.7–1.4 und der Faktor
    auf 0.5–2.0 begrenzt.
- **Ansichten in der App** (Ebene «Skiqualität»): *Skiqualität* (6 Klassen:
  durchgehend hart, Kruste, Pulver 0–10 / 10–20 / > 20 cm, nass – wenig oder
  kein Schnee wird nicht gezeichnet; «nass» = flüssiger Wassergehalt der
  obersten Schicht > 1 Vol-%), *Triebschnee* (abgeblasen, leichte Ablagerung,
  Triebschnee aus dem Wind-Index), *Dichte*, *Detail* (18 Klassen).
- **Klassifizierung** (`classify.py`, im Browser identisch nachgebaut):
  Schwellen wie Pulver 5/15/30 cm, Harsch ≥ 0.25 cm bzw. Bruchharsch ≥ 2 cm,
  Eis ≥ 700 kg/m³, nass ab 1 % Wassergehalt, wenig Schnee < 20 cm, Wind ab
  Index 0.45.
- **Live-Betrieb:** Die Schneedecke wird von Lauf zu Lauf weitergegeben
  (Zustand = `.sno`-Dateien, 5 Tage hinter «jetzt», nur mit vergangenem Wetter
  fortgeschrieben). Kaltstart mit 21 Tagen Vorlauf bzw. im Herbst ohne Schnee.
- **Qualitäts-Gates vor dem Veröffentlichen:** ≥ 90 % der Läufe erfolgreich,
  alle Frames vorhanden, keine unplausiblen Werte, IMIS-Fehler ≤ 80 cm.

### 3.5 Darstellung: Kachel-Dienst und Gerät
- **Mit Kachel-Dienst** (`tiles/worker`, Cloudflare R2 + Worker): Zoom 5–9 aus
  PMTiles-Archiven pro Zeitschritt, Zoom 10–12 vom Worker beim ersten Abruf
  mit derselben Rechenlogik gerechnet und danach für alle zwischengespeichert.
  Das Handy zeigt nur Bilder.
- **Ohne Dienst (Rückfall):**
- **Rausgezoomt:** fertige Frame-Bilder (~250–330 m pro Pixel).
- **Ab Zoom 10:** Das Gerät rechnet die Klassen selbst pro Pixel (Web Worker)
  auf Gelände-Kacheln (~30 m) mit derselben Interpolation wie die Pipeline.
  Übereinstimmung mit der Pipeline: 99.7 % (in CI geprüft).
- Zeitschieber in festen 2-h-Schritten (‹ › an den Enden); die scharfen Kacheln
  werden pro Schritt neu gerechnet (2 Web Worker), die Nachbarschritte im
  Voraus, bereits gerechnete Kacheln kommen aus einem Cache.
- Ab Zoom 13 werden die Zoom-12-Kacheln vergrössert statt neu gerechnet.

### 3.6 Weitere Modelle
- **Tour-Bewertung** (`model/tour_score.py`, JS-Gegenstück): Bewertung entlang
  der swisstopo-Skitourenrouten. Mit SNOWPACK-Export: Powder-Score 0–100 aus
  der Prognose für morgen 10 Uhr – jede Route alle 200 m mit Höhe, Neigung und
  Exposition (~115 m Gelände) aus der Matrix gelesen, Pulvertiefe zählt am
  meisten, Kruste, Nässe und Windpressung ziehen ab; gemittelt über die
  Abfahrtsabschnitte (22–50°). Kernzone des Lawinenbulletins → kein Lob.
  Der Standort-Knopf zeigt die 10 besten Touren im Umkreis von 25 km.
- **Community-Prognose**: Meldungen werden auf Hänge mit ähnlicher Höhe und
  Exposition in der Nähe übertragen.

---

## 4. Modelle und Datenquellen (APIs)

| Quelle | Schnittstelle | Wofür |
|---|---|---|
| **Open-Meteo** | `api.open-meteo.com/v1/forecast`, `historical-forecast-api…`, `archive-api…` | Wetter für Basis-Ebenen und SNOWPACK-Antrieb (ICON-CH1/CH2, ICON-D2, `icon_seamless`, ERA5-Archiv) |
| **MeteoSwiss OGD** | STAC `data.geo.admin.ch/api/stac/v1`, Lib `meteodata-lab` | ICON-CH1/CH2 direkt (Prognosestunden, 1-km-Niederschlagsfeld), SMN-Stationen |
| **SLF IMIS** | `measurement-api.slf.ch` | Schneehöhe an Stationen: Anzeige, Validierung, Korrektur |
| **SLF Lawinenbulletin** | `aws.slf.ch/api/bulletin` (CAAML) | Bulletin-Overlay |
| **swisstopo WMTS** | `wmts.geo.admin.ch` | Grundkarte, Relief, Hangneigung > 30°, Skirouten |
| **swisstopo Suche** | `api3.geo.admin.ch/rest/services/api/SearchServer` | Ortssuche |
| **swisstopo STAC** | `data.geo.admin.ch` | swissALTIRegio-DEM, Skitourenrouten (Vektor) |
| **Copernicus DEM GLO-30** | AWS Open Data (`copernicus-dem-30m`) | Höhenmodell des Hauptrasters |
| **Terrarium-Höhenkacheln** | `s3.amazonaws.com/elevation-tiles-prod` | scharfe SNOWPACK-Darstellung, 3D-Gelände |
| **ESA WorldCover 2021** | `esa-worldcover.s3…` | Waldmaske |
| **Supabase** | `*.supabase.co` (Auth, Postgres, Storage, Function `delete-account`) | Konten, Meldungen, Fotos, Feed |
| **Sentry** (optional) | `browser.sentry-cdn.com` | Fehlerprotokolle |
| **CDNs** | unpkg (Leaflet, MapLibre), jsDelivr (supabase-js, Tesseract), Google Fonts | Bibliotheken, Schrift |

**Wettermodelle:** ICON-CH1 (~1 km) und ICON-CH2 (~2 km) von MeteoSwiss,
ICON-D2 (DWD, ~2 km), ICON global/EU über `icon_seamless`, ERA5 (ECMWF) für
das Archiv. Pro Stunde wird das feinste verfügbare Modell genommen.
**Schneemodell:** SNOWPACK + MeteoIO (SLF), aus dem Quellcode gebaut
(`github.com/snowpack-model/snowpack`).

---

## 5. Bibliotheken

| Bereich | Bibliothek | Lizenz (prüfen bei Update) |
|---|---|---|
| Python | numpy, scipy, pandas | BSD-3 |
| | requests | Apache-2.0 |
| | rasterio, fiona (inkl. GDAL) | BSD-3 (GDAL: MIT/X) |
| | pyproj (PROJ) | MIT |
| | matplotlib | eigene, BSD-ähnlich |
| | Pillow | MIT-CMU (HPND) |
| | PyYAML | MIT |
| | meteodata-lab (MeteoSwiss, eigene venv), ecCodes | MIT bzw. Apache-2.0 – prüfen |
| Schneemodell | SNOWPACK, MeteoIO | **LGPL-3.0** |
| Web-App | Leaflet 1.9.4 | BSD-2 |
| | MapLibre GL JS 4.1.2 (3D) | BSD-3 |
| | supabase-js 2 | MIT |
| | Tesseract.js 5.1.1 (+ Sprachdaten) | Apache-2.0 |
| | Sentry Browser SDK | MIT |
| | Schrift Inter (Google Fonts) | SIL OFL 1.1 |
| iOS | Capacitor 6 + Plugins | MIT |
| Test/CI | Playwright (lokal), Node | Apache-2.0 / MIT |

---

## 6. Betrieb und Kosten (heute)

- **Hosting:** GitHub Pages (statisch, Limit ~1 GB Seite, ~100 GB/Monat Traffic).
- **Rechnen:** GitHub Actions (öffentliches Repo: Minuten gratis). Ein
  Live-Lauf dauert ~45 min, viermal täglich. Die Seite wird nur nach einem
  Merge (oder von Hand / nach einem Cloudflare-Lauf) neu gebaut; ein neuer
  Live-Lauf erscheint in der App also erst beim nächsten Deploy.
- **Daten:** Supabase (EU-Region), Gratis- oder Pro-Plan.
- **Kein eigener Server.** Zustand und Exporte liegen als Actions-Artefakte.

---

## 7. Rechtliches und Lizenzen für den kommerziellen Betrieb

### 7.1 Blocker – vor dem Verkauf lösen
| Punkt | Warum | Was tun |
|---|---|---|
| **Open-Meteo** | Die freie API ist **nur nicht-kommerziell** erlaubt. | Kommerzielles Abo («API-Plan») abschliessen oder auf MeteoSwiss OGD umstellen, für den Prognoseteil schon teilweise eingebaut. Das Archiv (Vergangenheit) braucht dann eine eigene Lösung. |
| **GitHub Pages** | Laut GitHub-Richtlinien nicht für kommerzielle Angebote (SaaS/Online-Business) gedacht; Grössen- und Traffic-Limits. | Hosting auf einen kommerziellen Anbieter verschieben (z. B. Cloudflare Pages/R2, Netlify, eigenes S3 + CDN). |
| **Eigene Lizenz fehlt** | Das Repo hat keine `LICENSE`-Datei. | Festlegen (proprietär oder Open Source); Rechte an Beiträgen Dritter klären. |
| **Haftung (Lawinen)** | Die App beeinflusst Entscheide im Lawinengelände. | AGB/Nutzungsbedingungen mit Haftungsbeschränkung. Nach Schweizer Recht ist ein Ausschluss für grobe Fahrlässigkeit nicht möglich (OR 100). Den Hinweis «kein Lawinenbulletin» (heute beim ersten Start) überall sichtbar halten. Produkthaftung und Versicherung prüfen. |

### 7.2 Datenlizenzen – Quellenangaben in der App
| Quelle | Lizenz | Pflicht |
|---|---|---|
| swisstopo (Karten, DEM, Routen, Suche) | OGD Schweiz, kommerziell frei | Quellenangabe «© swisstopo» |
| MeteoSwiss OGD (ICON, Stationen) | offene Behördendaten | Quellenangabe «Quelle: MeteoSchweiz»; genaue Bedingungen prüfen |
| SLF IMIS-Messdaten | CC BY 4.0 | «Daten: SLF/WSL, CC BY 4.0» |
| SLF Lawinenbulletin | Nutzungsbedingungen SLF – **prüfen** | Inhalt nicht verändern; nicht den Eindruck einer SLF-Partnerschaft erwecken |
| Copernicus DEM | Copernicus-Lizenz, kommerziell frei | Quellenangabe, keine ESA-Billigung suggerieren |
| ESA WorldCover | CC BY 4.0 | «© ESA WorldCover project 2021» |
| Terrarium-Höhenkacheln | gemischte Quellen (SRTM, EU-DEM, …) | Quellenliste der Kacheln angeben – **prüfen** |
| ERA5 / ECMWF (über Open-Meteo) | Copernicus/ECMWF-Lizenz | über Open-Meteo-Bedingungen abgedeckt – prüfen |

Alle Quellen zusammen in einem «Impressum / Datenquellen»-Bildschirm aufführen.

### 7.3 Software-Lizenzen
- **SNOWPACK/MeteoIO (LGPL-3.0):** Läuft nur auf dem Server (GitHub Actions)
  und wird nicht ausgeliefert, daher keine Weitergabepflichten. Wird es je
  in der App mitgeliefert, Quellcode-Angebot und Lizenztext beilegen.
- **MIT/BSD/Apache-Bibliotheken** (Tabelle §5): Lizenztexte und Copyright-
  Hinweise in einem «Open-Source-Lizenzen»-Bildschirm aufführen.
  Apache-2.0 (Tesseract): auch NOTICE-Dateien.
- **Schrift Inter (OFL):** Nutzung frei, Lizenz beilegen, wenn die Schrift
  mitgeliefert wird.

### 7.4 Datenschutz (Schweiz revDSG, EU DSGVO bei EU-Nutzern)
- **Datenschutzerklärung:** welche Daten (E-Mail, Standort, Fotos inkl. EXIF,
  Meldungen, Nachrichten), wozu, wie lange, wo gespeichert (Supabase EU),
  Rechte (Auskunft, Löschung).
- **Verträge (DPA/AVV):** mit Supabase und Sentry.
- **Google Fonts und CDNs** übermitteln die IP-Adresse beim Laden. In der EU
  ist das ohne Einwilligung riskant (LG München I, 2022). Darum Schrift und
  Bibliotheken selbst hosten; die iOS-Version bündelt sie ohnehin.
- **Fotos:** werden vor dem Upload auf 1600 px neu kodiert; dabei fallen die
  EXIF-Daten (auch der Standort) weg. Nur wenn das scheitert, geht heute das
  Original hoch – diesen Fall abfangen.
- **Konto löschen:** vorhanden (Edge Function `delete-account`).
- **Sentry:** derzeit aus (DSN leer). Beim Einschalten bleibt `sendDefaultPii`
  aus, und es kommt in die Datenschutzerklärung.

### 7.5 Nutzerinhalte (Community)
- AGB: Rechte an hochgeladenen Fotos und Texten (Nutzungslizenz an den
  Betreiber), verbotene Inhalte, Moderation.
- Melden und Moderation ist vorhanden (`report_flags`, Auto-Flag,
  Rate-Limit). Für Apple braucht es zusätzlich «Nutzer blockieren» (§8.2).

### 7.6 Marke und Wettbewerb
- Namen «Snowmapper» / «Snow Model» auf Markenkonflikte prüfen
  (swissreg.ch, EUIPO, App-Store-Suche).
- Keine Logos von SLF, White Risk oder MeteoSwiss ohne Erlaubnis. Verweise
  als Text sind in Ordnung.

### 7.7 Geschäftliches
- Mehrwertsteuer ab CHF 100 000 Umsatz pro Jahr (Schweiz); Apple/Google
  rechnen die Steuer beim Verkauf in ihren Stores ab.
- Bezahlfunktionen in der App müssen über Apple-IAP laufen (§8.2).
  Kommission 15 % (Small Business Program) bzw. 30 %.

---

## 8. Aufnahme in den App Store

### 8.1 Grundlagen
- Apple Developer Program (99 USD/Jahr). Für eine Firma braucht es eine
  D-U-N-S-Nummer.
- Mac mit Xcode für Build, Signatur und Upload. Die Schritte stehen in
  `apple-app/README.md`.
- App Store Connect: Bundle-ID (`ch.snowmodel.app`, anpassbar), Name,
  Untertitel, Beschreibung, Keywords, Screenshots (6.7"/6.5", optional iPad),
  **Support-URL**, **Datenschutz-URL**.
- Zuerst TestFlight (bis 10 000 externe Tester; braucht eine kurze Beta-Prüfung).

### 8.2 Prüfrichtlinien, die diese App betreffen
| Richtlinie | Thema | Stand / was tun |
|---|---|---|
| **4.2 Mindestfunktion** | Reine Website-Hüllen werden abgelehnt. | Native Funktionen zeigen: GPS-Standort, Haptik, Teilen, Offline-Modus, Kamera; optional Push. Capacitor ist erlaubt, wenn die App einen Mehrwert bietet. |
| **2.5.2 Kein nachgeladener Code** | JS darf nicht von CDNs nachgeladen werden. | Leaflet, MapLibre, supabase-js, Tesseract, Sentry ins App-Paket bündeln. Daten (Wetter, Frames) nachladen ist erlaubt. |
| **1.2 Nutzerinhalte** | Filter, Melden, **Nutzer blockieren**, Kontakt, AGB/EULA. | Melden ✅, Moderation ✅; **Blockieren fehlt noch**; Kontakt (Feedback) ✅; EULA verlinken. |
| **5.1.1 Datenschutz** | Datenschutzerklärung, Konto in der App löschbar, nur nötige Daten. | Löschen ✅ (Edge Function); Datenschutz-URL nötig. |
| **5.1.1 Berechtigungen** | Klare Begründungstexte für Standort, Kamera und Fotos. | Texte in `apple-app/ios-config/Info.plist.additions.xml` – prüfen. |
| **Privacy Manifest** | `PrivacyInfo.xcprivacy` mit «Required Reason APIs» (seit 2024 Pflicht). | Datei vorhanden in `apple-app/ios-config/` – mit den Capacitor-Plugins abgleichen. |
| **App Privacy (Nährwertetikett)** | Angaben zu erhobenen Daten. | E-Mail, Fotos, Standort (ungefähr/genau), nutzergenerierte Inhalte, Absturzdaten (Sentry); kein Tracking → **kein ATT-Dialog** nötig. |
| **4.8 Login mit Apple** | Nur Pflicht bei Drittanbieter-Login (Google, Facebook …). | Heute nur E-Mail/Passwort + Code → nicht nötig. |
| **2.1 Vollständigkeit** | Keine Platzhalter; Prüfer müssen alles testen können. | **Demo-Konto** und Hinweise («Demo-Modus», Lawinen-Haftungshinweis) im Review-Text. |
| **1.4 Körperliche Sicherheit** | Apps mit Gefahrenbezug werden genau geprüft. | Haftungshinweis beim Start ✅; keine Sicherheitsversprechen in Beschreibung oder Screenshots. |
| **3.1.1 In-App-Käufe** | Digitale Inhalte (z. B. «Pro»-Ebenen) nur über Apple-IAP. | Erst relevant, wenn bezahlt wird. |
| **Export-Compliance** | Verschlüsselung | Nur HTTPS → `ITSAppUsesNonExemptEncryption = NO`. |
| **Altersfreigabe** | Fragebogen | Nutzerinhalte und Nachrichten → eher 12+. |

### 8.3 Google Play (falls Android)
- Entwicklerkonto (25 USD einmalig). Neue Privatkonten brauchen einen
  geschlossenen Test mit mindestens 12 Testern über 14 Tage.
- Formular «Datensicherheit»; Konto-Löschung zusätzlich über einen Web-Link.
- Aktuelles Target-API-Level einhalten.

---

## 9. Offene Punkte (kurz)

1. Open-Meteo kommerziell lizenzieren oder vollständig auf MeteoSwiss OGD wechseln.
2. Hosting weg von GitHub Pages; Bibliotheken und Schriften selbst hosten.
3. `LICENSE`, AGB/EULA, Datenschutzerklärung, Impressum mit allen Quellenangaben.
4. «Nutzer blockieren» für Apple 1.2; Foto-Upload ohne Neukodierung abfangen.
5. iOS: CDN-Skripte bündeln, Berechtigungstexte, Demo-Konto, Screenshots.
