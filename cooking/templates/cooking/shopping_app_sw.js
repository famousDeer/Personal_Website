/* Service worker trybu zakupów (wersja {{ version }}).
 *
 * Zapisuje powłokę aplikacji i jej pliki, żeby lista otwierała się bez
 * połączenia z domowym serwerem. Danych list NIE trzyma - te są w IndexedDB
 * i obsługuje je sama aplikacja. Zapytania do API zawsze idą do sieci.
 * Plik jest generowany przez Django: lista plików ma adresy z hashami,
 * a wersja jest skrótem ich zawartości, więc każde wdrożenie z nowym
 * kodem aplikacji podmienia cache.
 */
const VERSION = '{{ version }}';
const CACHE_PREFIX = 'zakupy-';
const CACHE_NAME = CACHE_PREFIX + VERSION;
const PRECACHE = {{ precache_json|safe }};
const SHELL_URL = {{ shell_url_json|safe }};
const API_PREFIX = {{ api_prefix_json|safe }};

self.addEventListener('install', (event) => {
    event.waitUntil((async () => {
        const cache = await caches.open(CACHE_NAME);
        // Każdy plik osobno i tylko poprawne odpowiedzi bez przekierowań -
        // nigdy strona logowania zapisana zamiast aplikacji.
        await Promise.all(PRECACHE.map(async (url) => {
            const response = await fetch(url, { cache: 'reload', credentials: 'same-origin' });
            if (!response.ok || response.redirected) {
                throw new Error('Nie udało się pobrać ' + url + ' (' + response.status + ')');
            }
            await cache.put(url, response);
        }));
        await self.skipWaiting();
    })());
});

self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        const names = await caches.keys();
        await Promise.all(names
            .filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME)
            .map((name) => caches.delete(name)));
        await self.clients.claim();
    })());
});

self.addEventListener('fetch', (event) => {
    const request = event.request;
    if (request.method !== 'GET') {
        return;
    }
    const url = new URL(request.url);
    if (url.origin !== self.location.origin || url.pathname.startsWith(API_PREFIX)) {
        return;
    }
    if (request.mode === 'navigate' && url.pathname === SHELL_URL) {
        // Powłoka zawsze z pamięci: otwiera się od razu, także w sklepie.
        // Nowa wersja przychodzi razem z nowym service workerem.
        event.respondWith((async () => {
            const cache = await caches.open(CACHE_NAME);
            const cached = await cache.match(SHELL_URL);
            return cached || fetch(request);
        })());
        return;
    }
    if (PRECACHE.includes(url.pathname)) {
        event.respondWith((async () => {
            const cache = await caches.open(CACHE_NAME);
            return (await cache.match(url.pathname)) || fetch(request);
        })());
    }
});

self.addEventListener('message', (event) => {
    if (event.data === 'skip-waiting') {
        self.skipWaiting();
    }
});
