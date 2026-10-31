# MeteoSchweiz statt Open-Meteo: eigenes Wetterarchiv

## Warum

Die **Gratis-API von Open-Meteo ist nur für nicht-kommerzielle Nutzung**
erlaubt. Für ein Abo bräuchte es den kostenpflichtigen Plan (ab ca. 29 USD pro
Monat). **MeteoSchweiz Open Government Data (OGD)** ist frei, auch kommerziell.
Bedingung ist nur die Quellenangabe «Quelle: MeteoSchweiz». Der Haken: OGD
hält nur die neusten Modellläufe bereit, kein Archiv. SNOWPACK braucht aber
die vergangenen Tage. Deshalb bauen wir das Archiv selbst.

## Was jetzt läuft

| Teil | Woher | Datei |
|---|---|---|
| Prognose (nächste 33 h CH1, 120 h CH2) | MeteoSchweiz OGD | `tools/ogd_extract.py` |
| Vergangene Tage | **eigenes Archiv** (MeteoSchweiz), Open-Meteo nur noch für fehlende Stunden | `pipeline/wx_archive.py` |
| Bewölkung auf der Karte | MeteoSchweiz OGD | `pipeline/ogd_clouds.py` |
| Wetter-Ebenen der Web-App (Temperatur, Wind, Niederschlag → Neuschnee, Sonne) | **MeteoSchweiz**: Archiv für die vergangenen Tage, neuster ICON-CH-Lauf für die Prognose | `pipeline/ogd_site.py` |
| Höhenwind 850/700 hPa (Karte), Höhenprofil 850/700/500 hPa (SNOWPACK) | noch Open-Meteo | `pipeline/interactive_export.py`, `variant_a/forcing.py` |

Jeder Live-Lauf (`variant-a-live.yml`) läuft so ab:

1. Archiv holen: zuerst aus dem Actions-Cache, dann die letzten 21 Tage aus R2.
2. OGD-Punktreihen ziehen: die SNOWPACK-Punkte und die 300 Wetterpunkte der
   Web-App (`tools/site_points.py`, IDs `s0000` …). Die Web-App-Punkte gehen
   als Artefakt `ogd-site` an den Site-Build.
3. `tools/wx_archive.py add`: die Stunden 1–24 des Laufs pro Wetterpunkt in
   Tagesdateien ablegen (`ogd/YYYY/MM/DD.json.gz`). Gibt es für eine Stunde
   zwei Läufe, gewinnt der mit der kürzeren Vorlaufzeit. So wird jeder Tag
   eine Kette kurzer Prognosen, fast eine Analyse.
4. SNOWPACK-Lauf mit `--archive-dir`: Archivstunden ersetzen Open-Meteo in der
   Vergangenheit, mit derselben gemessenen Höhenkorrektur wie die OGD-Prognose.
5. Archiv nach R2 hochladen und im Cache sichern. Die Job-Zusammenfassung
   zeigt die Abdeckung der letzten 7 Tage.

Nach etwa zwei Wochen Betrieb kommen die Vergangenheitstage des
SNOWPACK-Fensters vollständig von MeteoSchweiz.

Der **Site-Build** (`deploy.yml`) holt das Artefakt `ogd-site` desselben
Laufs und die letzten 12 Archivtage (Cache, dann R2). `pipeline/ogd_site.py`
legt pro Wetterpunkt zuerst die Archivstunden und darüber die neuste
Prognose auf die Open-Meteo-Reihe:

- **Temperatur:** ICON rechnet auf der Höhe seiner Modellzelle. Die
  Verschiebung auf die Höhe des Wetterpunkts wird pro Punkt gemessen: der
  mittlere Unterschied über die Stunden, die beide Quellen haben (mindestens
  6, sonst keine Verschiebung).
- **Neuschnee:** Niederschlag × Schneeanteil (ganz Schnee bis 0,5 °C, ganz
  Regen ab 2 °C, dazwischen linear) × 0,7 cm/mm, gleich wie Open-Meteo.
- **Sonne:** aus der Globalstrahlung als Anteil am klaren Himmel.
- **Wind** 10 m: direkt.

Das Build-Log zeigt, wie viele Punkte und Stunden von MeteoSchweiz kamen
(`[INT] Wetter: MeteoSchweiz …`). Fehlt beides (erster Lauf, Fehler), bleibt
alles bei Open-Meteo.

## Wo das Archiv liegt: Cloudflare R2

**Empfehlung: Cloudflare R2**, im gleichen Konto wie die Kachel-Worker.

- **Keine Egress-Gebühren.** Abrufe kosten nichts extra, auch nicht aus
  GitHub Actions, einem eigenen Runner oder später einem Server.
- **Preis:** ca. 0.015 USD pro GB und Monat, 10 GB gratis. Das Archiv wächst um
  geschätzt 100–200 MB pro Jahr, liegt also lange im Gratisbereich.
- **Unabhängig von GitHub.** Wechselt ihr zu einem privaten Repo, einem eigenen
  Runner oder weg von GitHub, bleibt das Archiv, wo es ist. Es ist ein normaler
  S3-Speicher: `aws s3 sync` und jedes S3-Werkzeug funktionieren.
- Alternativen: Backblaze B2 (ähnlich günstig), Infomaniak Swiss Backup /
  Object Storage (Daten in der Schweiz, etwas teurer), AWS S3 (Egress kostet).
  Das Repo selbst ist nicht geeignet, weil es mit jedem Lauf wachsen würde.

### Einrichten (einmalig, ca. 5 Minuten)

Die R2-Secrets der Kacheln reichen schon: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`
und `R2_SECRET_ACCESS_KEY`. Das Archiv liegt dann im Kachel-Bucket unter
`wx-archive/`. Für einen eigenen Bucket legst du ihn in Cloudflare an
(z. B. `snowmapper-archive`) und setzt die Repo-Variable
`R2_ARCHIVE_BUCKET`. Ohne Secrets läuft das Archiv nur im Actions-Cache. Der
Cache hält nur rund 45 Tage und verfällt, wenn eine Woche lang kein Lauf kommt.

Sicherheit: `push` lädt nur hoch, wenn im gleichen Lauf vorher `pull`
geklappt hat. Eine unvollständige Tagesdatei überschreibt so nie eine
vollständige.

## Was noch fehlt, um Open-Meteo ganz abzulösen

1. **Höhenprofil:** Temperatur auf 850/700/500 hPa (oder T auf
   Modellflächen + HHL) zusätzlich aus OGD ziehen und archivieren. Dann
   braucht `_profile_dT` kein Open-Meteo mehr.
2. **Höhenwind der Karte** (850/700 hPa) kommt noch von Open-Meteo, ebenso
   alle Stunden, die weder im Archiv noch in der Prognose liegen (vor dem
   Archivstart, nach CH2). Mit vollem Archiv und U/V auf Druckflächen aus
   OGD fällt auch das weg.
3. **Saison-Replay:** Für einen Lauf ab 1. September braucht es das Archiv ab
   Saisonbeginn. Diesen Winter fehlt der Herbst noch. Bis das Archiv eine
   Saison abdeckt, bleibt dort Open-Meteo als Rückfall (für einen
   Neustart ausserhalb des Abos unproblematisch).
4. **Fallback ehrlich halten:** Solange Open-Meteo Lücken füllt, steht es in
   der App unter «Datenquellen & Lizenzen» (CC BY 4.0). Für ein Abo gilt:
   entweder ganz auf OGD umstellen oder den Open-Meteo-Plan buchen.
