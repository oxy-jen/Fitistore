/* Bump this version when changing the precache list or offline page. */
const CACHE_NAME = "fity-pwa-static-v1";
const PRECACHE_URLS = [
  "/static/pwa/offline.html",
  "/static/css/style.css",
  "/static/css/design-system.css",
  "/static/css/media-showcases.css",
  "/static/css/pwa.css",
  "/static/js/app.js",
  "/static/js/cart.js",
  "/static/js/nav-overflow.js",
  "/static/js/pwa-install.js",
  "/static/images/mainLogo-160.png",
  "/static/images/pwa/icon-192.png",
  "/static/images/pwa/icon-512.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys
        .filter((key) => key.startsWith("fity-pwa-static-") && key !== CACHE_NAME)
        .map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

function isSafeStaticAsset(url) {
  return url.pathname.startsWith("/static/css/") ||
    url.pathname.startsWith("/static/js/") ||
    url.pathname.startsWith("/static/images/") ||
    url.pathname === "/static/pwa/offline.html";
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(async () => {
      const offline = await caches.match("/static/pwa/offline.html");
      return offline || Response.error();
    }));
    return;
  }

  if (isSafeStaticAsset(url)) {
    event.respondWith(fetch(request).then(async (response) => {
      const cacheControl = response.headers.get("Cache-Control") || "";
      if (response.ok && response.type === "basic" &&
          !/private|no-store/i.test(cacheControl) && !response.headers.has("Set-Cookie")) {
        try {
          const cache = await caches.open(CACHE_NAME);
          await cache.put(request, response.clone());
        } catch (_) {
          // Storage pressure must not turn a successful online asset request into a failure.
        }
      }
      return response;
    }).catch(async () => {
      const cached = await caches.match(request);
      return cached || Response.error();
    }));
  }
  // APIs, uploads, account/admin content and all non-allowlisted requests stay network-only.
});
