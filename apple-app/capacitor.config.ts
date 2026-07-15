import type { CapacitorConfig } from '@capacitor/cli';

// ── App identity ────────────────────────────────────────────────────────────
// Change these two lines to rebrand. `appId` must be a reverse-domain string you
// control and must match the Bundle Identifier you register in App Store Connect.
const APP_ID = 'ch.snowmodel.app';
const APP_NAME = 'Snow Model';

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
    backgroundColor: '#e7e5f5',
    // Allow the WKWebView to load Supabase / tile CDNs over https.
    limitsNavigationsToAppBoundDomains: false,
  },
  plugins: {
    SplashScreen: {
      // The web app shows its own lightweight loader (#intro), so keep the
      // native splash minimal and let JS hide it once boot completes.
      launchShowDuration: 0,
      launchAutoHide: false,
      backgroundColor: '#e7e5f5',
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
