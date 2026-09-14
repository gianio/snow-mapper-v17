# Architektur-Empfehlungen — Testgruppen-Launch Herbst 2026

Stand: Juli 2026. Ziel: 10–50 Testnutzer:innen, iPhone-lastig, ohne Neuschreiben
der App. Sortiert nach Aufwand/Nutzen; P0 = vor dem Launch, P1 = während der
Testphase, P2 = nur wenn die Gruppe wächst.

---

## P0 — Blocker vor dem Launch

### 1. Die 32-MB-Monolith-Seite aufteilen (größtes Problem)
`index.html` bettet alle Datenwürfel (Schnee/Temp/Wind/Sonne/Terrain) als
Base64 ein → **~32 MB Erstladung**, auf Berg-Mobilfunk 30–120 s, und jeder
Daten-Refresh lädt auch das komplette UI neu.

Empfehlung (minimal-invasiv, Pipeline bleibt):
- Export in **App-Shell (`index.html`, ~300 KB)** + **Daten-Blob
  (`data-YYYYMMDDHH.bin`)** trennen. Der Shell lädt den Blob per `fetch` mit
  Fortschrittsbalken (das Intro ist dafür schon da) und cached ihn.
- Blob **gzip/brotli-komprimiert** ablegen (die u8-Würfel komprimieren ~3–5×;
  GitHub Pages liefert `.gz` nicht automatisch → vorkomprimieren und mit
  `DecompressionStream('gzip')` im Client entpacken, 5 Zeilen).
- `latest.json` zeigt auf den aktuellen Blob → UI-Deploys und Daten-Updates
  sind entkoppelt.
- Aufwand: ~1–2 Tage in `interactive_export.py` + kleiner Loader.

### 2. GitHub Pages Source auf „GitHub Actions" stellen
Steht noch aus (Settings → Pages). Ohne das liefert Pages die
Jekyll-Platzhalterseite statt der App.

### 3. Supabase-Migration einspielen + E-Mail-Template
`supabase/migrations/20260901000000_privacy_moderation_ratings.sql` ausführen (Storage-Policies, Kommentare, Profile),
im „Confirm signup"-Template `{{ .Token }}` ergänzen (6-stelliger Code).

### 4. Haftungs-Disclaimer (nicht optional)
Die App zeigt „Powder/Skiable" — das ist lawinenrelevante Information.
Erster App-Start: Modal „Experimentelle Modelldaten, ersetzt kein
SLF-Lawinenbulletin, Nutzung auf eigenes Risiko" mit Zustimmung (localStorage).
Dazu Link auf slf.ch. Für eine CH-Testgruppe zusätzlich kurze
Datenschutzerklärung (DSG: Konto-, Standort- und Foto-Daten in Supabase/EU).

---

## P1 — In der ersten Testwoche

### 5. Beobachtbarkeit
- **Sentry** (Browser-SDK, 1 Script-Tag) für JS-Fehler — sonst debuggt ihr
  per Screenshot-Chat.
- **Plausible/Umami** für anonyme Nutzung (welche Layer, wie oft Feed/Reports).
- **Feedback-Knopf** im Profil-Sheet → mailto oder kleines Supabase-Formular.

### 6. CI härten (Basis existiert)
- Im Deploy-Workflow vor dem Upload: `node --check` auf das extrahierte
  Inline-Script + `python tools/eval_powder.py --offline` als Regressionstest
  der Powder-Engine (Cache einchecken oder als Artifact).
- Wöchentlicher Cron: Harness **online** laufen lassen → Engine-Drift sichtbar.

### 7. Supabase-Härtung
- Rate-Limit für Reports (z. B. Trigger: max. 20 Reports/Nutzer/Tag).
- `reports.flagged`-Spalte + „Melden"-Knopf für Moderation.
- Tägliche Backups aktivieren (Dashboard, 1 Klick).
- Bild-Upload clientseitig auf ~1600 px verkleinern (Canvas), sonst füllen
  12-MP-Fotos den Free-Tier-Storage (1 GB) schnell.

### 8. PWA-Manifest + Service Worker
Die Nutzer verwenden die App bereits „standalone". Manifest + simpler
SW (App-Shell cache-first, Daten-Blob network-first) macht sie installierbar
und startet offline mit den letzten Daten — im Gelände Gold wert.

---

## P2 — Nur bei Wachstum (>50 aktive Nutzer)

