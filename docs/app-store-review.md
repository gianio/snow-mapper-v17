# App Store / TestFlight – Texte zum Einfügen

Stand: 5. November 2026. Alles, was in App Store Connect eingetragen werden muss,
fertig formuliert. Was nur du tun kannst, ist mit ☐ markiert.

## 0. Was bereits erledigt ist

| Punkt | Stand |
|---|---|
| Konto in der App löschen (Regel 5.1.1(v)) | ✅ Funktion `delete-account` ist live (Supabase, 5.11.2026) |
| Begründungstexte Standort, Kamera, Fotos, Face ID | ✅ automatisch im Build |
| Privacy-Manifest `PrivacyInfo.xcprivacy` | ✅ automatisch im Build |
| Verschlüsselung (`ITSAppUsesNonExemptEncryption = NO`) | ✅ |
| Melden und Blockieren von Personen und Beiträgen (Regel 1.2) | ✅ in der App |
| Push nur mit Einwilligung, nie Werbung | ✅ |
| Datenschutzerklärung, AGB, Impressum | ✅ als Seiten, ☐ Platzhalter füllen |
| Automatischer TestFlight-Build nach jedem Merge | ✅ |

## 1. Datenschutz-URL (keine eigene Website nötig)

Die Seiten werden mit der Web-App veröffentlicht:

- Datenschutz: `https://gianio.github.io/snow-mapper-v17/legal/datenschutz.html`
- Nutzungsbedingungen: `https://gianio.github.io/snow-mapper-v17/legal/agb.html`
- Impressum / Support: `https://gianio.github.io/snow-mapper-v17/legal/impressum.html`

☐ Vorher in `legal/datenschutz.html` und `legal/impressum.html` Name, Adresse und
E-Mail eintragen (oder mir schicken, ich trage sie ein). Mit einer eigenen .ch-Domain
später einfach die URLs austauschen.

## 2. TestFlight → Testinformationen (für externe Tester)

**Beta-App-Beschreibung**
> Snowmapper zeigt, wo in den Schweizer Alpen gerade guter Schnee liegt: ein
> Schneedeckenmodell (SNOWPACK, MeteoSchweiz-Prognosen) als Karte für Skiqualität,
> Neuschnee und Schneehöhe, Skitouren mit Höhenprofil und die Meldungen der
> Community. Bitte testet: Karte und Ebenen, Tourensuche, Schnellmeldung mit Foto,
> Aufzeichnen einer Tour, Push-Mitteilungen.

**Was soll getestet werden**
> Neue Funktionen in diesem Build: Push-Mitteilungen, Face ID, Suche nach Orten und
> Skitouren, Teilen eines Beitrags als Bild, Schnellaktionen auf dem App-Symbol.
> Feedback direkt in der App (Sprechblase oben links) oder per Screenshot in TestFlight.

**Feedback-E-Mail:** ☐ deine Adresse
**Marketing-URL:** optional
**Datenschutz-URL:** siehe 1.

**Anmeldedaten für die Beta-Prüfung**
☐ Ein Testkonto anlegen (z. B. `review@…` mit Passwort) und hier eintragen. Solange
die Einladungscodes nicht scharf geschaltet sind, kann sich Apple auch selbst
registrieren; sobald sie es sind, braucht das Testkonto zwingend einen Code.

## 3. Hinweise für die Prüfung (App Review Information → Notes)

> Snowmapper is a snow-conditions map for ski touring in Switzerland. The map works
> without an account; sign-in (email + 6-digit code) is only needed to post reports,
> comment, follow people and receive notifications.
>
> Background location: only while the user records a ski tour (tab "Aufzeichnen",
> button Start). The blue location indicator is visible during recording; recording
> stops with the Stop button. No location is collected in the background otherwise.
>
> User-generated content: every post and profile can be reported ("⋯" → Melden) and
> users can be blocked; reports are reviewed within 24 hours. Terms with a
> zero-tolerance policy for objectionable content: (AGB URL).
>
> Account deletion: Profile → Settings → "Konto & Daten löschen" deletes the account,
> all posts and photos.
>
> The snow information is a model, not an avalanche bulletin; the app says so on
> first start and links to the SLF bulletin.
>
> Demo account: (email / password)

## 4. App-Datenschutz («Datenschutz-Etiketten»)

Tracking: **Nein** (keine Daten für Werbung, kein Datenhandel).

| Datentyp | Erhoben | Mit Person verknüpft | Zweck |
|---|---|---|---|
| Kontaktinfo → E-Mail-Adresse | ja | ja | App-Funktionalität |
| Kontaktinfo → Name | ja | ja | App-Funktionalität |
| Kontaktinfo → Telefonnummer | ja | ja | App-Funktionalität |
| Standort → Genauer Standort | ja | ja | App-Funktionalität |
| Nutzerinhalte → Fotos | ja | ja | App-Funktionalität |
| Nutzerinhalte → Andere Nutzerinhalte (Meldungen, Kommentare, Nachrichten) | ja | ja | App-Funktionalität |
| Kennungen → Geräte-ID (Push-Token) | ja | ja | App-Funktionalität |
| Diagnose → Absturzdaten | nur wenn `SENTRY_DSN` gesetzt | nein | App-Funktionalität |

Nicht erhoben: Gesundheit, Finanzen, Kontakte, Browserverlauf, Suchverlauf
(Suchanfragen gehen an swisstopo, nicht an uns), Käufe.

## 5. Altersfreigabe

Fragebogen: alles «Keine», ausser
- **Nutzergenerierte Inhalte:** Ja (mit Melde- und Blockierfunktion)
- **Unbeschränkter Webzugriff:** Nein

Ergebnis erfahrungsgemäss 12+.

## 6. Store-Text (Kurzfassung)

**Untertitel:** Schnee, Touren & Pulver in der Schweiz
**Schlüsselwörter:** skitour,powder,schnee,neuschnee,lawine,slf,freeride,tourenski,schneehöhe,alpen
**Hinweis im Text:** «Snowmapper zeigt ein Modell und Meldungen der Community. Es
ersetzt nicht das Lawinenbulletin des SLF und keine eigene Beurteilung im Gelände.»

## 7. Family & Friends ohne Apple-Prüfung

| Weg | Wer | Prüfung | Grenze |
|---|---|---|---|
| **Interne Tester** | Personen mit Zugang zu deinem App-Store-Connect-Team (Benutzer und Zugriff → einladen, Rolle z. B. «Kundensupport») | keine | 100 |
| **Öffentlicher TestFlight-Link** (externe Gruppe) | jede Person mit dem Link | einmalige Beta-Prüfung (meist < 24 h), danach Builds sofort | 10 000 |
| **Ad-hoc-Verteilung** | registrierte Geräte (UDID) | keine | 100 Geräte/Jahr, eigenes Profil nötig |
| **Web-App** | alle, sofort | keine | – |

Empfehlung: eine externe Gruppe «Family & Friends» mit **öffentlichem Link**; der Link
kann ein Tester-Limit haben und jederzeit abgeschaltet werden.
