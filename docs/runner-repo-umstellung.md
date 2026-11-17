# Repo privat: Abläufe in einem öffentlichen Runner-Repo

Ziel: Dieses Repository (Code) wird **privat**. Die schweren Abläufe (Daten-Zyklus,
Seiten-Build, Kacheln, App-Builds – rund 15 000 Minuten im Monat) laufen in einem
**öffentlichen Runner-Repo**, wo GitHub-Minuten gratis sind. Das Runner-Repo enthält
keinen Code, nur erzeugte Workflows; sie holen den Code mit einem Lese-Token.
Die App läuft auf **deiner Domain** über Cloudflare Pages, nicht mehr auf
`github.io`.

```
privat: Code-Repo ──push/PR──▶ relay.yml ──repository_dispatch──▶ öffentlich: Runner-Repo
                                                                   ├─ deploy → Cloudflare Pages (deine Domain), R2
   ◀── Status (PR grün/rot) ────────────────────────────────────── ├─ Variant A, Kacheln, Health
                                                                   └─ Android / iOS → Play / TestFlight
```

Bleibt im Code-Repo: `validate.yml` (PR-Prüfung, ~2 min) und `relay.yml` (Sekunden).

## Was öffentlich sichtbar ist

- Die Workflow-Dateien und die **Logs** der Läufe (Build-Ausgaben, Dateinamen).
- Artefakte der Läufe (z. B. Modell-Daten). **Nicht** hochgeladen werden dort:
  Xcode-Projekt (enthält Quellcode), IPA, AAB, APK.
- Secrets bleiben geheim (auch in öffentlichen Repos nie lesbar).
- Der Code selbst nicht.

## Schritt 1 – vorbereiten (du)

1. **Öffentliches Runner-Repo** anlegen, neutraler Name, leer (z. B. `gianio/<name>`).
2. **Domain**: bei Cloudflare registrieren oder dorthin übertragen
   (Cloudflare › Domain Registration), z. B. `example.ch`.
3. **Cloudflare Pages**: Workers & Pages › Create › Pages › *Direct Upload*, Name
   z. B. `snowmapper` → *Custom domains* › `app.example.ch` (oder die Hauptdomain).
   Das Token `CLOUDFLARE_API_TOKEN` braucht zusätzlich *Cloudflare Pages: Edit*.
4. **Zwei Tokens** (GitHub › Settings › Developer settings › Fine-grained tokens):
   - `CODE_TOKEN` – nur das **Code-Repo**: *Contents: Read*, *Commit statuses: Read and write*
     (für die opt-in-Veröffentlichung von Variant A zusätzlich *Contents: Read and write*).
   - `RUNNER_TOKEN` – nur das **Runner-Repo**: *Contents: Read and write* (für `repository_dispatch`).

## Schritt 2 – Runner-Repo füllen (ich)

`python tools/runner_repo/make_runner.py --out <checkout des Runner-Repos>` erzeugt die
Workflows und das README; ich pushe sie.

## Schritt 3 – Einstellungen

**Runner-Repo** › Settings › Secrets and variables › Actions

| Variable | Wert |
|---|---|
| `CODE_REPO` | `gianio/snow-mapper-v17` |
| `RUN_REPO` | `gianio/<runner>` |
| `APP_URL` | `https://app.example.ch/` |
| `CF_PAGES_PROJECT` | `snowmapper` |
| `GH_PAGES` | `off` |
| `RUNNER` | nur falls ein eigener Runner genutzt wird |

Secrets: `CODE_TOKEN` + **alle** Secrets aus dem Code-Repo neu eintragen (GitHub
zeigt Secret-Werte nie an – aus deinen Unterlagen): `CLOUDFLARE_API_TOKEN`,
`CLOUDFLARE_ACCOUNT_ID`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`GH_DISPATCH_TOKEN`, `WINDY_WEBCAMS_KEY`, `SENTRY_DSN`, `ANDROID_*`,
`PLAY_SERVICE_ACCOUNT_JSON`, `APPLE_*`, `APPSTORE_API_KEY_JSON`.

Settings › Actions › General: *Fork pull request workflows from outside collaborators*
→ **Require approval for all outside collaborators**.

**Code-Repo**: Variable `RUN_REPO = gianio/<runner>`, `APP_URL = https://app.example.ch/`,
Secret `RUNNER_TOKEN`.

## Schritt 4 – Adresse umstellen

- **Supabase** › Edge Functions › Secrets: `APP_URL = https://app.example.ch/`
  (fav-alerts, push-notify, stripe-checkout lesen sie), Funktionen neu deployen.
- **Supabase** › Authentication › URL Configuration: *Site URL* und *Redirect URLs*
  um die neue Adresse ergänzen.
- **Cloudflare-Worker** (Kacheln): Variable `GH_REPO = gianio/<runner>`.
- **Windy**: die neue Domain im Webcam-Key erlauben.
- **Apps**: der nächste Android-/iOS-Build holt Daten von `APP_URL`; Datenschutz-
  und Support-Links in Play Console / App Store Connect auf die neue Domain.

## Schritt 5 – umschalten

1. Im Runner-Repo: *Actions › Build and deploy snow model › Run workflow* – die App
   muss unter der neuen Domain laufen.
2. Im Code-Repo die verschobenen Workflows stilllegen:
   `git mv .github/workflows/{deploy,cloudflare,variant-a-live,variant-a,variant-a-season,android-build,ios-testflight,swift-build,health,probe-ogd,probe-upstreams}.yml tools/runner_repo/workflows/`
   (der Generator liest sie dort weiter).
3. Einen PR testen: `validate` läuft im Code-Repo, `runner/…`-Status kommt vom Runner.
4. Alte Adresse: GitHub Pages im Code-Repo abschalten; für eine Übergangszeit kann
   `gianio.github.io/snow-mapper-v17` auf die neue Domain weiterleiten (eine kleine
   `index.html` mit Redirect – erst dann Repo privat stellen, weil Pages danach endet).
5. **Erst wenn die neuen App-Versionen live sind:** Die Apps im Store holen ihre Daten
   fest von der alten Adresse (`remote_data_base` zur Build-Zeit). Endet GitHub Pages,
   bekommen alte Installationen keine frischen Daten mehr. Also: neue Builds mit
   `APP_URL` veröffentlichen, ein paar Wochen warten (oder in Play/App Store ein
   Mindest-Update erzwingen), dann
6. **Code-Repo auf privat stellen.**

Zurück: Variable `RUN_REPO` im Code-Repo löschen und die Workflows zurück nach
`.github/workflows/` verschieben.

## Nach Änderungen an den Workflows

Original in `tools/runner_repo/workflows/` ändern, dann
`python tools/runner_repo/make_runner.py --out <runner-checkout>` und dort pushen.
