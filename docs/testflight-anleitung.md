# Erste TestFlight-Version – Anleitung für heute Abend

Alles Technische ist vorbereitet: Der Workflow **«iOS build (TestFlight)»** baut die
App auf einem Mac von GitHub, signiert sie, setzt Version und Build-Nummer und
lädt sie zu Apple hoch. Du brauchst **keinen eigenen Mac**. Offen sind nur die
Schritte, die an deinem Apple-Konto hängen.

Zeitbedarf: etwa 60–90 Minuten, davon viel Warten.

---

## 0. Vorher (5 Min.)

- [ ] PR gianio/snow-mapper-v17#120 mergen (GitHub → Pull requests → #120 → «Merge»).
      Danach läuft `deploy` automatisch. Die App lädt ihre Prognosen später von dort.
- [ ] Prüfen, dass das Apple Developer Program aktiv ist:
      <https://developer.apple.com/account> zeigt «Membership: Active».

## 1. App-ID registrieren (5 Min.)

developer.apple.com → **Certificates, IDs & Profiles** → **Identifiers** → **+**

- Typ: **App IDs** → **App**
- Description: `Snowmapper`
- Bundle ID: **Explicit** → `ch.snowmapper.app`
- Capabilities: nichts zusätzlich anhaken (Push kommt später)
- **Continue → Register**

## 2. Distribution-Zertifikat (10 Min.)

Du brauchst am Ende eine **.p12-Datei** und ihr Passwort.

**Mit Mac:**
1. Schlüsselbundverwaltung → Menü *Schlüsselbundverwaltung* → *Zertifikatsassistent*
   → *Zertifikat einer Zertifizierungsinstanz anfordern…* → E-Mail eintragen,
   «Auf der Festplatte sichern» → `CertificateSigningRequest.certSigningRequest`.
2. developer.apple.com → **Certificates** → **+** → **Apple Distribution** →
   die CSR-Datei hochladen → Zertifikat herunterladen und doppelklicken.
3. Im Schlüsselbund unter *Meine Zertifikate* «Apple Distribution: …» rechts klicken
   → **Exportieren** → Format .p12 → ein Passwort setzen → `distribution.p12`.

**Ohne Mac (Terminal unter Linux oder Windows mit Git Bash):**
```bash
openssl genrsa -out dist.key 2048
openssl req -new -key dist.key -out dist.csr -subj "/emailAddress=DEINE@MAIL/CN=Snowmapper/C=CH"
# dist.csr bei developer.apple.com → Certificates → + → Apple Distribution hochladen,
# distribution.cer herunterladen, dann:
openssl x509 -inform DER -in distribution.cer -out dist.pem
openssl pkcs12 -export -legacy -inkey dist.key -in dist.pem -out distribution.p12   # Passwort setzen
```

## 3. Provisioning Profile (5 Min.)

developer.apple.com → **Profiles** → **+**

- **Distribution → App Store Connect** → Continue
- App ID: `ch.snowmapper.app` → Continue
- Zertifikat: das eben erstellte **Apple Distribution** → Continue
- Name: `Snowmapper AppStore` → **Generate** → herunterladen
  (`Snowmapper_AppStore.mobileprovision`)

## 4. App in App Store Connect anlegen (5 Min.)

<https://appstoreconnect.apple.com> → **Apps** → **+** → **Neue App**

- Plattform: **iOS**
- Name: `Snowmapper` (falls vergeben: `Snowmapper CH` oder `Snowmapper – Pulver`)
- Primäre Sprache: **Deutsch**
- Bundle-ID: `ch.snowmapper.app` auswählen
- SKU: `snowmapper-ios`
- Zugriff: Voller Zugriff → **Erstellen**

## 5. API-Schlüssel für den Upload (5 Min.)

App Store Connect → **Benutzer und Zugriff** → **Integrationen** → **App Store Connect API**
→ **Team-Schlüssel** → **+**

- Name: `GitHub TestFlight`, Zugriff: **App-Manager** → **Generieren**
- **Schlüssel-ID** und oben die **Aussteller-ID (Issuer ID)** notieren
- **API-Schlüssel herunterladen** (`AuthKey_XXXX.p8`). Das geht nur **einmal**.

## 6. Vier Secrets bei GitHub eintragen (10 Min.)

GitHub → Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Die Base64-Texte erzeugst du so (Mac/Linux: Terminal, Windows: Git Bash):
```bash
base64 -i distribution.p12 | tr -d '\n' > p12.txt                       # Mac
base64 -w0 distribution.p12 > p12.txt                                    # Linux / Git Bash
base64 -i Snowmapper_AppStore.mobileprovision | tr -d '\n' > profile.txt # Mac
base64 -w0 Snowmapper_AppStore.mobileprovision > profile.txt             # Linux / Git Bash
```

