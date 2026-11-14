/* sw.js: lets the app work when the computer is asleep or far away.

   A service worker is a small script the browser keeps beside the app. Every request the app makes
   (the page, the list of episodes, the audio, a picture) passes through it first, so it can answer
   from the copies kept on the phone when the computer does not reply.

   Three kinds of requests:
     the app itself      asked from the computer first (so an update arrives at once); the copy on the
                         phone answers if the computer is silent
     /api/…              same rule; an answer taken from the phone carries the header X-Atelier-Offline,
                         which tells the app it is working offline
     /media/…            audio, pictures, voice: the copy on the phone first when the episode was
                         downloaded, the computer otherwise

   The copies of an episode are put there by the app (button "Télécharger sur ce téléphone"), in a
   cache named atelier-ep-<episode id>. Browsers only allow this on https addresses and on localhost. */

const SHELL = 'atelier-shell-v1', API = 'atelier-api-v1';
const SHELL_FILES = ['./', 'manifest.webmanifest', 'icon-192.png', 'icon-512.png'];
const WAIT = 1800;                                      // ms given to the computer before the phone answers

self.addEventListener('install', event => {
  event.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

function fromNetwork(request, ms) {
  const stop = new AbortController(), timer = setTimeout(() => stop.abort(), ms);
  return fetch(request, {signal: stop.signal, cache: 'no-store'}).finally(() => clearTimeout(timer));
}

async function markOffline(response) {                  // the same answer, labelled as coming from the phone
  const headers = new Headers(response.headers);
  headers.set('X-Atelier-Offline', '1');
  return new Response(await response.blob(), {status: response.status, statusText: response.statusText, headers});
}

let silentSince = 0;                                   // when the computer last failed to answer

async function computerFirst(request, cacheName, key) {
  const cache = await caches.open(cacheName);
  try {
    // after a silence, the next requests wait only a moment: opening the app offline must stay quick
    const response = await fromNetwork(request, Date.now() - silentSince < 20000 ? 300 : WAIT);
    if (response.ok) cache.put(key || request, response.clone());
    silentSince = 0;
    return response;
  } catch (e) {
    silentSince = Date.now();
    const kept = await cache.match(key || request);
    if (kept) return markOffline(kept);
    throw e;
  }
}

async function findMedia(url) {                         // the copy of a media file, whatever its ?v= version
  return (await caches.match(url.href)) || (await caches.match(url.href, {ignoreSearch: true}));
}

async function media(request) {
  const url = new URL(request.url), kept = await findMedia(url);
  if (!kept) return fetch(request);
  const range = /^bytes=(\d*)-(\d*)$/.exec(request.headers.get('range') || '');
  if (!range) return kept;
  // the audio player asks for pieces of the file ("bytes 300000-"): cut the piece out of the copy
  const blob = await kept.blob(), size = blob.size;
  let start = range[1] === '' ? size - Number(range[2]) : Number(range[1]);
  let end = range[1] === '' || range[2] === '' ? size - 1 : Math.min(Number(range[2]), size - 1);
  if (!(start >= 0) || start > end || start >= size)
    return new Response(null, {status: 416, headers: {'Content-Range': `bytes */${size}`}});
  return new Response(blob.slice(start, end + 1), {status: 206, headers: {
    'Content-Type': kept.headers.get('Content-Type') || 'application/octet-stream',
    'Content-Range': `bytes ${start}-${end}/${size}`, 'Content-Length': String(end - start + 1), 'Accept-Ranges': 'bytes'}});
}

self.addEventListener('fetch', event => {
  const request = event.request, url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;      // saving progress goes straight to the computer
  const path = url.pathname.slice(new URL(self.registration.scope).pathname.length);
  if (path.startsWith('media/')) { event.respondWith(media(request)); return; }
  if (path.startsWith('api/jobs')) return;
  if (path.startsWith('api/')) { event.respondWith(computerFirst(request, API, url.pathname)); return; }
  if (request.mode === 'navigate' || path === '' || path === 'index.html') { event.respondWith(computerFirst(request, SHELL, './')); return; }
  if (SHELL_FILES.includes(path) || path === 'logo.png') { event.respondWith(computerFirst(request, SHELL, path)); return; }
});
