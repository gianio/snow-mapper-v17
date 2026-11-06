# Datenschutz nach DSG – Snowmapper

Stand: 2. November 2026. Interne Unterlage für den Betreiber. Die öffentliche Erklärung steht in
`legal/datenschutz.html` (in der App unter Profil › App & Rechtliches).

## 1. Wo welche Daten liegen

| Dienst | Firma / Sitz | Speicherort der Daten | Welche Personendaten | Rolle |
|---|---|---|---|---|
| Supabase (Projekt `gdtxwowcqtbdkcoksivb`) | Supabase Inc., USA | **AWS eu-central-1, Frankfurt (DE)** | Konto (E-Mail, Passwort-Hash), Profil, private Angaben (Vor-/Nachname, Telefon), Gruppe (FF/BETA) und Einladungscode, Beiträge mit Ort, Fotos (Bucket `report-images`), Kommentare, Bestätigungen, Nachrichten, Favoriten, Blockierungen, Push-Abos, Feedback (+ Bildschirmfotos, Bucket `feedback`) | Auftragsbearbeiter |
| GitHub Pages + API | GitHub Inc. (Microsoft), USA | weltweites CDN (Fastly) | IP-Adresse in Server-Protokollen; keine Kontodaten | Auftragsbearbeiter |
| Cloudflare Worker + R2 | Cloudflare Inc., USA | weltweites Netz; R2 enthält nur Modell-/Wetterdaten | IP-Adresse in Protokollen | Auftragsbearbeiter |
| Resend | Plus Five Five Inc., USA | USA | Feedback-Text, Benutzername (Mail ans Team) | Auftragsbearbeiter |
| Gmail (Empfang Feedback) | Google, USA | Google-Rechenzentren | Feedback-Mails | eigenes Postfach |
| swisstopo (Karten, Suche) | Bund, CH | Schweiz | IP-Adresse beim Laden der Kacheln | eigenständig |
| AWS S3 Terrarium (Höhenkacheln) | Amazon, USA | us-east-1 | IP-Adresse (3D, Geländewerte im Punkt-Fenster) | eigenständig |
| Windy Webcams | Windyty SE, CZ | EU | IP-Adresse (nur Webcams) | eigenständig |
| Web-Push | Apple / Google / Mozilla | jeweils eigene | Push-Inhalt + Abo-Adresse | eigenständig |
| App Store / TestFlight / Play | Apple, Google | jeweils eigene | nach deren Bedingungen | eigenständig |

Nicht im Einsatz: Analyse- oder Werbe-Tools, Tracking-Cookies, Sentry (nur mit Secret `SENTRY_DSN`, dann in der Erklärung aufgeführt), SMS-Anbieter
(SMS-Bestätigung aus: `SMS_VERIFY=false`), Stripe (Paywall aus: `PRO_UI=false`). Bibliotheken
(Leaflet, MapLibre, supabase-js, PMTiles) liefert die eigene Seite aus, nicht unpkg/jsDelivr.

## 2. Was technisch umgesetzt ist

- **Zugang nur mit Einladung** (`app_settings.access = 'invite'`): Die Datenbank legt ein Konto nur mit
  gültigem Code an und verbraucht ihn in derselben Transaktion (`handle_new_user`). Jeder Code gilt für
  genau 1 Person (`max_uses = 1`). Erraten wird gebremst (10 Versuche/Stunde, `invite_attempts`).
- **Gruppen-Markierung** in `profiles.cohort`: `ff` (Family & Friends), `beta` (Tester), `team`.
  Abfrage z. B. `select username, cohort, invited_at, invite_code from profiles where cohort = 'ff';`
  Nicht durch die Person selbst änderbar (`profiles_guard`).
- **Private Angaben** (Vorname, Nachname, Telefon) nur in `profile_private` bzw. Auth-Metadaten;
  nur die Person selbst und der Service-Zugang lesen sie.
- **Auskunft/Herausgabe** (Art. 25/28 DSG): `my_data_export()` – Knopf «Meine Daten exportieren».
- **Löschung**: Edge Function `delete-account`, sonst `delete_my_account()` + Fotos über die
  Storage-API. Die Kaskaden entfernen Profil, Beiträge, Kommentare, Nachrichten, Follows, Favoriten,
  Push-Abos, Blockierungen und private Angaben; Feedback bleibt ohne Personenbezug.
- **Fristen** (pg_cron `privacy-retention`, täglich 03:23 UTC): `invite_attempts` 30 Tage, `push_log` 90 Tage.
- **Fotos**: EXIF/GPS wird vor dem Hochladen entfernt; der Bucket ist nicht mehr für alle auflistbar.
- **Zugriffsschutz**: RLS auf allen Tabellen (Ausnahme `spatial_ref_sys` von PostGIS – enthält nur
  Koordinatensysteme, keine Personendaten).

