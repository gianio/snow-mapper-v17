# Neuschnee & Schneetyp – Runde 19.11.2026: Befunde, Umsetzung, nächster Plan

Geprüft gegen Daten, die es schon gibt: SLF-Neuschneekarte HN1D (15.–19.11.,
1-km-Zellen), SwissMetNet-Niederschlag (stündlich, ~60 Bergstationen ≥ 900 m),
SLF-Lawinenbulletin vom 19.11. (119 Regionen, 76 mit «Triebschnee»), der
Modelllauf 202611191343 (stündlich PREC, TEMP, Wind, SNOW je 1-km-Zelle).
Varianten wurden offline aus dem Modelllauf nachgerechnet, die Parameter
**Leave-one-day-out** gewählt (am weggelassenen Tag geprüft).

## 1 · Hypothesen – was hielt, was nicht

| Hypothese | Prüfung | Ergebnis |
|---|---|---|
| Niederschlag ist ~2× zu hoch (Deakkumulation, Spin-up der ersten Vorlaufstunden) | Modell-PREC an der Zelle vs SwissMetNet `rre150h0`, 4 Tage, 06–06 UTC | **widerlegt**: Modell / Messer 1.07 · 1.07 · 1.82 · 1.23 (gesamt 1.29). Messer fangen Schnee 10–40 % zu wenig → Niederschlag ≈ richtig. Nur der 17.11. fällt auf. |
| Der Fehler sitzt in der Umrechnung Wasser → cm | mm Modell-PREC pro cm SLF HN1D an kalten Zellen > 1800 m | **bestätigt**: 2.1–2.8 mm/cm; mit der alten SLR (12–15:1) wurden daraus 2–3.5× zu viele cm |
| Wind macht Neuschnee dichter (Crocus-Dichte) | Offline-Neurechnung vs HN1D | **bestätigt**: Modell/SLF 2.76 → 1.67, MAE 10 km 4.1 → 1.9 cm |
| Höhenzuschlag +4 %/100 m zählt das Relief doppelt | Variante ohne Zuschlag | **bestätigt**: → 1.42 / 1.5 cm; Fehler wächst nicht mehr mit der Höhe |
| Luv/Lee × Wind erzeugt im Mittel Schnee | Mittel des Faktors auf Testgelände; Offline-Variante | Mittel 1.035 statt 1.0 → behoben; gegen HN1D **neutral** (kein messbarer Gewinn) |
| Windrichtung trifft die Triebschnee-Expositionen | Lee-Sektor (dom. Wind 24–120 h) vs Bulletin-Expositionen | **bestätigt**: Jaccard 0.54 (Zufall 0.29) |
| Wind gewichtet mit Schneefall × U³ trifft besser | dieselbe Prüfung | **kein Gewinn** (0.54) → nicht eingebaut |
| Schneetyp zeigt Pulver rundum, wo das SLF Triebschnee meldet | Regel-Simulation in den 76 Regionen | **bestätigt**: 45/76 «alle Expositionen» (R1 Kälte überstimmte den Wind, 10-m-Wind 18 km/h vs 700 hPa 48 km/h) |

## 2 · Umgesetzt (release/1.0)

**Neuschnee** (`model/factors.py`, `model/snow_model.py`, `config/model_params.yaml`)
- SLR = 1000 / ρ, ρ = 125 + 6·T + 35·√U (Form wie Crocus, Vionnet et al. 2012; a, c gegen HN1D).
- Kein Höhenzuschlag mehr auf den Niederschlag (ICON-CH hat das Relief).
- Regen/Schnee auf der **Feuchtkugeltemperatur** (Psychrometer, Luftdruck der Höhe),
  Feuchte von MeteoSchweiz (RELHUM_2M – wurde bisher geholt und verworfen) bzw. Open-Meteo.
- Luv/Lee × Wind lokal (1.5 km) auf Mittel 1 normiert.

