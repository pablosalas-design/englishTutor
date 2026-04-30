const CACHE_NAME = "tutor-shell-v4";
const SHELL = [
  "/",
  "/static/styles.css",
  "/static/app.js",
  "/static/avatar.js",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/apple-touch-icon.png",
  "/static/manifest.webmanifest"
];

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
  const url = new URL(req.url);

  // No tocar peticiones de API ni websockets ni el endpoint de sesión
  if (req.method !== "GET") return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws") || url.pathname === "/session") {
    return;
  }

  // Estrategia: cache-first para shell estático, network-first para el resto
  if (SHELL.includes(url.pathname) || url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(req, copy)).catch(() => {});
        return res;
      }))
    );
  }
});
