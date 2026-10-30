# Repo privat: Cloudflare Pages + eigener Runner

Ziel: Das Repository kann privat werden, ohne GitHub Pro und ohne bezahlte
Actions-Minuten. Die Webseite liegt dann auf Cloudflare Pages (gratis), die
schweren Jobs laufen auf einem eigenen Server.

| | heute (öffentlich) | privat, nur GitHub | privat, so wie hier |
|---|---|---|---|
| Webseite | GitHub Pages | GitHub Pages, braucht Pro (4 $) | Cloudflare Pages, gratis |
| SNOWPACK, Build, Kacheln (~12 000 min/Monat) | gratis | ca. 60–120 $ | eigener Server, ca. €8–15 |
| PR-Prüfungen, Health-Check | gratis | in den Gratis-Minuten | in den Gratis-Minuten |

Alles ist so gebaut, dass du Schritt für Schritt umstellst und jederzeit
zurück kannst: Ohne die Variablen unten läuft alles wie heute.

## Die Schalter (Repository-Variablen)

GitHub › Repository › Settings › Secrets and variables › Actions › **Variables**

| Variable | Wert | Wirkung |
|---|---|---|
| `CF_PAGES_PROJECT` | z. B. `snowmapper` | jeder Deploy lädt die Seite auch auf Cloudflare Pages |
| `RUNNER` | `self-hosted` | SNOWPACK-Zyklus, Seiten-Build, Kacheln, Variant-A-Läufe gehen auf deinen Server |
| `GH_PAGES` | `off` | kein GitHub-Pages-Deploy mehr (nötig, wenn privat ohne Pro) |
| `APP_URL` | z. B. `https://snowmapper.pages.dev/` | der Health-Check prüft diese Adresse |

Der Health-Check läuft absichtlich immer bei GitHub: Er soll auch melden,
wenn dein Server ausfällt. Die PR-Prüfungen bleiben ebenfalls bei GitHub.

## Schritt 1: Cloudflare Pages

1. Cloudflare › Workers & Pages › Create › Pages › **Direct Upload**, Name
   z. B. `snowmapper`. Oder auf deinem Rechner:
   `npx wrangler pages project create snowmapper --production-branch main`
2. Das bestehende Token `CLOUDFLARE_API_TOKEN` (Secret im Repo) braucht
   zusätzlich die Berechtigung **Cloudflare Pages: Edit** – im Cloudflare-
   Dashboard › My Profile › API Tokens › das Token bearbeiten.
3. Variable `CF_PAGES_PROJECT = snowmapper` setzen.
4. Actions › «Build and deploy snow model» › Run workflow. Danach läuft die
   App auch unter `https://snowmapper.pages.dev/`. GitHub Pages läuft parallel
   weiter, bis du umstellst.

Grenzen von Pages: 20 000 Dateien, 25 MiB pro Datei. Der Deploy prüft beides
vorher und bricht mit einer klaren Meldung ab, statt halb hochzuladen.

## Schritt 2: eigener Server mit Runner

1. Hetzner Cloud › neuer Server: **Ubuntu 24.04, x86**, mindestens
   4 vCPU / 8 GB / 80 GB (z. B. CPX31), mit deinem SSH-Schlüssel.
2. GitHub › Settings › Actions › Runners › **New self-hosted runner** › das
   Token kopieren (gilt eine Stunde).
3. Auf dem Server als root:

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/gianio/snow-mapper-v17/main/tools/runner/setup-runner.sh
   RUNNER_TOKEN=<Token> bash setup-runner.sh
   ```

   (Ist das Repo schon privat, die Datei per `scp` kopieren statt per curl.)
   Das Skript installiert Build-Werkzeuge, GitHub CLI, Sicherheits-Updates,
   Firewall, 4 GB Swap und den Runner als Dienst, und räumt alte
   Arbeitsordner wöchentlich weg.
4. Variable `RUNNER = self-hosted` setzen.
5. Testen: Actions › «Variant A live» › Run workflow. Der erste Lauf baut
   SNOWPACK einmal (wenige Minuten), danach kommt es aus dem Cache.

Zurück: Variable `RUNNER` löschen – alles läuft wieder bei GitHub.

## Schritt 3: die Adresse umstellen (nur wenn Pages die Hauptadresse wird)

Die alte Adresse `https://gianio.github.io/snow-mapper-v17/` steht noch hier:

- `supabase/functions/fav-alerts/index.ts` und `push-notify/index.ts` (`APP`) –
  danach die Funktionen neu deployen
- Supabase › Authentication › URL Configuration: **Site URL** und
  **Redirect URLs** um die neue Adresse ergänzen (sonst gehen Login-Links ins Leere)
- `pipeline/interactive_export.py`: die Rückfall-Adresse für Links aus der
  iOS-App (Teilen, Anmelden)
- `.github/workflows/android-build.yml`, `ios-testflight.yml`: `remote_data_base`
- Windy-Webcams: die neue Domain im Windy-Key erlauben
- Variable `APP_URL` für den Health-Check
- Eine eigene Domain (z. B. `snowmapper.ch`) in Cloudflare Pages › Custom
  domains macht künftige Umzüge unnötig.

## Schritt 4: privat schalten

GitHub › Settings › General › Danger Zone › **Change visibility** › Private.
Ohne GitHub Pro danach `GH_PAGES = off` setzen. Die Kacheln (Worker + R2),
Supabase und die Apps sind davon nicht betroffen.
