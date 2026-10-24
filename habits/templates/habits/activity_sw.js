/* Service worker strony aktywności (wersja {{ version }}).
 *
 * Zapisuje stronę i jej pliki, żeby otwierała się bez połączenia z domowym
 * serwerem. Danych nie trzyma - treningi i kolejka zmian są w IndexedDB
 * i obsługuje je sama strona. Zapytania do API zawsze idą do sieci.
 * Wzorowany na service workerze trybu zakupów (cooking/shopping_app_sw.js):
 * instalacja wymaga tylko plików koniecznych, reszta dochodzi w tle, a każda
 * udana odpowiedź z sieci uzupełnia pamięć.
 */
const VERSION = '{{ version }}';
const CACHE_PREFIX = 'aktywnosc-';
const CACHE_NAME = CACHE_PREFIX + VERSION;
const PRECACHE = {{ precache_json|safe }};
const REQUIRED = {{ required_json|safe }};
const SHELL_URL = {{ shell_url_json|safe }};
const API_PREFIX = {{ api_prefix_json|safe }};

async function cacheUrl(cache, url) {
    const response = await fetch(url, { cache: 'reload', credentials: 'same-origin' });
    if (!response.ok || response.redirected) {
        throw new Error('Nie udało się pobrać ' + url + ' (' + response.status + ')');
    }
    await cache.put(url, response);
}

self.addEventListener('install', (event) => {
    event.waitUntil((async () => {
        const cache = await caches.open(CACHE_NAME);
        await Promise.all(REQUIRED.map((url) => cacheUrl(cache, url).catch(() => cacheUrl(cache, url))));
        await Promise.all(PRECACHE
            .filter((url) => !REQUIRED.includes(url))
            .map((url) => cacheUrl(cache, url).catch(() => null)));
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

    // Strona: najpierw pamięć (otwiera się od razu, także poza domem),
    // w tle odświeżamy zapis. Przekierowanie na logowanie nie trafia do pamięci.
    if (request.mode === 'navigate' && url.pathname === SHELL_URL) {
        event.respondWith((async () => {
            const cache = await caches.open(CACHE_NAME);
            const cached = await cache.match(SHELL_URL);
            const network = fetch(SHELL_URL, { cache: 'no-store', credentials: 'same-origin' })
                .then(async (response) => {
                    if (response.ok && !response.redirected) {
                        await cache.put(SHELL_URL, response.clone());
                    }
                    return response;
                });
            if (cached) {
                event.waitUntil(network.catch(() => null));
                return cached;
            }
            return network;
        })());
        return;
    }

    if (url.pathname.startsWith('/static/')) {
        event.respondWith((async () => {
            const cache = await caches.open(CACHE_NAME);
            const cached = await cache.match(url.pathname);
            if (cached) {
                return cached;
            }
            const response = await fetch(request);
            if (response.ok && !response.redirected && request.mode !== 'no-cors') {
                cache.put(url.pathname, response.clone()).catch(() => null);
            }
            return response;
        })());
    }
});

self.addEventListener('message', (event) => {
    if (event.data === 'skip-waiting') {
        self.skipWaiting();
    }
});
