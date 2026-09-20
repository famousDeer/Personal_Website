/* Service worker trybu zakupów (wersja {{ version }}).
 *
 * Zapisuje powłokę aplikacji i jej pliki, żeby lista otwierała się bez
 * połączenia z domowym serwerem. Danych list NIE trzyma - te są w IndexedDB
 * i obsługuje je sama aplikacja. Zapytania do API zawsze idą do sieci.
 * Plik jest generowany przez Django: lista plików ma adresy z hashami,
 * a wersja jest skrótem ich zawartości, więc każde wdrożenie z nowym
 * kodem aplikacji podmienia cache.
 *
 * Zasada: instalacja nie może polec przez jeden poboczny plik (iPhone potrafi
 * uciąć pojedyncze pobranie), bo wtedy nic by się nie zapisało i aplikacja nie
 * otworzyłaby się poza domem. Dlatego wymagamy tylko plików koniecznych,
 * resztę dobieramy w tle, a każda udana odpowiedź z sieci dopisuje się do
 * pamięci - aplikacja sama się uzupełnia przy każdym użyciu w domu.
 */
const VERSION = '{{ version }}';
const CACHE_PREFIX = 'zakupy-';
const CACHE_NAME = CACHE_PREFIX + VERSION;
const PRECACHE = {{ precache_json|safe }};
const REQUIRED = {{ required_json|safe }};
const SHELL_URL = {{ shell_url_json|safe }};
const SCOPE_PREFIX = {{ scope_prefix_json|safe }};
const API_PREFIX = {{ api_prefix_json|safe }};
const NOTIFICATION_ICON = {{ icon_json|safe }};

async function cacheUrl(cache, url) {
    let response;
    try {
        response = await fetch(url, { cache: 'reload', credentials: 'same-origin' });
    } catch (error) {
        // Niektóre przeglądarki potrafią odmówić pobrania z pominięciem cache.
        response = await fetch(url, { credentials: 'same-origin' });
    }
    if (!response.ok || response.redirected) {
        throw new Error('Nie udało się pobrać ' + url + ' (' + response.status + ')');
    }
    await cache.put(url, response);
}

async function cacheRequired(cache, url) {
    try {
        await cacheUrl(cache, url);
    } catch (error) {
        await cacheUrl(cache, url);   // druga próba: chwilowy błąd sieci nie psuje instalacji
    }
}

self.addEventListener('install', (event) => {
    event.waitUntil((async () => {
        const cache = await caches.open(CACHE_NAME);
        // Konieczne: bez nich nie ma czego pokazać w sklepie.
        await Promise.all(REQUIRED.map((url) => cacheRequired(cache, url)));
        // Reszta (ikony, czcionka ikon, manifest) - bez przerywania instalacji.
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

function isAppAsset(url) {
    return PRECACHE.includes(url.pathname) || url.pathname.startsWith('/static/');
}

self.addEventListener('fetch', (event) => {
    const request = event.request;
    if (request.method !== 'GET') {
        return;
    }
    const url = new URL(request.url);
    if (url.origin !== self.location.origin || url.pathname.startsWith(API_PREFIX)) {
        return;
    }

    // Wejście do aplikacji: najpierw pamięć (otwiera się od razu, także w
    // sklepie), a w tle odświeżamy zapis na później.
    const isShellNavigation = request.mode === 'navigate'
        && (url.pathname === SHELL_URL || url.pathname === SHELL_URL.replace(/\/$/, ''));
    if (request.mode === 'navigate' && !isShellNavigation && url.pathname.startsWith(SCOPE_PREFIX)) {
        // Inna strona zakupów (np. ikona dodana do ekranu z listy zakupów):
        // z połączeniem zwykła strona, bez połączenia - zapisany tryb zakupów,
        // zamiast ekranu błędu przeglądarki.
        event.respondWith((async () => {
            try {
                // Bez pamięci przeglądarki: inaczej poza domem dostalibyśmy
                // starą kopię strony, której obrazki i style i tak się nie wczytają.
                return await fetch(request.url, { cache: 'no-store', credentials: 'same-origin' });
            } catch (error) {
                const cache = await caches.open(CACHE_NAME);
                const shell = await cache.match(SHELL_URL);
                if (shell) {
                    return shell;
                }
                throw error;
            }
        })());
        return;
    }
    if (isShellNavigation) {
        event.respondWith((async () => {
            const cache = await caches.open(CACHE_NAME);
            const cached = await cache.match(SHELL_URL);
            const network = fetch(SHELL_URL, { cache: 'reload', credentials: 'same-origin' })
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

    if (isAppAsset(url)) {
        event.respondWith((async () => {
            const cache = await caches.open(CACHE_NAME);
            const cached = await cache.match(url.pathname);
            if (cached) {
                return cached;
            }
            // Pierwsze udane pobranie zapisuje plik na później - dzięki temu
            // brak pliku po nieudanej instalacji naprawia się sam.
            const response = await fetch(request);
            if (response.ok && !response.redirected && request.mode !== 'no-cors') {
                cache.put(url.pathname, response.clone()).catch(() => null);
            }
            return response;
        })());
    }
});

self.addEventListener('message', (event) => {
    const data = event.data;
    if (data === 'skip-waiting') {
        self.skipWaiting();
        return;
    }
    if (data && data.type === 'gotowosc' && event.ports && event.ports[0]) {
        const port = event.ports[0];
        (async () => {
            const cache = await caches.open(CACHE_NAME);
            const keys = await cache.keys();
            const cached = keys.map((request) => new URL(request.url).pathname);
            port.postMessage({
                version: VERSION,
                cached: cached.length,
                total: PRECACHE.length,
                missingRequired: REQUIRED.filter((url) => !cached.includes(url)),
            });
        })().catch(() => port.postMessage({ version: VERSION, cached: 0, total: PRECACHE.length, missingRequired: REQUIRED }));
    }
});

// --- Powiadomienia -------------------------------------------------------
self.addEventListener('push', (event) => {
    let payload = {};
    try {
        payload = event.data ? event.data.json() : {};
    } catch (error) {
        payload = { title: 'Dom', body: event.data ? event.data.text() : '' };
    }
    const title = payload.title || 'Lista zakupów';
    event.waitUntil(self.registration.showNotification(title, {
        body: payload.body || '',
        icon: payload.icon || NOTIFICATION_ICON,
        badge: payload.badge || NOTIFICATION_ICON,
        tag: payload.tag || 'dom',
        renotify: false,
        data: { url: payload.url || SHELL_URL },
    }));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const target = (event.notification.data && event.notification.data.url) || SHELL_URL;
    event.waitUntil((async () => {
        const clientList = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
        for (const client of clientList) {
            if (client.url.includes(SHELL_URL) && 'focus' in client) {
                await client.focus();
                if ('navigate' in client && !client.url.endsWith(target)) {
                    await client.navigate(target).catch(() => null);
                }
                return;
            }
        }
        await self.clients.openWindow(target);
    })());
});
