# FityStores PWA and Android TWA

## Current implementation

The Flask storefront remains the only shop backend. The PWA adds an installable web manifest, brand icons made from the existing FityStores logo, browser install guidance, a root-scoped service worker, and a generic offline page. It does not cache page or API responses. The worker only caches same-origin CSS, JavaScript, approved public images, and the generic offline page; requests that change data are left to Flask and the browser.

The service worker is served at `/service-worker.js` so its allowed scope is `/`. It uses a versioned cache name and deletes older FityStores PWA caches after an update. Bump `CACHE_NAME` in `app/static/pwa/service-worker.js` when changing the offline page or precache list.

`/.well-known/assetlinks.json` returns an empty JSON array until a real signing certificate fingerprint is configured. It never publishes a guessed or placeholder fingerprint. The package ID is currently `top.fitystore.app`, checked against the repository's Android files; it remains provisional until confirmed for the release.

## Browser installation

- On supported Chrome and desktop browsers, the `Install FityStores` control appears only after the browser emits `beforeinstallprompt`; clicking it invokes the native browser prompt.
- The footer's `How to install FityStores` control gives browser-specific steps for iOS Safari, Android Chrome, and supported desktop browsers. It does not pretend to trigger an unsupported native prompt.
- The install prompt is never opened automatically. A dismissed native prompt is suppressed for 14 days. Already-installed standalone sessions do not show the installation help control.
- Chrome's install affordance may depend on HTTPS, a valid manifest, icon availability, and browser-specific eligibility rules.

## Android TWA setup

A TWA uses the installed browser to display the existing site and requires Digital Asset Links ownership verification. It is not a WebView and does not introduce another backend. GoogleChromeLabs Bubblewrap is the project generator. The repository currently has no generated Android project or signing key, so no APK or AAB is included. Generate the project after the PWA changes have been deployed and the production manifest and icons are publicly reachable:

```powershell
# Node.js 18 or newer is required by the current Bubblewrap CLI.
npm install --global @bubblewrap/cli

# From the repository root; answer the package ID prompt with top.fitystore.app
# after confirming that ID is available for the release.
bubblewrap init --manifest=https://fitystore.top/static/manifest.webmanifest --directory=android
```

During initialization, create or select the release keystore at a location outside this repository, and choose a memorable alias. Keep the keystore and passwords out of source control. Make at least two encrypted backups in separate secure locations. A lost signing key can prevent future updates to a directly distributed app.

Build and test:

```powershell
Set-Location android
.\gradlew.bat assembleDebug
adb install .\app\build\outputs\apk\debug\app-debug.apk

# Bubblewrap creates a signed release APK and Play-ready AAB after signing is configured.
bubblewrap build
```

Use the same release key for future direct APK updates. Before Google Play publication, decide whether to use Play App Signing and obtain the app-signing certificate fingerprint from Play Console; the Play app-signing certificate may differ from the upload key. Do not infer either fingerprint from the debug key.

## Digital Asset Links

After the release signing certificate exists, obtain its SHA-256 fingerprint with `keytool -list -v -keystore <keystore-path> -alias <alias>`. For Play distribution, also obtain the Play app-signing certificate fingerprint from Play Console. Configure the real colon-separated fingerprint(s) as `ANDROID_ASSETLINKS_SHA256_CERT_FINGERPRINTS` in the web server environment, alongside the confirmed `ANDROID_PACKAGE_ID`. The Flask endpoint will then emit the relationship for `https://fitystore.top/.well-known/assetlinks.json`.

Verify the deployed response is JSON with the exact package ID, `delegate_permission/common.handle_all_urls`, and the release certificate fingerprint(s). Verify TWA domain validation on a physical Android device; until validation succeeds, Chrome can show browser UI. No Render setting, environment variable, DNS record, keystore, or production service was changed for this local implementation.

## External navigation and network failure

The TWA must be generated with Bubblewrap's standard Trusted Web Activity/custom-tabs fallback, not a WebView substitute. Bubblewrap's host and scope use `https://fitystore.top/`. Links to domains outside the verified FityStores origin should continue in the system browser. The website's existing Flask login, cart, checkout, payment and account flows remain responsible for their normal navigation and session handling.

The service worker displays a generic offline page if a navigation cannot reach the server. It does not save a page response, customer data, or transaction. Retry after reconnecting to use the normal site.

## Rollback

Revert the PWA manifest/registration markup and PWA assets in a code release. To remove an already installed worker from a browser during local testing, open DevTools > Application > Service Workers and choose Unregister, then clear site storage. No server-side database rollback is required.

## Outstanding before a distributable Android build

1. Deploy and verify the PWA manifest, icons, service worker, and offline page over production HTTPS.
2. Confirm the Android package ID is available and reserve it for FityStores.
3. Install Bubblewrap and generate the official TWA project from the production manifest.
4. Create and securely back up the release key; do not commit it.
5. Configure the real release (and, later, Play app-signing) fingerprint in Digital Asset Links.
6. Build the APK/AAB and verify install, navigation, login, cart, checkout, external links, offline behavior, and TWA verification on a physical Android device.

Bubblewrap's official CLI requires Node.js 18 or newer and generates its Android project from a web manifest. It produces signed APK and AAB artifacts when signing is configured. See [Bubblewrap CLI documentation](https://github.com/GoogleChromeLabs/bubblewrap/blob/main/packages/cli/README.md) and [Chrome's TWA quick start](https://developer.chrome.com/docs/android/trusted-web-activity/quick-start).