Gegen HN1D (5 Tage): **Modell/SLF 2.76 → 1.14, MAE 10 km 4.1 → 1.2 cm** (LOO 1.1).
Höhenbänder Modell / SLF: < 1500 m 0.3 / 0.9 · 1500–2000 1.6 / 1.7 · 2000–2500 3.4 / 2.5 · > 2500 5.0 / 4.6 cm
(vorher 0.6 / 0.9 · 3.6 / 1.7 · 7.8 / 2.5 · 13.1 / 4.6). Die Feuchtkugel-Trennung ist in diesen
Zahlen **nicht** enthalten (die Modelldatei hat keine Feuchte) – sie soll das Defizit unter 1500 m verkleinern.

**Schneetyp** (`computePowder` in der App, `model/powder_engine.py` synchron)
- W1: Wind = max(10 m, 0.4 × 700 hPa) – der Kammwind verfrachtet.
- Der Wind entscheidet die Expositionen; Kälte (R1) und klare Nacht (R4) öffnen keine Expositionen mehr.
  windstill → alle; sonst → Lee (Luv = «Hart»); W2 Sturm → Lee mit reduzierter Qualität (vorher: gar nichts).
- S1: Sonnenschaden = Clear-Sky × (0.25 + 0.75 × Sonnenanteil der letzten 24 h). Vorher verloren
  Süd- und Westhänge ihr Pulver auch unter geschlossener Wolkendecke.

Kritisch: Am 19.11. war es überall windig, auch in Regionen ohne Triebschnee – die
Trennschärfe der Windregel ist mit einem Tag **nicht** belegt. S1 hat noch keine Bodenwahrheit.

## 3 · Nächster Plan

| # | Schritt | Warum | Messbar an |
|---|---|---|---|
| 0 | `tools/eval_powder.py` (Golden-Datensatz, 27 Beobachtungen) mit der neuen Schneetyp-Logik laufen lassen – hier blockiert, Open-Meteo antwortete von der gemeinsamen IP nur mit 429 | Regression der Schneetyp-Regeln ausschliessen | Trefferquote (vorher 26/27) |
| 1 | Nach dem Deploy: `calibrate_newsnow.py` / Cockpit auf dem **neuen** Lauf | prüft die Umstellung mit echter Pipeline (inkl. Feuchte, 250-m-Gitter) | Modell/SLF 0.8–1.2, MAE 10 km < 2 cm |
| 2 | Bulletin täglich archivieren (Snow-Research) und die Windregel über ≥ 20 Tage gegen Triebschnee-Expositionen werten, **mit** Regionen ohne Triebschnee | Trennschärfe statt nur Treffer | Jaccard, Anteil «rundum» in TS-/Nicht-TS-Regionen |
| 3 | 17.11. (Modell / Messer 1.82): zu nass im Wallis/Berner Oberland (CDM 14 vs 2 mm, MVE 13 vs 3, GRH 9 vs 2), zu trocken am Nordrand (CHA 7 vs 23, DOL 17 vs 27) – räumlich verschoben, nicht systematisch. Prüfen, ob das Archiv dort eine späte CH2-Stunde statt CH1 hat | Ortsfehler des Wettermodells vs Nahtstelle | Verhältnis je Vorlaufstunde und Modell |
| 4 | Feuchte in die Modelldatei (1 Byte) | dann lässt sich die Feuchtkugel-Trennung offline prüfen wie die Dichte | Bias < 1500 m |
| 5 | Schneefallgrenze je Ereignis gegen IMIS-HS-Anstieg nach Höhe | direkte Prüfung der Phase | Höhe, ab der HS steigt, Modell vs IMIS |
| 6 | S1 gegen IMIS-Oberflächentemperatur (TSS 0 °C erreicht?) auf Süd-/Nordstationen | Bodenwahrheit für Sulz/Kruste | Trefferquote «Oberfläche nass» |
| 7 | Stationskorrektur für die vergangenen Stunden (log-Verhältnis HN1D/Modell, 30 km geglättet, 0.5–2) | was nach 1–4 bleibt | MAE der Analyse-Tage |
| 8 | Dichteparameter nach ≥ 3 Wochen neu wählen (LOO), auch nach Temperaturklassen | 5 Tage = eine Wetterlage | LOO-MAE |

Werkzeuge: Offline-Neurechnung und SwissMetNet-Vergleich laufen gegen `data/latest.json`
des Modells und die öffentlichen SLF-/MeteoSchweiz-Daten (gehören ins Research-Repo,
nicht hierher).
