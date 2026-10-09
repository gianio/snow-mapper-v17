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

---

## 0. Ausgangslage (aus dem Code geprüft)

| Thema | Ist-Zustand | Handlungsbedarf |
|---|---|---|
| Live-Daten | Boot-Loader setzt `isDemo=true` (Z. ~1115) — App startet auf Demo-Datensatz | **P0** Default umdrehen |
| Registrierung | Passwort nur **einmal** eingeben, kein Wiederholen-Feld | **P0** |
| Bestätigung | 6-stelliger Code via `verifyOtp` ✅, hängt aber am Supabase-Template | **P0** Template prüfen |
| Passwort zurücksetzen | **Link**-basiert (`resetPasswordForEmail` + `redirectTo`) — in der iOS-WebView fragil | **P0** auf Code umbauen |
| Rate-Limit Login | Kein clientseitiges Sperren nach Fehlversuchen | **P0** |
| Face ID | Als **App-Sperre** vorhanden, aber kein Speichern des Passworts / Auto-Login per Face ID | **P0** |
| Layer | 16 Layer/Ansichten (Basis + 9 Skiqualität-Unteransichten) | **P1** reduzieren |
| Tabs | Karte · Touren · Aufzeichnen · Feed · Melden | **P1** reduzieren |
| Navigation | Kein `history.pushState`/Zurück-Stack — Sheets haben keine einheitliche Zurück-Logik | **P0** |
| Push / Powder-Alarm | Nicht vorhanden | **P1** (neu im Scope) |
| Generator | `pipeline/interactive_export.py` = 16 000 Zeilen, eine Datei | Abspecken = auch Code löschen |

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
- [ ] Felder: E-Mail, Benutzername, **Passwort**, **Passwort wiederholen**
- [ ] Live-Validierung: E-Mail-Format, Passwort ≥ 8 Zeichen, beide gleich, Benutzername frei (Check vor Absenden)
- [ ] Passwort anzeigen/verbergen (Auge-Icon)
- [ ] `autocomplete="new-password"` / `username` korrekt → iOS-Schlüsselbund schlägt starkes Passwort vor und **speichert es**
- [ ] Doppelklick-Schutz (Button deaktivieren während Request)
- [ ] Bereits registrierte E-Mail → klare Meldung + direkt «Anmelden» / «Passwort vergessen» anbieten

### 2.3 E-Mail-Bestätigung per Code (kein Link!)
- [ ] **Supabase → Auth → Email Templates → «Confirm signup»:** nur `{{ .Token }}` (6-stelliger Code), **keinen** `{{ .ConfirmationURL }}`
- [ ] Template **«Reset password»** ebenfalls auf `{{ .Token }}` umstellen
- [ ] Template **«Magic Link»** / «Change email» prüfen (gleich)
- [ ] Eigener SMTP-Absender (z. B. Resend/Postmark) — Supabase-Standard-SMTP ist auf wenige Mails/h limitiert → **P0 für Launch**
- [ ] Code-Eingabe: 6 Einzelfelder oder ein Feld mit `autocomplete="one-time-code"` + `inputmode="numeric"` (iOS füllt Code aus Mail-App vor)
- [ ] Code einfügen aus Zwischenablage funktioniert
- [ ] «Code erneut senden» mit **60-s-Countdown**
- [ ] «Falsche E-Mail? Ändern» → zurück zum Formular, Eingaben bleiben erhalten
- [ ] App geschlossen während Code-Schritt → beim nächsten Start direkt wieder im Code-Schritt
- [ ] Code abgelaufen → klare Meldung + neuer Code

### 2.4 Nach Bestätigung: Face ID + Passwort speichern
- [ ] Direkt nach erfolgreicher Bestätigung automatisch angemeldet (Session aus `verifyOtp`)
- [ ] Angebot: **«Mit Face ID anmelden?»** — falls `BiometricAuth.checkBiometry()` verfügbar; sonst Schritt still überspringen
- [ ] Credentials sicher speichern: **iOS Keychain** (Capacitor `@capacitor-community/secure-storage` o. ä.) — **niemals** `localStorage`
- [ ] Speichern: Supabase **Refresh-Token** im Keychain, geschützt mit Biometrie (`kSecAccessControlBiometryCurrentSet`) — nicht das Klartext-Passwort
- [ ] Zusätzlich iOS-Passwort-Autofill nutzen (Schlüsselbund speichert E-Mail/Passwort selbst) → Associated Domains `webcredentials:` einrichten
- [ ] Web/PWA-Fallback: kein Face ID, Session bleibt via Supabase-Persistenz