## 3. Was du noch selbst erledigen musst

1. ✅ **Kontakt eingetragen** (Giani Morf, Grüsch) in Datenschutz, AGB und Impressum (6.11.2026). Frist für Sicherungskopien: 30 Tage, bei einem Wechsel des Supabase-Plans prüfen.
2. **Auftragsbearbeitungsverträge (AVV/DPA)** abschliessen bzw. bestätigen:
   - Supabase: Dashboard › Organization › Legal Documents › DPA anfordern und unterschreiben.
   - Resend: DPA unter resend.com/legal/dpa akzeptieren.
   - Cloudflare und GitHub: DPA gilt mit den Nutzungsbedingungen; einmal herunterladen und ablegen.
3. **Zwei-Faktor-Anmeldung** für Supabase, GitHub, Cloudflare, Resend und das Gmail-Konto einschalten.
4. ✅ **Edge Function `delete-account`** ist deployt (5.11.2026).
5. **Telefonnummer**: Ist sie wirklich nötig? Das DSG verlangt, nur so viele Daten zu erheben wie für den
   Zweck nötig. Wenn sie nur für Rückfragen dient, besser freiwillig machen.
6. Vor dem öffentlichen Start: Erklärung von einer Fachperson prüfen lassen.

## 4. Bearbeitungsverzeichnis (Art. 12 DSG)

Für Unternehmen unter 250 Mitarbeitenden ohne Bearbeitungen mit hohem Risiko freiwillig; hier zur Übersicht.

| Bearbeitung | Zweck | Betroffene / Daten | Empfänger | Ausland | Aufbewahrung |
|---|---|---|---|---|---|
| Konto & Anmeldung | App-Nutzung | Nutzende: E-Mail, Passwort-Hash, Name, Telefon | Supabase | DE; USA (DPF/SCC) | bis Kontolöschung |
| Einladung & Gruppe | geschlossene Testphase | Code, Gruppe, Datum, Fehlversuche | Supabase | DE | Konto; Fehlversuche 30 T. |
| Beiträge & Fotos | Community-Schneeinfo | Ort, Zeit, Schneeart, Fotos, Texte | Supabase; andere Nutzende je Sichtbarkeit | DE | bis Löschung |
| Nachrichten | Kommunikation | Text, Zeit, Beteiligte | Supabase | DE | bis Kontolöschung |
| Aufzeichnungen | Aktivitäten | GPS-Spur, Höhe, Zeit, Erschütterung | Supabase; Freunde | DE | bis Löschung |
| Push | Neuschnee-Hinweise | Abo-Adresse, Gerät, Favoriten | Supabase, Push-Dienste | DE; USA | bis Abmeldung; Log 90 T. |
| Feedback & Meldungen | Verbesserung, Moderation | Text, Bildschirmfoto, Benutzername | Supabase, Resend, Gmail | DE; USA | max. 24 Monate |
| Server-Protokolle | Betrieb, Sicherheit | IP, Zeit, Adresse | GitHub, Cloudflare, Supabase, swisstopo, AWS, Windy | USA, CH, EU | nach Anbieter, ≤ 30 T. |

Massnahmen (Art. 8 DSG): HTTPS, RLS, Rollen (`profiles.role = 'admin'` nur Betreiber),
Passwort-Hash, Rate-Limits, Löschfristen, Export- und Löschfunktion, Zwei-Faktor für Admin-Konten.

## 5. Datenpanne – was tun (Art. 24 DSG)

1. Ursache stoppen (Schlüssel rotieren: Supabase › Settings › API; betroffene Funktion abschalten).
2. Festhalten: was, wann, welche Daten, wie viele Personen.
3. Bei voraussichtlich **hohem Risiko** für die Betroffenen: **so rasch als möglich** dem EDÖB melden
   (Online-Formular auf edoeb.admin.ch) und, wenn nötig zu ihrem Schutz, die Betroffenen informieren.
   Bei EU-Betroffenen zusätzlich innert 72 h an die zuständige EU-Behörde.
4. Jeden Vorfall dokumentieren, auch ohne Meldepflicht.

## 6. Auskunftsbegehren

Innert 30 Tagen, kostenlos. Identität über die Konto-E-Mail prüfen. Normalerweise genügt der Hinweis
auf «Meine Daten exportieren» (die Person lädt alles selbst herunter). Kann sie das nicht, im SQL-Editor
die Tabellen aus Abschnitt 1 nach ihrer `id` bzw. `user_id` abfragen und als Datei senden.
