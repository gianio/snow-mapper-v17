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
`web/migration-latest.sql` ausführen (Storage-Policies, Kommentare, Profile),
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
- Kein eigener Backend-Server — Supabase Free-Tier reicht für 50 Nutzer locker.
- Kein natives App-Store-Release — PWA deckt die Testphase ab.

## Kosten Testphase
GitHub (Pages+Actions) 0 CHF · Supabase Free 0 CHF · Sentry Free 0 CHF ·
Plausible ~9 €/M (oder Umami selbst gehostet 0) · Domain optional ~15 CHF/Jahr.