| Name | Inhalt |
|---|---|
| `APPLE_CERTIFICATE_P12` | Inhalt von `p12.txt` |
| `APPLE_CERTIFICATE_PASSWORD` | das Passwort aus Schritt 2 |
| `APPLE_PROVISIONING_PROFILE` | Inhalt von `profile.txt` |
| `APPSTORE_API_KEY_JSON` | siehe unten |

`APPSTORE_API_KEY_JSON` als **eine** Zeile JSON. Den Schlüssel mit `\n` statt
echten Zeilenumbrüchen schreiben:
```json
{"key_id":"ABC123XYZ9","issuer_id":"69a6de7f-....-....-....-............","key":"-----BEGIN PRIVATE KEY-----\nMIGT...\n...\n-----END PRIVATE KEY-----"}
```
Mit dem folgenden Befehl entsteht diese Zeile automatisch:
```bash
python3 -c "import json;print(json.dumps({'key_id':'ABC123XYZ9','issuer_id':'DEINE-ISSUER-ID','key':open('AuthKey_ABC123XYZ9.p8').read()}))"
```

Team-ID und Profilname liest der Workflow selbst aus dem Profil, die musst du
nicht eintragen.

## 7. Bauen und hochladen (30–40 Min., meist Warten)

GitHub → **Actions** → links **«iOS build (TestFlight)»** → **Run workflow**

1. **Erst der Probelauf:** Branch `main`, `upload` **aus** → Run.
   Prüft die ganze Kette ohne Signatur (ca. 20 Min.). Muss grün werden.
2. **Dann der echte Lauf:** Branch `main`, `upload` **an**, `version` `1.0.0`,
   `build_number` leer lassen → Run.
   - Grün = hochgeladen. In der Zusammenfassung steht «Uploaded to App Store Connect».
   - Die signierte `.ipa` liegt zusätzlich unter *Artifacts* → `snowmapper-ipa`. Falls
     der Upload-Schritt scheitert, kannst du sie auf einem Mac mit Apples App
     **Transporter** hochladen.

## 8. Auf dem iPhone testen (15 Min.)

1. App Store Connect → deine App → **TestFlight**. Nach 10–30 Min. erscheint der
   Build «1.0.0 (Laufnummer)». Die Exportfrage ist schon beantwortet
   (`ITSAppUsesNonExemptEncryption = NO`), der Build ist sofort testbar.
2. **Interne Tests** → Gruppe anlegen → dich selbst (und bis zu 100 Teammitglieder)
   hinzufügen. Interne Tests brauchen **keine** Apple-Prüfung.
3. Auf dem iPhone die App **TestFlight** aus dem App Store laden → Einladung annehmen
   → **Snowmapper** installieren.

Für **externe** Tester (Freunde ohne App-Store-Connect-Zugang) braucht es eine
kurze Beta-Prüfung von Apple. Vorher in App Store Connect ausfüllen:
Testinformationen, Feedback-E-Mail, Datenschutz-URL und ein Demo-Konto.

---

## Wenn etwas rot wird

| Fehlermeldung im Log | Ursache | Lösung |
|---|---|---|
| `a signing secret is missing` | Secret fehlt oder ist falsch benannt | Namen in Schritt 6 prüfen |
| `profile is for '…', not ch.snowmapper.app` | falsches Profil hochgeladen | Profil aus Schritt 3 nehmen |
| `No signing certificate "iOS Distribution" found` / `doesn't include signing certificate` | Profil gehört zu einem anderen Zertifikat | Profil mit dem Zertifikat aus Schritt 2 neu erzeugen, Secret ersetzen |
| `MAC verification failed` / `.p12` lässt sich nicht importieren | falsches Passwort oder p12 ohne `-legacy` erzeugt | Passwort prüfen oder p12 mit `-legacy` neu exportieren |
| `The bundle version must be higher` | Build-Nummer schon benutzt | im Feld `build_number` eine höhere Zahl angeben |
| `Authentication failed` beim Upload | API-Schlüssel-JSON falsch | `key_id`, `issuer_id` und `\n` im Schlüssel prüfen |

## Noch vor der App-Store-Einreichung (nicht für TestFlight nötig)

- Supabase: E-Mail-Vorlagen «Confirm signup» und «Reset password» nur mit `{{ .Token }}`
- Datenschutzerklärung, Impressum und Support-URL online
- App-Privacy-Angaben in App Store Connect
- Hintergrund-Standort: Die App zeichnet Touren auch bei gesperrtem Bildschirm auf.
  Apple fragt bei der Prüfung danach, deshalb in den Review-Notizen erklären
  (Aufzeichnen einer Skitour, blaue Standortanzeige sichtbar).
