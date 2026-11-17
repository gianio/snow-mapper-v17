# Runner

Dieses Repository enthält **keinen Code**, nur Workflows. Sie holen den Code aus
einem privaten Repository (Variable `CODE_REPO`) mit einem Lese-Token und laufen
hier, weil öffentliche Repositories unbegrenzt Gratis-Minuten haben.

Die Workflows werden erzeugt (`tools/runner_repo/make_runner.py` im Code-Repo) –
hier nichts von Hand ändern.

## Einstellungen

**Variables** (Settings › Secrets and variables › Actions › Variables)

| Name | Wert |
|---|---|
| `CODE_REPO` | `owner/name` des privaten Code-Repos |
| `RUN_REPO` | `owner/name` dieses Repos (für den Wartungs-Hinweis in der App) |
| `APP_URL` | öffentliche Adresse der App, z. B. `https://app.example.ch/` |
| `CF_PAGES_PROJECT` | Name des Cloudflare-Pages-Projekts |
| `GH_PAGES` | `off` |

**Secrets**: `CODE_TOKEN` (fine-grained, nur das Code-Repo: *Contents: read*,
*Commit statuses: read & write*) und alle Secrets, die die Workflows schon
bisher brauchten (Cloudflare, R2, Sentry, Windy, Android, Apple).

**Settings › Actions › General**: «Fork pull request workflows» → *Require approval
for all outside collaborators*; Artefakte, die Code oder App-Builds enthalten,
werden hier nicht hochgeladen.
