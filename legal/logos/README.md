# Logos für «Datenquellen & Lizenzen»

Jede Quelle in der App hat ein farbiges Kürzel (st, MS, OSM …). Liegt hier eine
Datei `<id>.svg` (oder `.png`/`.webp`), zeigt die App stattdessen das Logo.
Der Build kopiert die Dateien nach `dist/logos/` und schreibt
`dist/logos/index.json`.

Die IDs stehen in `SRC_GROUPS` (`pipeline/interactive_export.py`):

| id | Quelle | Logo verwenden? |
|---|---|---|
| swisstopo, meteoswiss, bafu | Bundesämter | **Nein, ohne schriftliche Bewilligung.** Das Bundeslogo (Wappen + «Schweizerische Eidgenossenschaft») ist dem Bund vorbehalten (Corporate Design Bund, Wappenschutzgesetz). Das Kürzel und die Textquelle genügen der OGD-Lizenz. |
| slf | WSL-Institut SLF | Nur mit Zustimmung des SLF (Markenrecht). |
| osm | OpenStreetMap | Erlaubt für Quellenangaben gemäss OSMF-Trademark-Policy; Datei von wiki.openstreetmap.org/wiki/Logos. |
| copernicus, worldcover | EU / ESA | Copernicus-Logo-Richtlinien beachten (Nutzerinnen von Copernicus-Daten dürfen es meist zur Quellenangabe verwenden). |
| openmeteo, geonames, windy, terrain | Dienste | Logo der Brand-Seite des Anbieters; im Zweifel fragen. |
| leaflet, maplibre, supabase, pmtiles, capacitor | Open Source | Logos aus den offiziellen Repos/Brand-Seiten, nur zur Nennung. |

Faustregel: Ein Logo darf nicht nach Partnerschaft oder Empfehlung aussehen.
Im Zweifel bleibt das Kürzel.
