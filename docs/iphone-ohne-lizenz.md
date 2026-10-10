# Snowmapper ohne Developer-Lizenz auf dem eigenen iPhone

Mit einer normalen (kostenlosen) Apple-ID und Xcode läuft die echte App auf
**deinem** iPhone. Die App läuft nach **7 Tagen** ab, dann einfach wieder
installieren (Schritte 5–6). Andere Personen können sie so nicht installieren.

Du brauchst: einen Mac (macOS 14.5 oder neuer), ein USB-Kabel, dein iPhone.

1. **Xcode installieren:** Mac App Store → «Xcode» → Laden (ca. 10 GB, dauert).
   Einmal öffnen, die Zusatzkomponenten installieren lassen.
2. **Apple-ID in Xcode:** Xcode → *Settings…* → *Accounts* → **+** → *Apple ID* → anmelden.
   Es erscheint ein Team «Dein Name (Personal Team)».
3. **Projekt herunterladen:** GitHub → *Actions* → «iOS build (TestFlight)» → den neuesten
   grünen Lauf öffnen → unten bei *Artifacts* **snowmapper-xcode-projekt** laden.
   Zip doppelklicken, dann `snowmapper-xcode.tar.gz` doppelklicken → Ordner `snowmapper`
   (darin `ios` und `node_modules` – beide zusammen lassen).
4. **Öffnen:** `snowmapper/ios/App/App.xcworkspace` doppelklicken (die *.xcworkspace*, nicht *.xcodeproj*).
5. **Signieren:** links oben das blaue **App**-Symbol → *TARGETS: App* →
   Reiter **Signing & Capabilities** →
   - «Automatically manage signing» an
   - **Team:** «Dein Name (Personal Team)»
   - Meldet Xcode, die Bundle-ID sei vergeben: *Bundle Identifier* auf
     `ch.snowmapper.app.DEINNAME` ändern.
6. **iPhone verbinden:** per Kabel anschliessen, auf dem iPhone «Vertrauen» tippen.
   Oben in Xcode als Ziel dein iPhone wählen und **▶ (Run)** drücken.
   - Erstes Mal: iPhone → *Einstellungen → Datenschutz & Sicherheit →
     Entwicklermodus* einschalten, Neustart, bestätigen.
   - Startet die App nicht («Nicht vertrauenswürdiger Entwickler»):
     *Einstellungen → Allgemein → VPN & Geräteverwaltung* → deine Apple-ID →
     **Vertrauen**.

Fertig: Snowmapper liegt auf dem Home-Bildschirm.
