const CACHE_NAME = "tutor-shell-v13";

// Recursos del "shell" que pre-cacheamos en el install (para que la app abra offline).
const SHELL = [
  "/",
  "/static/styles.css",
  "/static/app.js",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/apple-touch-icon.png",
  "/static/manifest.webmanifest"
];

// Assets que cambian raramente y son grandes: cache-first (imágenes/fuentes).
const HEAVY_ASSET_RE = /\.(png|jpg|jpeg|webp|svg|woff2?)$/i;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // No tocar peticiones de API ni websockets ni el endpoint de sesión
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws") || url.pathname === "/session") {
    return;
  }
  // No tocar peticiones a otros orígenes (OpenAI, etc.)
  if (url.origin !== self.location.origin) return;

  // Estrategia para assets pesados (modelos, imágenes): cache-first.
  if (HEAVY_ASSET_RE.test(url.pathname)) {
    event.respondWith(
      caches.match(req).then((cached) =>
        cached || fetch(req).then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, copy)).catch(() => {});
          }
          return res;
        })
      )
    );
    return;
  }

  // Estrategia para HTML / JS / CSS / manifest: network-first.
  // Así, en cuanto subas código nuevo, el navegador lo coge sin quedarse en caché viejo.
  // Si no hay red, usamos lo que tengamos en caché (modo offline).
  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(req))
  );
});
