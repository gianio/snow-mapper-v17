# Release-Plan 1.0 — Todo-Liste

Stand: 9. Oktober 2026 · Branch `release/1.0`
Baut auf [`launch-plan-2026-12-01.md`](launch-plan-2026-12-01.md) (Termine,
App Store) und [`launch-checklist.md`](launch-checklist.md) auf. Dieses Dokument
ist die **Arbeitsliste** bis zur Einreichung (Ziel: **3. November**).

Grundsatz: **Kill your darlings.** Was nicht direkt «Wo finde ich heute guten
Schnee?» oder «Was melden andere?» beantwortet, fliegt aus 1.0 raus oder wird
versteckt. Alles Gestrichene ist ein späteres Update, kein Verlust.

Legende: `[ ]` offen · `[~]` teilweise vorhanden · `[x]` erledigt
Priorität: **P0** = Blocker · **P1** = muss vor Einreichung · **P2** = wenn Zeit

### Inhalt
0. Ausgangslage · 0.1 Branch & Arbeitsweise
1. Abspecken — «Kill your darlings»
2. Onboarding & Auth — fail-safe
3. Notifications & Powder-Alarm
4. Design — einwandfrei auf jedem Gerät
5. Performance & Datenverbrauch
6. Navigation — keine Sackgassen
7. Layer — beste lesbare Auflösung
8. Release-Blocker (Infrastruktur, Build, Launch-Plan)
9. Rechtliches & App-Review-Richtlinien
10. Aufräumen & Code-Struktur
11. Übersetzungen (DE/EN/FR/IT)
12. Barrierefreiheit
13. Sicherheit & Datenschutz
14. Qualitätssicherung & Tests
15. Gerätetest auf echtem iPhone
16. Nach dem Launch
17. Reihenfolge / Zeitplan
18. Definition of Done

---

## 0. Ausgangslage (aus dem Code geprüft)

| Thema | Ist-Zustand | Handlungsbedarf |
|---|---|---|
| Live-Daten | ~~Boot-Loader setzt `isDemo=true`~~ → Live ist Standard, Demo nur über `?demo` / Einstellungen | ✅ |
| Registrierung | Passwort zweimal, Validierung, bekannte E-Mail erkannt | ✅ |
| Bestätigung | 6-stelliger Code via `verifyOtp` ✅, hängt aber am Supabase-Template | **P0** Template prüfen |
| Passwort zurücksetzen | Mit 6-stelligem Code (E-Mail → Code → neues Passwort ×2 → angemeldet) | ✅ (Supabase-Template noch umstellen) |
| Rate-Limit Login | 3 Fehler → 5 Min. Sperre mit Countdown (Login, Code, Reset, Erneut senden) | ✅ Client · offen: Server |
| Face ID | Als **App-Sperre** vorhanden, aber kein Speichern des Passworts / Auto-Login per Face ID | **P0** |
| Layer | 16 Layer/Ansichten (Basis + 9 Skiqualität-Unteransichten) | **P1** reduzieren |
| Tabs | Karte · Touren · Aufzeichnen · Feed · Melden | **P1** reduzieren |
| Navigation | Kein `history.pushState`/Zurück-Stack — Sheets haben keine einheitliche Zurück-Logik | **P0** |
| Push / Powder-Alarm | Nicht vorhanden | **P1** (neu im Scope) |
| Generator | `pipeline/interactive_export.py` = 16 000 Zeilen, eine Datei | Abspecken = auch Code löschen |
| Moderation | Meldungen melden ✅, aber **kein Nutzer-Blockieren** | **P0** (App Store 1.2) |
| Rechtliches | Kein Impressum, keine Nutzungsbedingungen als eigene Seite | **P0** |
| Übersetzungen | ~670 Einträge DE→EN/FR/IT, neueste Texte teils ohne Übersetzung | **P1** |
| Fehler-Tracking | Sentry-Stub vorhanden, **DSN leer** | **P0** |

### Unterwegs gefundene und behobene Fehler
- [x] **Neue Nutzer sahen weder Willkommens-Ablauf noch Disclaimer:** `dismissIntro()` lief beim Start, bevor `ONB_SLIDES`/`onb` deklariert waren (TDZ-Fehler `Cannot access 'onb' before initialization`). Jetzt nach dem Laden aufgerufen.
- [x] «Zum Home-Bildschirm hinzufügen» legte sich über das offene Anmeldefenster → wartet jetzt, bis Dialoge geschlossen sind
- [x] Anmeldefenster auf iPhone SE: scrollbar (Tastatur), Safe-Area-Abstände, leeres rotes Fehlerfeld ausgeblendet
- [ ] Alte Coach-Marks (`#coach`, z-index 8000) liegen über allen Dialogen → mit Kap. 10 entfernen

