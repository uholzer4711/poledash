/* Minimal service worker: keep /static assets (vendored JS, fonts, CSS)
   available offline / across CDN-less networks. Pages and API calls stay
   network-only — live telemetry must never be served stale — with a tiny
   offline notice as the navigation fallback. */
const CACHE = 'poledash-static-v1';
const CORE = [
  '/static/vendor/fonts/fonts.css',
  '/static/vendor/htmx.min.js',
  '/static/vendor/chart.umd.min.js',
  '/static/vendor/date-fns.min.js',
  '/static/vendor/chartjs-adapter-date-fns.bundle.min.js',
  '/static/vendor/leaflet/leaflet.css',
  '/static/vendor/leaflet/leaflet.js',
  '/static/icon.svg',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(CORE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;

  if (url.pathname.startsWith('/static/')) {
    // Stale-while-revalidate: serve from cache instantly, refresh in the
    // background (ujh.css is ?v=-busted, so a new URL is a new cache entry).
    e.respondWith(
      caches.open(CACHE).then(async (c) => {
        const hit = await c.match(e.request);
        const refresh = fetch(e.request)
          .then((r) => { if (r.ok) c.put(e.request, r.clone()); return r; })
          .catch(() => hit);
        return hit || refresh;
      })
    );
    return;
  }

  if (e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request).catch(() =>
        new Response(
          '<!doctype html><meta charset="utf-8"><title>poledash</title>' +
          '<body style="background:#0f1012;color:#e8e8ea;font:16px/1.5 sans-serif;' +
          'display:grid;place-items:center;height:100vh;margin:0">' +
          '<div style="text-align:center"><div style="font-size:22px">poledash</div>' +
          '<div style="opacity:.6;margin-top:8px">offline — no connection to the server</div></div>',
          { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
        )
      )
    );
  }
});
