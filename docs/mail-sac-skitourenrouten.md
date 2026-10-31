# Mail-Entwurf: Skitourenrouten (SAC / swisstopo)

**Warum:** Der Datensatz «Skitourenrouten» (`ch.swisstopo-karto.skitouren`,
opendata.swiss) ist für die **kommerzielle Nutzung nur mit Bewilligung des
Datenlieferanten** freigegeben. Die Routen stammen vom SAC und werden von
swisstopo publiziert. Solange Snowmapper gratis ist, ist das unkritisch. Vor
einem Abo oder einer Werbefinanzierung braucht es aber eine schriftliche
Zusage.

**An:** SAC Zentralverband, Bern ([Kontakt prüfen, z. B. Tourenportal / Kommunikation])
**CC:** swisstopo Geodatenvertrieb, mapsales@swisstopo.ch
**Betreff:** Anfrage: Nutzung der Skitourenrouten in der App «Snowmapper»

---

Guten Tag

Ich entwickle «Snowmapper», eine App für Skitourengängerinnen und Skitourengänger
in der Schweiz (Web-App und iOS-App, zurzeit in einer geschlossenen Testphase).
Sie zeigt modellierte Schnee- und Pulverbedingungen auf der Karte, berechnet mit
dem Schneedeckenmodell SNOWPACK des SLF und mit Wetterdaten von MeteoSchweiz.
Dazu kommen Beobachtungen aus der Community.

Für die Tourenplanung verwenden wir die Skitourenrouten, die swisstopo in
Zusammenarbeit mit dem SAC als Geodaten publiziert (Ebene
«ch.swisstopo-karto.skitouren»). Wir zeigen die Routen auf der Karte und
berechnen für jede Route aus dem Schneemodell einen «Powder-Score». Die Quelle
nennen wir in der App.

Gemäss Nutzungsbedingungen braucht die kommerzielle Nutzung dieses Datensatzes
eine Bewilligung des Datenlieferanten. Snowmapper ist heute kostenlos. Später
möchten wir ein günstiges Abo für Zusatzfunktionen anbieten. Deshalb möchten wir
frühzeitig klären:

1. Dürfen wir die Skitourenrouten in Snowmapper anzeigen und für den
   Powder-Score verwenden, auch wenn die App künftig kostenpflichtige Funktionen
   hat?
2. Zu welchen Bedingungen (Lizenz, allfällige Gebühr, Quellenangabe)?
3. Wäre eine Zusammenarbeit denkbar? Zum Beispiel könnten wir bei jeder Route
   auf das SAC-Tourenportal verweisen, damit die Tourenbeschreibungen beim SAC
   bleiben. Oder wir könnten dem SAC anonymisierte Schneebeobachtungen aus der
   Community zur Verfügung stellen.

Uns ist wichtig: Snowmapper ersetzt weder das Lawinenbulletin noch die
Tourenbeschreibungen des SAC. Wir möchten Tourengängerinnen und Tourengänger zu
guten, sicheren Touren und zu den Angeboten des SAC führen.

Gerne zeige ich Ihnen die App in einem kurzen Gespräch oder schicke Ihnen einen
Testzugang.

Freundliche Grüsse

[Vorname Name]
[Telefon]
Snowmapper · [Website]

---

**Nach der Antwort:** Zusage ablegen (PDF in einer privaten Ablage, nicht im
Repo). Danach die Quellenangabe in `SRC_GROUPS` und `OVERLAYS.skitour*.attr`
(`pipeline/interactive_export.py`) an den verlangten Wortlaut anpassen.