### 0.1 Branch & Arbeitsweise
- [x] Branch `release/1.0` von `main` erstellt (Stand nach Merge von gianio/snow-mapper-v17#117) und gepusht; Arbeitsstand sauber
- [x] Release-Arbeit läuft ab jetzt **nur** auf `release/1.0`; Plan per gianio/snow-mapper-v17#118 nach `main` gemergt, Umsetzung in Folge-PRs
- [x] CI: `validate.yml` läuft auf jedem PR nach `main`; `deploy.yml` (GitHub Pages) erst nach dem Merge in `main` — der Branch selbst wird nicht ausgeliefert
- [ ] Jedes Kapitel = eigener, kleiner Commit-Block; nach jedem Block `validate.yml` grün
- [ ] Code-Freeze für neue Features ab **27. Oktober** — danach nur noch Fixes
- [ ] Version festlegen: `1.0.0`, Build-Nummer pro TestFlight-Upload hochzählen
- [ ] `CHANGELOG.md` für 1.0 führen (dient auch als «Was ist neu»-Text im App Store)

---

## 1. Abspecken — «Kill your darlings» (zuerst entscheiden!)

Ziel: eine App, die man in **30 Sekunden versteht**. Nutzerfrage im Zentrum:
*«Wo ist heute/morgen Powder, und stimmt das?»*

### 1.1 Bleibt drin (Core)
- [ ] **Powder-Report aus SNOWPACK** (Ebene «Skiqualität») als **Startansicht** — das Alleinstellungsmerkmal
- [ ] **Neuschnee** und **Schneehöhe** als Basis-Layer
- [ ] **Wind** (für Triebschnee-Verständnis) — eine Ansicht, nicht drei
- [ ] **Zeitregler** (heute ±5 Tage) — evtl. auf −3/+3 kürzen (spart Daten)
- [ ] **Skitouren** mit Powder-Score (swisstopo-Routen)
- [ ] **Community:** Meldung erfassen (Foto + Zustand + Tour), Feed, Meldung melden (Moderation), Profil, Konto löschen
- [ ] **Gemeldetes Powder** als Layer (Community-Daten auf der Karte)
- [ ] **SLF-Bulletin-Link** + Disclaimer (rechtlich nötig)
- [ ] Ortssuche

### 1.2 Fliegt raus / wird versteckt (Feature-Flag, Code bleibt für 1.1)
- [ ] **Nachrichten / DMs** — Moderationsrisiko, kein Launch-Nutzen
- [ ] **Aufzeichnen / Recorder** als eigener Tab → raus (GPS-Tracking = Akku, Datenschutz, Review-Fragen)
- [ ] **Zeichnen eigener Schnee-Karten** → ersetzt durch einfachen «Zone markieren»-Schritt im Melden-Flow, oder ganz raus
- [ ] **OCR-Gipfelerkennung** — Spielerei, grosse Bibliothek (Tesseract)
- [ ] **3D-Ansicht** — teuer in Daten und Akku
- [ ] **Webcams** — externe API-Abhängigkeit
- [ ] **«x Leute schauen diese Tour an»** (Realtime Presence) — Websocket-Kosten, kaum Nutzen
- [ ] **Folgen-System** → raus aus 1.0 (Feed zeigt einfach alles in der Nähe)
- [ ] Layer **Temperatur, Bewölkung, Oberflächentemperatur, befahrbar, Exposition, Hangneigung, Relief** → in einen «Mehr»-Bereich oder raus
- [ ] Skiqualität-Unteransichten **Abweichung, Windgepresst, Sonnendeckel, Firn, Abgeweht, Dichte, Detail (18 Klassen)** → eine Ansicht «Skiqualität» (6 Klassen) + eine «Triebschnee»
- [ ] Prognose aus «ähnlichen Hängen» (Terrain-Similarity) → raus, verwirrt neben SNOWPACK
- [ ] Demo-Modus nur noch versteckt (Entwickler-Einstellung)

### 1.3 Ziel-Navigation (3 Tabs + 1 Aktion)
- [ ] **Karte** (Powder-Report, Layer-Wahl, Zeitregler)
- [ ] **Touren** (Liste + Powder-Score, Detail mit Meldungen)
- [ ] **Feed** (Community-Meldungen in der Nähe)
- [ ] **+ Melden** (zentraler Button, öffnet Melden-Flow)
- [ ] Profil/Einstellungen über Avatar oben rechts

### 1.4 Code wirklich entfernen
- [ ] Gestrichene Features hinter **einem** Flag-Objekt (`FEATURES = {dm:false, rec:false, …}`) — dann tote Pfade löschen
- [ ] Ungenutzte Übersetzungs-Strings entfernen
- [ ] `web/` (React-Experiment), `index 2.html`, Screenshots im Root aufräumen
- [ ] Bundle-Grösse vorher/nachher messen und hier eintragen

---

## 2. Onboarding & Auth — fail-safe

Ziel: Kein Nutzer bleibt je in einem Zustand hängen, aus dem er nicht
herauskommt. Jeder Schritt hat **Zurück**, **Erneut senden**, **Abbrechen**
und eine verständliche Fehlermeldung.

### 2.1 Erster Start (vor Login)
- [ ] Reihenfolge: Sprache → 3 Screens «So funktioniert's» (Powder-Karte, Touren, Melden) → Disclaimer akzeptieren → Standort (optional) → **Karte sofort nutzbar ohne Konto**
- [ ] Konto erst verlangen, wenn der Nutzer melden/kommentieren will («Lazy Signup»)
- [ ] Überspringen jederzeit möglich, Onboarding in Einstellungen erneut aufrufbar
- [ ] Fortschritt pro Schritt speichern (App-Kill mitten im Onboarding → setzt dort fort)

### 2.2 Registrieren
- [x] Felder: E-Mail, Benutzername, **Passwort**, **Passwort wiederholen**
- [ ] Live-Validierung: E-Mail-Format, Passwort ≥ 8 Zeichen, beide gleich, Benutzername frei (Check vor Absenden)
- [x] Passwort anzeigen/verbergen (Auge-Icon)
- [ ] `autocomplete="new-password"` / `username` korrekt → iOS-Schlüsselbund schlägt starkes Passwort vor und **speichert es**
- [x] Doppelklick-Schutz (Button deaktivieren während Request)
- [x] Bereits registrierte E-Mail → klare Meldung + direkt «Anmelden» / «Passwort vergessen» anbieten

### 2.3 E-Mail-Bestätigung per Code (kein Link!)
- [ ] **Supabase → Auth → Email Templates → «Confirm signup»:** nur `{{ .Token }}` (6-stelliger Code), **keinen** `{{ .ConfirmationURL }}`
- [ ] Template **«Reset password»** ebenfalls auf `{{ .Token }}` umstellen
- [ ] Template **«Magic Link»** / «Change email» prüfen (gleich)
- [ ] Eigener SMTP-Absender (z. B. Resend/Postmark) — Supabase-Standard-SMTP ist auf wenige Mails/h limitiert → **P0 für Launch**
- [x] Code-Eingabe: 6 Einzelfelder oder ein Feld mit `autocomplete="one-time-code"` + `inputmode="numeric"` (iOS füllt Code aus Mail-App vor)
- [x] Code einfügen aus Zwischenablage funktioniert
- [x] «Code erneut senden» mit **60-s-Countdown**
- [x] «Falsche E-Mail? Ändern» → zurück zum Formular, Eingaben bleiben erhalten
- [x] App geschlossen während Code-Schritt → beim nächsten Start direkt wieder im Code-Schritt
- [x] Code abgelaufen → klare Meldung + neuer Code

### 2.4 Nach Bestätigung: Face ID + Passwort speichern
- [x] Direkt nach erfolgreicher Bestätigung automatisch angemeldet (Session aus `verifyOtp`)
- [x] Angebot: **«Mit Face ID anmelden?»** — falls `BiometricAuth.checkBiometry()` verfügbar; sonst Schritt still überspringen
- [ ] Credentials sicher speichern: **iOS Keychain** (Capacitor `@capacitor-community/secure-storage` o. ä.) — **niemals** `localStorage`
- [ ] Speichern: Supabase **Refresh-Token** im Keychain, geschützt mit Biometrie (`kSecAccessControlBiometryCurrentSet`) — nicht das Klartext-Passwort
- [ ] Zusätzlich iOS-Passwort-Autofill nutzen (Schlüsselbund speichert E-Mail/Passwort selbst) → Associated Domains `webcredentials:` einrichten
- [ ] Web/PWA-Fallback: kein Face ID, Session bleibt via Supabase-Persistenz

### 2.5 Wiederkehrender Nutzer — automatisch erkannt
- [x] App-Start: gültige Session → direkt Karte, kein Login-Screen
- [ ] Session abgelaufen + Face ID aktiv → Face ID-Prompt → Refresh-Token aus Keychain → angemeldet
- [ ] Face ID abgebrochen/fehlgeschlagen → Fallback auf Passwort-Login (E-Mail vorausgefüllt)
- [ ] Face ID auf Gerät geändert (neues Gesicht) → Keychain-Eintrag ungültig → sauber auf Passwort zurückfallen
- [ ] Refresh-Token serverseitig widerrufen → sauber abmelden, kein Endlos-Loop

### 2.6 Passwort zurücksetzen — mit Code, alle Schritte
- [x] Schritt 1: E-Mail eingeben → `resetPasswordForEmail(email)` **ohne** `redirectTo`
- [x] Schritt 2: 6-stelligen Code eingeben → `verifyOtp({email, token, type:'recovery'})`
- [x] Schritt 3: neues Passwort **zweimal** → `updateUser({password})`
- [ ] Schritt 4: automatisch angemeldet; Face ID-Keychain mit neuem Token aktualisieren
- [x] Unbekannte E-Mail → **gleiche** Erfolgsmeldung zeigen (kein Konto-Enumerieren)
- [x] Jeder Schritt hat «Zurück» und «Abbrechen»
- [x] Alten Link-Pfad (`authOnRecovery`, `type=recovery` im Hash) als Fallback behalten, bis alle alten Mails abgelaufen sind

### 2.7 Brute-Force-Schutz: mehr als 3 Fehlversuche → 5 Min. Sperre
- [x] **Clientseitig:** Zähler pro E-Mail für Login, Code-Eingabe und Reset; nach 3 Fehlern Button gesperrt mit sichtbarem Countdown «Erneut versuchen in 4:59»; Zustand überlebt App-Neustart
- [ ] **Serverseitig (wichtig — Client allein ist umgehbar):** Supabase → Auth → Rate Limits setzen (Sign-in/OTP-Verify, E-Mails/h); zusätzlich `auth_attempts`-Tabelle + Edge Function oder Auth-Hook, der nach 3 Fehlern/5 Min. pro E-Mail **und** IP blockt
- [ ] CAPTCHA (hCaptcha/Turnstile, in Supabase integriert) bei Registrierung und Reset aktivieren
- [x] Gleiche Regel für «Code erneut senden» (max. 3 pro 5 Min.)

### 2.8 Test-Matrix Auth (alle Fälle manuell auf Gerät durchspielen)
- [ ] Neuer Nutzer, alles glatt
- [ ] Falscher Code 1×, 3× (Sperre), Code abgelaufen
- [ ] Mail kommt nicht an → erneut senden
- [ ] App-Kill in jedem Schritt
- [ ] Flugmodus in jedem Schritt (Fehlermeldung statt Spinner für immer)
- [ ] Bereits registrierte E-Mail registrieren
- [ ] Reset für unbekannte E-Mail
- [ ] Face ID verweigert in iOS-Einstellungen
- [ ] Gerät ohne Face ID (Touch ID / nur Code)
- [ ] Abmelden → wieder anmelden → Face ID funktioniert
- [ ] Konto löschen → Keychain geleert, Neu-Registrierung mit gleicher E-Mail möglich

---

## 3. Notifications & Powder-Alarm

Ziel: Der Grund, die App im Winter wieder zu öffnen.

### 3.1 Produkt
- [ ] Nutzer wählt **1–3 Lieblingsgebiete** (Kreis auf Karte oder Tour/Region aus Liste)
- [ ] Schwelle: «Alarm ab __ cm Neuschnee» (Standard 20 cm) oder «Skiqualität = Pulver > 20 cm»
- [ ] Max. **1 Push pro Gebiet und Tag**, Ruhezeit 21–07 Uhr, Versand ~06:30 vor Tourenstart
- [ ] Optional: «Neue Meldung auf meiner Lieblingstour»
- [ ] Push-Erlaubnis **erst** fragen, wenn der Nutzer einen Alarm einrichtet (nicht beim ersten Start)
- [ ] Antippen der Notification → öffnet Karte genau auf Gebiet + Zeitpunkt (Deep Link)
- [ ] Alarme in Einstellungen verwalten, pausieren, löschen

### 3.2 Technik
- [ ] `@capacitor/push-notifications` + APNs-Key (.p8) im Apple-Konto
- [ ] Tabelle `push_tokens (user_id, token, platform, updated_at)` + RLS
- [ ] Tabelle `powder_alerts (user_id, geom/center+radius, threshold_cm, enabled)` + RLS
- [ ] Edge Function `powder-alert`: nach jedem `variant-a-live`-Lauf aufgerufen, liest pro Alarm den Powder-Wert aus den Frames, versendet via APNs, dedupliziert (`alert_log`)
- [ ] Ungültige Tokens (APNs 410) automatisch löschen
- [ ] Web/PWA: Web Push optional (P2)
- [ ] Datenschutzerklärung + App-Privacy-Labels ergänzen (Gebiet = ungefährer Standort)

---

## 4. Design — einwandfrei auf jedem Gerät

### 4.1 Geräte-Matrix (alle Screens auf allen prüfen)
- [ ] iPhone SE (3. Gen., 375×667 — kleinster Screen, kein Notch)
- [ ] iPhone 13 mini (375×812)
- [ ] iPhone 15/16 (393×852, Dynamic Island)
- [ ] iPhone 16 Pro Max (440×956)
- [ ] iPad (falls unterstützt — sonst in App Store Connect «iPhone only»)
- [ ] Android Chrome (Web) + Desktop Safari/Chrome
- [ ] Hoch- und Querformat (oder Querformat bewusst sperren)

### 4.2 Regeln
- [ ] **Keine Überlappungen:** Tab-Bar, Kartenbuttons, Zeitregler, Sheets, Toasts, Dynamic Island, Home-Indikator — alle über `env(safe-area-inset-*)` ausgerichtet
- [ ] **Lesbarkeit:** Schrift min. 13 px (Labels), 15–17 px Fliesstext; Kontrast WCAG AA (4.5:1) auch auf Glas-Hintergrund über der Karte → Glas-Blur stärker oder halbdeckender Hintergrund
- [ ] **Touch-Ziele** ≥ 44×44 pt, mindestens 8 pt Abstand
- [ ] **Dynamische Schriftgrösse** (iOS «Grössere Schrift») bis 130 % ohne Abschneiden
- [ ] Lange Texte (FR/IT sind ~30 % länger als DE) → kein Abschneiden, Umbruch statt Ellipse bei Buttons
- [ ] Tastatur offen → Eingabefeld + Absenden-Button bleiben sichtbar (Auth, Melden, Kommentar)
- [ ] Dark/Light-Mode konsistent, Kartenlegende in beiden lesbar
- [ ] Leere Zustände (kein Feed, keine Meldung, offline) mit Text + Aktion statt leerer Fläche
- [ ] Ladezustände: Skeletons statt Spinner, nie > 300 ms leerer Screen
- [ ] Visuelle Regressionstests: Playwright-Screenshots aller Hauptscreens in 4 Viewports in CI

---

## 5. Performance & Datenverbrauch

Ziel-Budgets (auf iPhone SE, 4G, Kaltstart):

| Messgrösse | Budget |
|---|---|
| Erste Karte sichtbar | < 1.5 s (Wiederholt: < 0.5 s aus Cache) |
| Interaktiv | < 2.5 s |
| Initialer Download (gzip) | < 400 KB Shell+JS, Daten separat |
| Daten pro Sitzung (ohne Fotos) | < 2 MB |
| Layer-Wechsel | < 150 ms |
| Zeitregler-Schritt | < 50 ms (60 fps) |

### 5.1 Messen zuerst
- [ ] Lighthouse/WebPageTest-Baseline (Mobil, Slow 4G) dokumentieren
- [ ] Bundle-Analyse: welche Bibliotheken sind wie gross (Leaflet, Tesseract, 3D, Supabase-SDK …)
- [ ] Sentry Performance aktivieren (DSN setzen — liegt seit September brach)

### 5.2 Optimieren
- [ ] Gestrichene Features (Kap. 1.2) entfernen = grösster Gewinn
- [ ] Lazy-Load: Supabase-SDK erst bei Community-Nutzung, Touren-Daten erst im Touren-Tab
- [ ] Raster-Blobs gzip/brotli + `DecompressionStream` (aufgeschoben seit Checkliste → jetzt machen)
- [ ] Nur sichtbaren Zeitschritt + Nachbarn laden statt aller 11 Tage
- [ ] Rasterdaten als Uint8/Uint16 statt Float statt JSON
- [ ] Kacheln/Frames: WebP/AVIF statt PNG, `Cache-Control` + ETag, Service Worker «stale-while-revalidate»
- [ ] Fotos: Upload ≤ 1600 px ✅; im Feed **Thumbnails** (400 px, Supabase Image Transform) + `loading="lazy"`
- [ ] Feed paginiert (20 Einträge, dann nachladen)
- [ ] Realtime-Websocket nur, wenn wirklich nötig (Presence ist gestrichen)
- [ ] «Datensparmodus» (bei `navigator.connection.saveData` oder manuell): keine Bild-Autoloads, nur aktueller Tag
- [ ] Ältere Geräte: Web Worker für Rendering ✅, Animationen bei `prefers-reduced-motion` aus, Windanimation drosseln

---

## 6. Navigation — keine Sackgassen

### 6.1 Regeln
- [ ] **Jeder** Screen/Sheet hat einen sichtbaren Weg zurück (X oder ‹) **und** reagiert auf iOS-Wischgeste vom Rand / Android-Zurück
- [ ] Echter Navigations-Stack: `history.pushState` bei jedem Sheet/Detail, `popstate` schliesst das oberste → Browser-Zurück und Hardware-Zurück funktionieren überall
- [ ] Sheets per Herunterwischen schliessbar
- [ ] Tab erneut antippen → zurück zum Tab-Anfang (Liste nach oben scrollen)
- [ ] Zustand pro Tab erhalten (Scrollposition Feed, gewählter Layer, Zeitpunkt)

### 6.2 Verknüpfungen in alle Richtungen
- [ ] Karte → Tour → Meldungen der Tour → Meldung → Profil des Autors → dessen Meldungen → zurück auf Karte an Meldungsort
- [ ] Feed-Meldung → «Auf Karte zeigen» → Karte zentriert, Layer + Zeitpunkt passend
- [ ] Tour-Detail → «Powder melden» → Melden-Flow mit Tour vorausgefüllt → nach Absenden zurück zur Tour (nicht zur Startseite)
- [ ] Push-Notification → Gebiet auf Karte → Tour in der Nähe
- [ ] Deep Links (`snowmapper://tour/123`, `…/report/456`) für Teilen + Push

### 6.3 Prüfen
- [ ] Navigationsdiagramm aller Screens zeichnen; jeder Knoten braucht Eingang **und** Ausgang
- [ ] Playwright-Test: von jedem Screen aus «Zurück» bis zur Karte, ohne hängen zu bleiben
- [ ] Fehlerzustände (offline, 404, gelöschte Meldung) zeigen Text + «Zurück»-Button, nie leeren Screen

---

## 7. Layer — beste lesbare Auflösung

Ziel: **einfach, aber viel Detail**. Wenige Layer, dafür scharf.

- [ ] Layer-Auswahl auf **5** reduzieren: Skiqualität (Powder-Report, Standard) · Triebschnee · Neuschnee · Schneehöhe · Wind; plus Overlay-Schalter: Touren, Meldungen, Bulletin
- [ ] Skiqualität: Darstellung immer über **scharfe Kacheln aus dem Gelände** (Worker-Rendering), grobe Frames nur als Platzhalter während des Ladens
- [ ] Zoom-abhängige Auflösung: grob bei Übersicht, fein (≤ 100 m Gelände-Detail) ab Zoom 12 — nie unnötig viel laden
- [ ] Hillshade/Relief als dezenter Hintergrund immer an → Gelände lesbar ohne Extra-Layer
- [ ] Farbskalen: max. 6 Klassen, farbenblind-tauglich (prüfen mit Simulator), gleiche Bedeutung in allen Layern
- [ ] Transparenz so, dass Ortsnamen/Gipfel lesbar bleiben; Beschriftungs-Layer **über** dem Daten-Layer
- [ ] Legende: kompakt, antippbar für Erklärung in einem Satz pro Klasse
- [ ] Antippen auf Karte → kleines Info-Kärtchen («Pulver 15 cm, Nordhang 2400 m, vor 2 Tagen gefallen»)
- [ ] Retina: Kacheln in @2x rendern, Linien/Symbole als Vektor
- [ ] Vorher/Nachher-Screenshots aller 5 Layer in 3 Zoomstufen in `docs/` ablegen

---

## 8. Release-Blocker — Infrastruktur, Build, Launch-Plan

### 8.1 Daten
- [x] **P0** Live-Daten als Standard (`isDemo=false`), Demo nur versteckt
- [ ] **P0** Cloudflare-Tiles: Workflow `cloudflare.yml` mit `deploy_worker` **und** `publish` ausführen, damit die Ebene «Nur Pulver» live ist; danach prüfen, dass Kacheln auf dem Gerät laden
- [ ] **P0** `variant-a-live.yml` läuft stabil alle 6 h (letzte 10 Läufe grün)
- [ ] **P1** Monitoring: Alarm, wenn `data/latest.json` > 12 h alt
- [ ] **P2** Secret `WINDY_WEBCAMS_KEY` setzen — **nur falls Webcams in 1.0 bleiben** (laut Kap. 1.2 gestrichen; ohne Schlüssel blendet sich das Overlay sauber aus)

### 8.2 Supabase
- [ ] **P0** Migrationen via Supabase CLI (`link`, `migration repair`, `db push`) — `harden_functions` + `harden_functions_2` prüfen
- [ ] **P0** Edge Function `delete-account` deployt + getestet
- [ ] **P0** Backups (täglich) einschalten, Plan-Limits (DB 500 MB / Storage 1 GB / Egress 5 GB) prüfen, Upgrade-Schwelle festlegen
- [ ] **P0** **Leaked-Password-Protection** aktivieren (Auth → Passwords → HaveIBeenPwned-Prüfung), Mindestlänge 8 serverseitig
- [ ] **P0** Bestätigungs-E-Mail beim Registrieren enthält den **6-stelligen Code** (`{{ .Token }}`) — siehe Kap. 2.3
- [ ] **P0** Eigener SMTP-Versand (Kap. 2.3)
- [ ] **P1** Supabase **Security- und Performance-Advisors** ohne Warnungen

### 8.3 Native App & TestFlight
- [ ] **P0** Apple Developer Program aktiv, App-Record `ch.snowmapper.app` in App Store Connect
- [ ] **P0** Build-Kette: `npm install` → `npm run build:web` (mit `SNOW_REMOTE_DATA_BASE`) → `npx cap sync ios` → `apply-ios-config.py` → Signatur → Upload (`ios-testflight.yml`)
- [ ] **P0** Signatur-Secrets: `APPLE_CERTIFICATE_P12`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_PROVISIONING_PROFILE`, `APPSTORE_API_KEY_JSON`
- [ ] **P0** Info.plist-Zweckangaben nur für Funktionen, die **noch drin** sind (Standort, Kamera/Fotos, Face ID, Push) — gestrichene (z. B. Hintergrund-GPS) entfernen, sonst Review-Rückfrage
- [ ] **P0** App-Privacy-Labels + `PrivacyInfo.xcprivacy` = tatsächliches Verhalten
- [ ] **P0** Demo-Konto für App Review + Review-Notizen (Melden-Flow erklären)
- [ ] **P1** Store-Texte DE/EN/FR/IT, Keywords, Kategorie (Wetter), Altersfreigabe, Screenshots 6.9"/6.7" + 6.5"
- [ ] **P1** Native Integrationen sichtbar (Guideline 4.2): Face ID, Push, Haptik, Teilen, Offline

### 8.4 Fehler-Tracking
- [ ] **P0** Sentry-Projekt anlegen, Browser-DSN in `SENTRY_DSN` eintragen → Abstürze bei Testern sichtbar
- [ ] **P1** Release-Tag (`1.0.0+build`) an Sentry übergeben, Source-Maps hochladen
- [ ] **P1** Alarm bei neuem Fehler im Core-Flow (Login, Melden, Karte laden) per Mail

---

## 9. Rechtliches & App-Review-Richtlinien

- [ ] **P0** **Impressum** als eigene Seite (Name, Adresse, Kontakt — CH-Pflicht bei kommerziellem Angebot)
- [ ] **P0** **Datenschutzerklärung** als eigene Seite: E-Mail, Standort, Fotos, Inhalte, Push-Token, Face ID (bleibt auf dem Gerät), Sentry, Supabase EU, Löschung/Export, revDSG + DSGVO
- [ ] **P0** **Nutzungsbedingungen** als eigene Seite inkl. Null-Toleranz für anstössige Inhalte (Apple 1.2 verlangt das bei nutzergenerierten Inhalten)
- [ ] **P0** Alle drei Seiten verlinkt aus: App (Einstellungen + Registrierung), App Store Connect (Datenschutz-URL, Support-URL), Website
- [ ] **P0** Registrierung: Checkbox/Hinweis «Ich akzeptiere Nutzungsbedingungen und Datenschutz» mit Links
- [ ] **P0** **Nutzer blockieren** (Apple 1.2): Tabelle `user_blocks` + RLS, Feed/Kommentare blendet blockierte Nutzer aus, Menü «Blockieren» auf Profil und Meldung
- [ ] **P0** Moderation: gemeldete Inhalte innerhalb 24 h prüfen (Prozess + Admin-Ansicht oder SQL-View), Kontakt für Meldungen
- [ ] **P1** Disclaimer als Jurist gegenlesen: Positionierung «Schneebedingungen», **keine** Lawinenrisiko-Bewertung
- [ ] **P1** Datenlizenzen prüfen (Open-Meteo kommerziell?, swisstopo, SLF/IMIS) — siehe `docs/APP-UEBERSICHT.md` §7

---

## 10. Aufräumen & Code-Struktur

- [ ] **P1** Ordner `web/` (nicht ausgeliefertes React-Experiment) entfernen
- [ ] **P1** Altlasten in `interactive_export.py` entfernen: alte Startseite, alte Coach-Marks, ungenutzte Knöpfe, tote Funktionen
- [ ] **P1** Root aufräumen: `index 2.html`, `Designer (4).png`, `image.png`, `changes needed.md` → `docs/archive/` oder löschen
- [ ] **P1** Gestrichene Features (Kap. 1.2) hinter `FEATURES`-Flags, dann toten Code löschen
- [ ] **P2** Die 15 000-Zeilen-Datei aufteilen: CSS, JS-Module (Karte, Auth, Feed, Touren, Engine) und Übersetzungen als eigene Quelldateien, die der Generator zusammensetzt — erleichtert Reviews und Tests
- [ ] **P2** Linter (`node --check` ✅) um ESLint auf das erzeugte `app.js` erweitern

---

## 11. Übersetzungen (DE/EN/FR/IT)

- [ ] **P1** Skript, das alle sichtbaren deutschen Texte im Generator findet und gegen die Übersetzungstabelle prüft → Liste fehlender Einträge, als CI-Check
- [ ] **P1** Fehlende Texte nachziehen — vor allem die zuletzt neu hinzugekommenen (Onboarding, Face ID, Feed, Touren, Melden, Auth-Fehler)
- [ ] **P1** Supabase-Fehlermeldungen (englisch) auf eigene, übersetzte Texte abbilden
- [ ] **P1** E-Mail-Vorlagen (Code, Reset) mehrsprachig oder neutral zweisprachig
- [ ] **P1** Datums-/Zahlenformate pro Sprache (`Intl`)
- [ ] **P2** Muttersprachler-Review FR + IT (Skitouren-Fachbegriffe: Harsch, Sulz, Triebschnee)

---

## 12. Barrierefreiheit

- [ ] **P1** Kontraste WCAG AA (4.5:1 Text, 3:1 Bedienelemente) — Glas-Elemente über heller Karte besonders prüfen
- [ ] **P1** VoiceOver: jedes Symbol-Element mit `aria-label` (aktuell ~110 vorhanden — Lücken suchen), sinnvolle Lesereihenfolge, Sheets als `role="dialog"` mit Fokus-Falle
- [ ] **P1** Tippflächen ≥ 44×44 pt
- [ ] **P1** Farbe nie einziger Informationsträger (Legende mit Muster/Text)
- [ ] **P1** `prefers-reduced-motion` respektieren, dynamische Schriftgrösse bis 130 %
- [ ] **P2** Automatischer axe-core-Check in Playwright

---

## 13. Sicherheit & Datenschutz

- [ ] **P0** RLS-Audit aller Tabellen: jede Tabelle hat RLS an, Policies schreiben nur eigene Zeilen
- [ ] **P0** Keine Secrets im Client ausser dem Anon-Key; Service-Role nur in Edge Functions
- [ ] **P0** Storage-Bucket `report-images`: EXIF/GPS-Metadaten aus Fotos entfernen vor Upload
- [ ] **P1** `npm audit` / Abhängigkeiten aktualisieren (Capacitor, Supabase-SDK)
- [ ] **P1** Content-Security-Policy für die WebView
- [ ] **P1** Daten-Export (DSG-Auskunftsrecht) funktioniert

---

## 14. Qualitätssicherung & Tests

- [ ] **P0** `validate.yml` grün (Python + JS, Engine↔Pipeline-Parität)
- [ ] **P1** Playwright-Smoke-Test in CI: App lädt, Karte rendert, Layer wechseln, Login-Formular validiert, jeder Screen hat Zurück (Kap. 6)
- [ ] **P1** Screenshot-Tests 4 Viewports × 4 Sprachen (Kap. 4)
- [ ] **P1** Auth-Test-Matrix (Kap. 2.8) manuell auf Gerät, Ergebnis hier abhaken
- [ ] **P1** TestFlight-Gruppe 5–10 echte Tourengänger, Feedback-Kanal (Formular oder Mail)
- [ ] **P2** Powder-Regression mit Cache (`tools/eval_powder.py`) als echtes CI-Gate

---

## 15. Gerätetest auf echtem iPhone (Funktionstest)

Mindestens 2 iPhones, 2 iOS-Versionen inkl. ältester unterstützter (iOS 16.2).

- [ ] Registrieren → Code → Face ID → automatisch angemeldet
- [ ] Login, Abmelden, Passwort zurücksetzen
- [ ] Feed: laden, nachladen, Meldung öffnen, melden, blockieren
- [ ] Profil: bearbeiten, eigene Meldungen, Konto löschen
- [ ] Melden mit **Kamera** und aus der Fotomediathek, mit/ohne Standort
- [ ] Karte: alle 5 Layer, Zeitregler, Touren, offline starten
- [ ] Push: Erlaubnis, Alarm einrichten, Test-Push, Antippen öffnet richtige Stelle
- [ ] Face ID: an/aus, abgebrochen, Gerät ohne Face ID
- [ ] Nur falls nicht gestrichen: **Aufzeichnen mit Hintergrund-GPS** (Akku nach 2 h messen), **Nachrichten**
- [ ] Schlechtes Netz (Berg, Edge), Flugmodus, App im Hintergrund > 1 h
- [ ] Wärme/Akku: 30 min Kartennutzung

---

## 16. Nach dem Launch (vorbereiten, nicht bauen)

- [ ] Support-Mail + Antwortvorlagen
- [ ] Wöchentlich: Sentry, Supabase-Kosten, Moderationsqueue, App-Store-Bewertungen
- [ ] Backlog 1.0.1 / 1.1: Powder-Alarm (falls verschoben), Nachrichten, Aufzeichnen, Folgen, Web-Push, Webcams, 3D
- [ ] In-App-Hinweis «Neue Version verfügbar» für spätere Updates

---

## 17. Reihenfolge / Zeitplan bis Einreichung

| Woche | Fokus |
|---|---|
| **KW 41** (9.–12. Okt) | Kap. 1 entscheiden + Feature-Flags; Live-Daten Default; Supabase-Templates auf Code; SMTP |
| **KW 42** (13.–19. Okt) | Kap. 2 Auth komplett (Code-Reset, Passwort ×2, Sperre, Face ID + Keychain); Kap. 6 Navigations-Stack |
| **KW 43** (20.–26. Okt) | Kap. 5 Performance + Code-Entfernen; Kap. 7 Layer; Kap. 3 Push-Grundgerüst; TestFlight an 5–10 Tester |
| **KW 44** (27. Okt – 2. Nov) | Kap. 4 Design-Pass auf allen Geräten; Auth-Test-Matrix; Tester-Feedback; Store-Metadaten |
| **3. Nov** | **Einreichung App Review** |
| **Nov** | Review-Runden; Powder-Alarm fertigstellen (darf als 1.0.1 nachkommen, falls knapp) |
| **1. Dez** | Release |

**Wenn Zeit knapp wird, in dieser Reihenfolge schieben:** Powder-Alarm → 1.0.1,
Web-Push → später, Querformat → sperren, iPad → «iPhone only».
**Nicht schieben:** Auth fail-safe, Navigation ohne Sackgassen, Live-Daten, Design ohne Überlappungen.

---

## 18. Definition of Done für 1.0

- [ ] Neuer Nutzer kommt ohne Hilfe von Installation bis erster Meldung, auf iPhone SE und Pro Max
- [ ] Kein Auth-Fall aus 2.8 endet in einem Zustand ohne Ausweg
- [ ] Jeder Screen hat einen Zurück-Weg (automatisierter Test grün)
- [ ] Performance-Budgets aus Kap. 5 eingehalten (gemessen, nicht geschätzt)
- [ ] Keine Überlappung/abgeschnittener Text in Screenshot-Tests (4 Viewports × DE/FR/IT)
- [ ] Sentry 7 Tage TestFlight ohne unbehandelte Fehler im Core-Flow
- [ ] Powder-Alarm löst bei Test-Schneefall korrekt genau 1× aus
