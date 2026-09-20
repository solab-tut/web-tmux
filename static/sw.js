'use strict';

// When a phone discards a tab and re-creates it, the browser — not the page —
// fetches the document. On a link that has only just woken up that request can
// hang with nothing able to cancel it: the restored page works fine, but the
// browser is left showing "loading" for good, and the stop button does not
// clear it. Answering navigations from this cache takes the network out of
// that path entirely, so the load always completes.
//
// Everything here is the app shell. Terminal contents never travel over HTTP —
// they only ever go through the WebSocket — so nothing cached here is private.

const CACHE = 'web-tmux-shell-v1';
const DOCUMENT = '/';
const AUTH_PATH = '/auth/session';
const IMMUTABLE_PREFIX = '/vendor/';

const SHELL = [
  DOCUMENT,
  '/style.css',
  '/app.js',
  '/history.js',
  '/vendor/xterm/5.3.0/css/xterm.css',
  '/vendor/xterm/5.3.0/lib/xterm.js',
  '/vendor/xterm-addon-fit/0.8.0/lib/xterm-addon-fit.js',
  '/vendor/xterm-addon-unicode11/0.4.0/lib/xterm-addon-unicode11.js',
  '/vendor/xterm-addon-webgl/0.16.0/lib/xterm-addon-webgl.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // 'reload' so a freshly installed worker precaches what the server has
    // now, not whatever the HTTP cache happens to be holding.
    await cache.addAll(SHELL.map((url) => new Request(url, { cache: 'reload' })));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key !== CACHE) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

// Refresh one entry in the background. It must never reject: the whole point
// is that what the page got back does not depend on this succeeding.
async function revalidate(request) {
  try {
    const response = await fetch(request);
    if (response && response.ok && response.type === 'basic') {
      const cache = await caches.open(CACHE);
      await cache.put(request, response.clone());
    }
  } catch (_) {
    // Offline, or the link is still coming up. The cache stays as it is.
  }
}

// Keeping the worker alive for the refresh is a courtesy, not a requirement,
// and waitUntil throws if the event has already settled. Anything thrown here
// would reject respondWith and hand the browser a network error, which is far
// worse than a refresh that gets cut short.
function background(event, promise) {
  try { event.waitUntil(promise); } catch (_) {}
}

async function respond(event, url) {
  const request = event.request;
  const navigation = request.mode === 'navigate';
  try {
    const cache = await caches.open(CACHE);
    const cached = await cache.match(navigation ? DOCUMENT : request);
    if (cached) {
      // Answer from the cache, then look for a newer copy without making the
      // page wait for it. The vendored files are versioned in their path, so
      // they never change under the same URL and need no refresh.
      if (navigation) {
        background(event, revalidate(new Request(DOCUMENT, { cache: 'reload' })));
      } else if (!url.pathname.startsWith(IMMUTABLE_PREFIX)) {
        background(event, revalidate(request));
      }
      return cached;
    }
    const response = await fetch(request);
    if (response && response.ok && response.type === 'basic') {
      background(event, cache.put(request, response.clone()));
    }
    return response;
  } catch (_) {
    // Storage unavailable, or the network failed on a miss. Let the browser
    // make the request itself rather than turning this into an error page.
    return fetch(request);
  }
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  let url;
  try { url = new URL(request.url); }
  catch (_) { return; }
  if (url.origin !== self.location.origin) return;
  // The session cookie has to come from the server every time, and it is the
  // one response that is marked no-store.
  if (url.pathname === AUTH_PATH) return;

  event.respondWith(respond(event, url));
});