- **Daten als Tiles** (PMTiles/COG auf R2/Supabase-Storage) statt Ein-Blob →
  lädt nur den sichtbaren Ausschnitt; nötig, wenn Auflösung/Gebiet wachsen.
- **Eigene Domain** + Versionierung der Daten-Snapshots (Rollback).
- **Serverseitiges Passkey-Login** (Edge Function) statt des lokalen
  WebAuthn-Gates.
- **Push-Notifications** echt ausliefern (Service Worker + Web Push/VAPID,
  Edge Function als Sender) — der Profil-Toggle existiert schon.
- Model-Rechnung von GH Actions auf einen Runner mit mehr RAM/Zeitbudget,
  falls Auflösung ↑.

## Bewusst NICHT empfohlen für den Herbst
- Kein Framework-Rewrite (React etc.) — der Single-File-Ansatz ist für diese
  Teamgröße ein Feature, kein Bug. Erst bei >3 Mitwirkenden überdenken.
- ~~Kein eigener Backend-Server~~ → **überholt, siehe Teil B** (Sept 2026): das
  SNOWPACK/Alpine3D-Layer braucht einen zustandsbehafteten Rechen-Host. Für
  *Auth/Feed/Reports* bleibt Supabase aber richtig — der Host rechnet nur.
- ~~Kein natives App-Store-Release~~ → **überholt, siehe `apple-app/ROADMAP.md`**
  (Sept 2026): iOS ist beschlossen, Capacitor-Gerüst existiert.

---
---

# Teil B — Backend für das SNOWPACK/Alpine3D-Layer

Stand: September 2026. Gilt zusätzlich zu Teil A oben. Anlass: das
Skiqualitäts-Layer wird von einem **numerischen Modell** (SNOWPACK, später
Alpine3D) gerechnet, das **täglich läuft und den Schneedeckenzustand
fortschreibt**. Das ist architektonisch etwas anderes als alles bisherige und
sprengt den bisherigen „alles in GitHub Actions"-Ansatz.

## B1 — Warum das nicht in CI laufen kann

Der Lauf von heute startet aus dem Zustand von gestern. Daraus folgt:

- **Der Zustand ist das eigentliche Asset.** Gehen die `.sno`-Zustandsdateien
  verloren, muss die **ganze Saison** ab Schneejahresbeginn neu gerechnet
  werden. Nicht der Code ist das Wertvolle, sondern der fortgeschriebene
  Zustand.
- **GitHub Actions ist ephemer.** Container sind weg, Caches unzuverlässig —
  genau diese Fehlerklasse ist in `deploy.yml` schon dokumentiert (ein
  Cache-Key lieferte monatelang einen halbfertigen Demo-Datensatz aus).
  Sequenzieller Zustand + wegwerfbare Runner ist die falsche Kombination.
- **Rechenbudget.** Der Deploy-Job ist bei 60 min bereits knapp. SNOWPACK pro
  Säule ist um Größenordnungen teurer als die heutige Rasterarithmetik.

→ **Eigener Host mit persistenter Disk.** Klein (1D-SNOWPACK auf einem
stratifizierten Punktsatz: wenige €/Monat), groß nur für Alpine3D
(Cluster/Burst-Instanz).

## B2 — Zwei Läufe, nicht einer

| Lauf | Antrieb | Zustand | Zweck |
|---|---|---|---|
| **Analyse** (autoritativ) | Vergangenheit/Analyse | schreibt den Zustand fort | die Saison |
| **Prognose** (Zweig) | Vorhersage | **verwirft** den Zustand | Zukunft im Timeslider |

Der Prognoselauf zweigt vom aktuellen Zustand ab, rechnet n Tage vor und wird
täglich weggeworfen. Er darf **nie** in den Zustand zurückfließen, sonst
akkumuliert sich Prognosefehler über die Saison. Das entspricht genau dem, was
die App heute schon zeigt (Vergangenheit + Zukunft in einem Zeitstrahl).

## B3 — Lücken sind Gift

Ein ausgefallener Tag blockiert den nächsten. Nötig:

- **Antriebsdaten validieren, bevor gerechnet wird** (nicht erst hinterher).
- **Lückenfüllung** für fehlende Meteo-Eingaben — MeteoIO kann das.
- **Catch-up-Modus**: n verpasste Tage der Reihe nach nachrechnen.
- **Datierte Zustands-Snapshots** statt nur „latest" → Replay ab beliebigem Tag,
  wenn Antriebsdaten revidiert werden oder ein Modellfehler behoben ist.
