# Snowmapper Pro – Paywall

Das Grundgerüst ist gebaut und **standardmässig aus**: Solange die Paywall aus
ist, ändert sich für niemanden etwas. Du schaltest sie in der App ein
(Einstellungen › Konto › «Paywall: aus – umschalten», nur für dich als Admin).

## Was Pro ist

| Pro-Funktion | Wo die App fragt |
|---|---|
| Skiqualität (SNOWPACK) | Ebene einschalten (`ovToggle('variantA')`) |
| Neuschnee-Push für Favoriten | «Push für Neuschnee einschalten» (`favPushOn`) |
| Offline-Kartenausschnitt | «Kartenausschnitt offline speichern» (`offlineSave`) |
| Powder-Score der Touren | in der Liste genannt, noch nicht gesperrt |

Preise im Angebot: **Saisonpass CHF 29** (gilt bis 31. Mai) und **monatlich
CHF 4**. Text und Preise: `PRO_PRICES` / `PRO_FEATURES` in
`pipeline/interactive_export.py`. Die echten Preise legst du in Stripe fest.

## Wer Pro hat

`pro_state()` in `supabase/migrations/20261101000000_entitlements.sql`:

1. **Du** (Rolle `admin`): immer.
2. **Gekauft oder geschenkt**: Eintrag in `entitlements` mit `valid_until` in
   der Zukunft.
3. **Mit Einladungscode** (FF / BETA): gratis bis `app_settings.tester_pro_until`
   (vorgegeben: 31.5.2027).

Schenken: `select pro_gift('<user-uuid>', 365);` im SQL-Editor oder per RPC als Admin.

## Stripe einrichten (Web)

1. Konto auf [stripe.com](https://stripe.com) eröffnen. Als Land Schweiz wählen
   und das Bankkonto hinterlegen.
2. **Produkte** › «Snowmapper Pro» mit zwei Preisen:
   - Saisonpass: einmalig CHF 29 → Preis-ID `price_…`
   - Monatlich: wiederkehrend CHF 4/Monat → Preis-ID `price_…`
3. Unter **Einstellungen › Zahlungsmethoden** Karten, Apple Pay, Google Pay und
   **TWINT** einschalten. TWINT geht nur für den Saisonpass, also für
   Einmalzahlungen.
4. **Webhook**: Developers › Webhooks › Endpunkt
   `https://gdtxwowcqtbdkcoksivb.functions.supabase.co/stripe-webhook` mit den
   Ereignissen `checkout.session.completed`, `customer.subscription.created`,
   `customer.subscription.updated` und `customer.subscription.deleted`. Den
   Signing Secret `whsec_…` kopieren.
5. **Secrets in Supabase** (Dashboard › Edge Functions › Secrets):
   `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_SEASON`,
   `STRIPE_PRICE_MONTH`, optional `APP_URL`.
6. **Funktionen deployen**:
   `supabase functions deploy stripe-checkout` und
   `supabase functions deploy stripe-webhook --no-verify-jwt`.
7. Zuerst im **Testmodus** mit der Karte `4242 4242 4242 4242` prüfen, ob Pro
   danach in der App aktiv ist (Einstellungen › Konto).

## iOS-App (später)

Apple verlangt dort den In-App-Kauf. Die App zeigt in der iOS-Version deshalb
keine Kaufknöpfe, sondern «bald erhältlich». So geht es weiter:

1. **RevenueCat**: gratis bis 2 500 USD Umsatz pro Monat. Darin die zwei
   Produkte anlegen, gleich wie in App Store Connect.
2. Plugin `@revenuecat/purchases-capacitor` in `apple-app/` einbauen.
3. Eine Edge Function `revenuecat-webhook` schreibt `entitlements` mit
   `source='apple'`, gleich wie der Stripe-Webhook.
4. Apple nimmt 15 % (Small Business Program).

## Vor dem Einschalten der Paywall

- **Datenquellen kommerziell**:
  - Open-Meteo: Die Wetter-Ebenen kommen jetzt von MeteoSchweiz. Open-Meteo
    füllt nur noch Höhenwind und Lücken. Vor dem Einschalten diese Reste
    ablösen oder den Open-Meteo-Plan buchen (siehe
    `docs/wetterarchiv-meteoschweiz.md`)
  - Skitourenrouten: Zusage des SAC (`docs/mail-sac-skitourenrouten.md`)
  - Windy-Webcams: kostenpflichtiger Tarif oder die Ebene weglassen
- **Daten schützen**: Die Skiqualitäts-Kacheln sind heute öffentlich. Für eine
  echte Sperre muss der Kachel-Worker (`tiles/worker`) das Supabase-JWT prüfen
  und `is_pro()` abfragen. Die App muss die Kacheln dann mit Token laden.
- **AGB** um Preise, Laufzeit, Kündigung und Rückerstattung ergänzen
  (`legal/agb.html`).
- **MWST**: erst ab CHF 100 000 Umsatz pro Jahr. In-App-Käufe rechnet Apple
  selbst ab.
