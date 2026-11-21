# Kernprodukt: Schneetyp-Layer + Community-Schnellmeldungen + Feed

Stand 21.11.2026. Leitidee: **Das Modell schlägt vor, die Community prüft – sichtbar, mit einem Tipp.**
Jede Schnellmeldung ist zugleich Bodenwahrheit für den Schneetyp-Layer, und jede Modellaussage
lässt sich im Gelände bestätigen oder widerlegen. Damit wird die Community zum Messnetz des Modells
und das Modell zum Einstieg für die Community.

## 1 · Kritischer Blick (heute)

| Problem | Warum es weh tut |
|---|---|
| Der Layer sagt *was*, nicht *warum* («Hart») | Ohne Begründung kein Vertrauen; man kann den Fehler nicht selbst einschätzen |
| Meldung und Modell leben nebeneinander | Wer meldet, sieht nicht, ob das Modell recht hatte; das Modell lernt nichts aus der Meldung |
| Schnellmeldung beginnt bei null (Rad auf «Pulver») | Mehr Tipps als nötig; meist bestätigt man nur, was ohnehin wahrscheinlich ist |
| Wochentage auf der Zeitleiste («Mi 15:00») | Man muss zählen; der Bezug zu heute fehlt |
| Feed zeigt Meldungen ohne Einordnung | Ob eine Meldung überrascht (Modell sagt Kruste, Meldung sagt Pulver), ist die interessanteste Information und war unsichtbar |
| Golden-Datensatz mit 27 Punkten | Zu klein, um die Schneetyp-Regeln ehrlich zu kalibrieren |

## 2 · Umgesetzt in dieser Runde (M3)

1. **Zeitleiste in Tagesabständen:** «Heute», «−1d», «+2d» statt Wochentagen, auch in den
   Zeitangaben («−1d 13:00») und beim «Früher …» der Schnellmeldung.
2. **Ein Modell-Orakel für alles** (`modelSnowCell` / `modelSnowAt`): Schneetyp-Klasse im Vokabular
   der Schnellmeldung, Pulvertiefe und eine **Begründung in Klartext** aus den Regel-Flags
   («Wind aus W – Lee-Hang, eingeweht», «Regen nach dem letzten Schneefall», «angetaut und wieder
   gefroren», «warme Luft – die Oberfläche ist nass»). Dieselbe Funktion speist Chip, Meldung und Feed.
3. **Punkt-Chip** auf dem Schneetyp-Layer: Wert oben, Begründung als zweite Zeile (höchstens zwei
   Zeilen, nie breiter als das Handy), daneben ein M3-Tonal-Button **«+ Melden»**.
4. **Schnellmeldung mit Vorschlag:** Eine M3-Outlined-Card «MODELL HIER · Pulver · 13 cm» mit
   Begründung und einer Aktion **«Stimmt»**. Ein Tipp stellt Rad und Tiefe ein, danach korrigiert man
   nur, was abweicht. Der Button zeigt «Übernommen», solange das Rad auf dem Vorschlag steht. Bei
   «Früher …» folgt der Vorschlag der gewählten Zeit.
5. **Jede Schnellmeldung speichert die Modellaussage mit** (`condition_data.model = {k, cm, agree, v}`).
   So entsteht automatisch ein gelabelter Datensatz «Modell gegen Gelände» mit Modellversion.
   Das ist der Ersatz für den zu kleinen Golden-Datensatz.
6. **Feed: «Modell: …» neben jeder Schnellmeldung** als M3-Assist-Chip: grün mit Haken bei gleicher
   Klasse, neutral mit ≈ bei verwandter Klasse (z. B. Pulver/Altpulver, Hart/Triebschnee), rot mit ×
   bei Widerspruch. Ältere Meldungen ohne gespeicherte Aussage rechnen den Chip aus Ort und Zeit nach.

## 3 · Nächste Schritte (priorisiert)

| # | Schritt | Wirkung | Aufwand |
|---|---|---|---|
| A1 | **Trefferquote öffentlich:** ein Karten-/Feed-Banner «Modell diese Woche: 71 % bestätigt (n = 54)» aus `condition_data.model.agree` | Ehrlichkeit als Produktmerkmal; motiviert zum Melden | klein (SQL-View + Chip) |
| A2 | **Meldungen korrigieren den Layer lokal:** frische Meldungen (≤ 24 h, gleiche Exposition ± 300 m) überschreiben die Klasse in ~1.5 km mit Ring «von der Community bestätigt» | Der Layer wird dort besser, wo Leute unterwegs sind | mittel |
| A3 | **Unsicherheit zeigen:** Zellen nahe einer Regelgrenze (z. B. Wind 18–22 km/h, warm_dh 5–7) schraffiert oder mit Punktmuster | Verhindert falsche Sicherheit | mittel |
| A4 | **Regeln aus den gelabelten Meldungen nachjustieren** (Schwellen `PD_*` per Gittersuche, Holdout nach Datum), sobald ≥ 200 Meldungen mit `model` vorliegen | Kalibrierung mit echter Bodenwahrheit | mittel |
| B1 | **Feed-Filter «Überraschungen»:** nur Meldungen, die dem Modell widersprechen | die wertvollsten Meldungen zuerst | klein |
| B2 | **«Bestätigen»-Geste im Feed mit Ortsprüfung:** wer innerhalb von 3 h am selben Hang war, kann mit einem Tipp «gleich» melden (Kopie mit eigener Position) | doppelt so viele Datenpunkte für einen Tipp | mittel |
| B3 | **Meldungs-Erinnerung beim Tourende** (Tracking stoppt → «Wie war der Schnee? Modell sagte: Pulver») | Meldungen genau dann, wenn man es weiss | klein |
| C1 | Legende des Schneetyp-Layers mit Flächenanteilen im Ausschnitt («Pulver 34 %») | schneller Überblick, klare Lesbarkeit | klein |
| C2 | Zeitleisten-Chips (−2d … +3d) mit kleinem Schneetyp-Farbstreifen je Tag | sieht man, wann sich die Oberfläche ändert | mittel |

Kritische Schranken: Keine Meldung «aus der Ferne» zählt als Bodenwahrheit; die Schnellmeldung
bleibt an GPS bzw. den verschobenen Punkt gebunden. Community-Korrekturen (A2) nur mit
Vertrauensstufe des Melders (es gibt `progTrustOf`) und nie über das Lawinenbulletin hinweg.