- **Backups** der Zustandsdateien. Siehe B1: das ist die Saison.
- **Alerting**, wenn ein Lauf ausfällt — sonst fällt es erst Tage später auf.

## B4 — Zwei Ebenen, App fasst den Host nie an

```
[Rechen-Host: privat, zustandsbehaftet, persistente Disk]
   täglicher Analyselauf  -> Zustand(t) + abgeleitetes Skiqualitäts-Feld
   täglicher Prognoselauf -> Prognosezweig (wegwerfbar)
        |
        v  publiziert unveränderliche, versionierte Artefakte
[Object Storage / CDN: öffentlich, zustandslos]
   data/<stamp>.json(.gz) + latest.json (Pointer)
        |
        v  nur lesend
[Web-App (GitHub Pages) + iOS-App]
```

- **Atomar publizieren**: schreiben, dann den Pointer umlegen. Das ist exakt das
  `latest.json`-Muster, das die Pipeline schon benutzt — nie ein Teilergebnis
  veröffentlichen.
- **Ein fehlgeschlagener Lauf degradiert auf das letzte gute Artefakt**, statt
  die App zu brechen. Die App darf nie von der Verfügbarkeit des Hosts abhängen.
- Der `--split`-Build trennt Shell und Datenblob bereits → clientseitig ändert
  sich fast nichts.

## B5 — Lizenz: Modell bleibt auf dem Host

SNOWPACK/Alpine3D stehen unter GPL-Familie. **Nur abgeleitete Zahlen** dürfen
die Grenze zur App überschreiten, das Programm selbst bleibt auf dem Host.
Damit wird das Modell nicht „distributed" und die GPL-Pflichten reichen nicht in
ein geschlossenes iOS-Binary hinein. Das ist ein Lizenz- *und* ein
Rechenbudget-Argument für dieselbe Trennung.

Zusätzlich: **Attribution/Zitation** für SLF/WSL-Modelle, und nirgends den
Eindruck einer SLF-Billigung oder eines Bulletin-Ersatzes erzeugen (Teil A §4).

## B6 — Zusammenfall mit iOS

Derselbe Host liefert das, was die iOS-App ohnehin braucht: **frische
Prognosedaten ohne neues App-Store-Release**. Der Boot-Loader kann das schon
(`SNOW_REMOTE_DATA_BASE`, remote-first mit Fallback auf die im Binary
gebündelte Kopie). Also **eine** Infrastruktur für drei Bedürfnisse:
Skiqualitäts-Layer, Datenaktualität in der App, und Rechnen außerhalb von CI.

## B7 — Reihenfolge

1. SLF/WSL kontaktieren (Datenzugang/Kollaboration) — lange Vorlaufzeit, kostet nichts.
2. SNOWPACK 1D an einigen IMIS-Stationen prototypen, gegen gemessene HS
   validieren (`validation/` existiert). Beweist die Antriebskette — das ist das
   Hauptrisiko.
3. Skiqualitäts-Index aus den Profilen ableiten; gegen **eigene Nutzer-Reports**
   kalibrieren (der `progdiff`-Layer vergleicht Modell vs. Meldungen schon).
4. Auf stratifizierte virtuelle Hänge skalieren, als neues Layer ausliefern —
   parallel zum bestehenden „Powder Conditions", zum Vergleich.
5. Rechnen von CI auf den Host umziehen (gemeinsam mit iOS-Datenlieferung).
6. Optional: Alpine3D für **eine** Region — nur damit kommen Triebschnee und
   Abgeweht *räumlich* heraus (laterale Prozesse, 1D kann das nicht).

## B8 — Kosten (Größenordnung, zusätzlich zu Teil A)

Kleine VM mit persistenter Disk für 1D-SNOWPACK ~5–20 €/M · Object
Storage/CDN für die Artefakte ~1–5 €/M · Alpine3D nur bei Bedarf als
Burst-Instanz (deutlich teurer, stunden- statt monatsweise buchen) ·
Apple Developer Program 99 $/Jahr.

## Kosten Testphase
GitHub (Pages+Actions) 0 CHF · Supabase Free 0 CHF · Sentry Free 0 CHF ·
Plausible ~9 €/M (oder Umami selbst gehostet 0) · Domain optional ~15 CHF/Jahr.