### 2.5 Wiederkehrender Nutzer — automatisch erkannt
- [ ] App-Start: gültige Session → direkt Karte, kein Login-Screen
- [ ] Session abgelaufen + Face ID aktiv → Face ID-Prompt → Refresh-Token aus Keychain → angemeldet
- [ ] Face ID abgebrochen/fehlgeschlagen → Fallback auf Passwort-Login (E-Mail vorausgefüllt)
- [ ] Face ID auf Gerät geändert (neues Gesicht) → Keychain-Eintrag ungültig → sauber auf Passwort zurückfallen
- [ ] Refresh-Token serverseitig widerrufen → sauber abmelden, kein Endlos-Loop

### 2.6 Passwort zurücksetzen — mit Code, alle Schritte
- [ ] Schritt 1: E-Mail eingeben → `resetPasswordForEmail(email)` **ohne** `redirectTo`
- [ ] Schritt 2: 6-stelligen Code eingeben → `verifyOtp({email, token, type:'recovery'})`
- [ ] Schritt 3: neues Passwort **zweimal** → `updateUser({password})`
- [ ] Schritt 4: automatisch angemeldet; Face ID-Keychain mit neuem Token aktualisieren
- [ ] Unbekannte E-Mail → **gleiche** Erfolgsmeldung zeigen (kein Konto-Enumerieren)
- [ ] Jeder Schritt hat «Zurück» und «Abbrechen»
- [ ] Alten Link-Pfad (`authOnRecovery`, `type=recovery` im Hash) als Fallback behalten, bis alle alten Mails abgelaufen sind

### 2.7 Brute-Force-Schutz: mehr als 3 Fehlversuche → 5 Min. Sperre
- [ ] **Clientseitig:** Zähler pro E-Mail für Login, Code-Eingabe und Reset; nach 3 Fehlern Button gesperrt mit sichtbarem Countdown «Erneut versuchen in 4:59»; Zustand überlebt App-Neustart
- [ ] **Serverseitig (wichtig — Client allein ist umgehbar):** Supabase → Auth → Rate Limits setzen (Sign-in/OTP-Verify, E-Mails/h); zusätzlich `auth_attempts`-Tabelle + Edge Function oder Auth-Hook, der nach 3 Fehlern/5 Min. pro E-Mail **und** IP blockt
- [ ] CAPTCHA (hCaptcha/Turnstile, in Supabase integriert) bei Registrierung und Reset aktivieren
- [ ] Gleiche Regel für «Code erneut senden» (max. 3 pro 5 Min.)

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

## 8. Release-Blocker aus dem bestehenden Launch-Plan (noch offen)

- [ ] **P0** Live-Daten als Standard (`isDemo=false`), Demo nur versteckt
- [ ] **P0** Migrationen via Supabase CLI (`link`, `migration repair`, `db push`) — `harden_functions` + `harden_functions_2` prüfen
- [ ] **P0** Edge Function `delete-account` deployt + getestet
- [ ] **P0** Apple Developer Program aktiv
- [ ] **P0** Datenschutzerklärung + Support-URL gehostet (inkl. Push + Face ID)
- [ ] **P0** App-Privacy-Labels = tatsächliches Verhalten
- [ ] **P0** Demo-Konto für App Review + Review-Notizen
- [ ] **P0** Supabase-Backups aktiv, Plan-Limits (Storage/Egress) geprüft
- [ ] **P1** Sentry-DSN gesetzt
- [ ] **P1** Monitoring: Alarm, wenn `data/latest.json` > 12 h alt
- [ ] **P1** Store-Metadaten + Screenshots (6.7", 6.5")
- [ ] **P1** Native Integrationen sichtbar (Guideline 4.2): Face ID, Push, Haptik, Teilen, Offline

---

## 9. Reihenfolge / Zeitplan bis Einreichung

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

## 10. Definition of Done für 1.0

- [ ] Neuer Nutzer kommt ohne Hilfe von Installation bis erster Meldung, auf iPhone SE und Pro Max
- [ ] Kein Auth-Fall aus 2.8 endet in einem Zustand ohne Ausweg
- [ ] Jeder Screen hat einen Zurück-Weg (automatisierter Test grün)
- [ ] Performance-Budgets aus Kap. 5 eingehalten (gemessen, nicht geschätzt)
- [ ] Keine Überlappung/abgeschnittener Text in Screenshot-Tests (4 Viewports × DE/FR/IT)
- [ ] Sentry 7 Tage TestFlight ohne unbehandelte Fehler im Core-Flow
- [ ] Powder-Alarm löst bei Test-Schneefall korrekt genau 1× aus
