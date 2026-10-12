import type { CapacitorConfig } from '@capacitor/cli';

// ONE config for BOTH platforms. iOS and Android are generated from this file
// and bundle the same ./www, so the two apps cannot drift apart.
//
// ── App identity ────────────────────────────────────────────────────────────
// Change these two lines to rebrand. `appId` must be a reverse-domain string you
// control. It is the iOS Bundle Identifier (App Store Connect) AND the Android
// applicationId (Google Play) -- the Play one can never change after the first
// upload.
const APP_ID = 'ch.snowmapper.app';
const APP_NAME = 'Snowmapper';

const config: CapacitorConfig = {
  appId: APP_ID,
  appName: APP_NAME,
  // The web app is generated into ./www by `npm run build:web`
  // (which runs ../pipeline/interactive_export.py). It is bundled into the
  // binary, so the app opens fully offline on first launch.
  webDir: 'www',
  ios: {
    // Let the web content draw under the status bar / home indicator; the web
    // app already handles safe areas via env(safe-area-inset-*).
    contentInset: 'never',
    backgroundColor: '#ffffff',
    // Allow the WKWebView to load Supabase / tile CDNs over https.
    limitsNavigationsToAppBoundDomains: false,
  },
  android: {
    backgroundColor: '#ffffff',
    // Required by @capacitor-community/background-geolocation: without it
    // Android halts location updates ~5 min after the screen locks, which
    // would silently cut a recorded tour short.
    useLegacyBridge: true,
    // Never allow plain-http content inside the app.
    allowMixedContent: false,
  },
  plugins: {
    // Android edge-to-edge (mandatory from API 35). The web app already lays
    // out with viewport-fit=cover + env(safe-area-inset-*), as on iOS. "native"
    // makes env() correct on Chromium >= 140 and pads the WebView on older
    // ones, so the same CSS works everywhere. ("css" would additionally inject
    // --safe-area-inset-* variables the app never reads, and it logs an error
    // when it fires before the document exists.)
    SystemBars: {
      insetsHandling: 'native',
      initialViewportFitValueHint: 'cover',
    },
    SplashScreen: {
      // The web app shows its own lightweight loader (#intro), so keep the
      // native splash minimal and let JS hide it once boot completes.
      launchShowDuration: 0,
      launchAutoHide: false,
      backgroundColor: '#ffffff',
      showSpinner: false,
    },
    StatusBar: {
      overlaysWebView: true,
      style: 'DARK',
    },
    Keyboard: {
      resize: 'native',
    },
  },
};

export default config;
