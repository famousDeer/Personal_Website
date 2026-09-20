/* Tryb zakupów offline.
 *
 * Źródłem prawdy jest serwer w domu. Telefon trzyma w IndexedDB:
 *   - ostatni stan list pobrany z serwera ("snapshot"),
 *   - kolejkę zmian zrobionych od tamtej pory ("outbox").
 * Ekran to zawsze snapshot + zmiany z kolejki nałożone po kolei, więc każde
 * kliknięcie widać od razu, także bez połączenia. Gdy serwer jest osiągalny,
 * kolejka idzie na serwer (każda operacja ma własny UUID, więc powtórka po
 * zerwanym połączeniu niczego nie dubluje), a w odpowiedzi przychodzi nowy stan.
 *
 * iOS nie ma synchronizacji w tle, dlatego synchronizujemy przy otwarciu,
 * powrocie do aplikacji, po każdej zmianie i co 30 s, gdy aplikacja jest otwarta.
 */
(() => {
    'use strict';

    const config = JSON.parse(document.getElementById('shopping-app-config').textContent);
    const app = document.querySelector('[data-app]');
    const el = (selector) => app.querySelector(selector);

    const SYNC_INTERVAL_MS = 30000;
    const REQUEST_TIMEOUT_MS = 8000;
    const MAX_BATCH = 200;
    const OP_ADD = 'item.add';
    const OP_SET_PURCHASED = 'item.set_purchased';
    const OP_SET_QUANTITY = 'item.set_quantity';
    const OP_DELETE = 'item.delete';
    const OP_PANTRY_MOVEMENT = 'pantry.movement';
    const OP_SHOP_ADD = 'shop.add';
    const OP_SHOP_ORDER = 'shop.set_order';
    const OP_LIST_SHOP = 'list.set_shop';

    const unitLabels = Object.fromEntries(config.units.map((unit) => [unit.value, unit.label]));
    const categoryOrder = config.categoryGroups.flatMap((group) => group.categories);
    const timeFormat = new Intl.DateTimeFormat('pl-PL', { hour: '2-digit', minute: '2-digit' });
    const dateFormat = new Intl.DateTimeFormat('pl-PL', { day: 'numeric', month: 'numeric' });

    // ------------------------------------------------------------------
    // Pamięć w telefonie (IndexedDB, z zapasem w pamięci operacyjnej)
    // ------------------------------------------------------------------
    const store = (() => {
        const memory = { kv: new Map(), outbox: new Map(), seq: 0 };
        let dbPromise = null;

        // Zapas na wypadek, gdyby IndexedDB nie zadziałało (iPhone potrafi
        // odmówić zapisu w karcie prywatnej albo po wyczyszczeniu danych).
        const backup = {
            read(key) {
                try {
                    const raw = localStorage.getItem('zakupy-' + key);
                    return raw ? JSON.parse(raw) : undefined;
                } catch (error) {
                    return undefined;
                }
            },
            write(key, value) {
                try {
                    localStorage.setItem('zakupy-' + key, JSON.stringify(value));
                } catch (error) {
                    /* brak miejsca albo tryb prywatny */
                }
            },
        };

        function open() {
            if (dbPromise) {
                return dbPromise;
            }
            dbPromise = new Promise((resolve) => {
                if (!('indexedDB' in window)) {
                    resolve(null);
                    return;
                }
                let request;
                try {
                    request = indexedDB.open('lista-zakupow', 1);
                } catch (error) {
                    resolve(null);
                    return;
                }
                request.onupgradeneeded = () => {
                    const db = request.result;
                    if (!db.objectStoreNames.contains('kv')) {
                        db.createObjectStore('kv');
                    }
                    if (!db.objectStoreNames.contains('outbox')) {
                        db.createObjectStore('outbox', { keyPath: 'seq', autoIncrement: true });
                    }
                };
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => resolve(null);
                request.onblocked = () => resolve(null);
            });
            return dbPromise;
        }

        function tx(db, name, mode, work) {
            return new Promise((resolve, reject) => {
                const transaction = db.transaction(name, mode);
                const objectStore = transaction.objectStore(name);
                let result;
                Promise.resolve(work(objectStore)).then((value) => { result = value; });
                transaction.oncomplete = () => resolve(result);
                transaction.onerror = () => reject(transaction.error);
                transaction.onabort = () => reject(transaction.error);
            });
        }

        const requestValue = (request) => new Promise((resolve, reject) => {
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });

        return {
            async persistent() {
                return Boolean(await open());
            },
            async get(key) {
                const db = await open();
                if (!db) {
                    return memory.kv.get(key) ?? backup.read(key);
                }
                try {
                    return (await tx(db, 'kv', 'readonly', (os) => requestValue(os.get(key)))) ?? backup.read(key);
                } catch (error) {
                    return backup.read(key);
                }
            },
            async set(key, value) {
                memory.kv.set(key, value);
                backup.write(key, value);
                const db = await open();
                if (!db) {
                    return;
                }
                await tx(db, 'kv', 'readwrite', (os) => { os.put(value, key); });
            },
            async outboxAll() {
                const db = await open();
                if (!db) {
                    return [...memory.outbox.values()];
                }
                return tx(db, 'outbox', 'readonly', (os) => requestValue(os.getAll()));
            },
            async outboxAdd(op) {
                const db = await open();
                if (!db) {
                    memory.seq += 1;
                    const entry = { seq: memory.seq, op };
                    memory.outbox.set(entry.seq, entry);
                    return entry;
                }
                const seq = await tx(db, 'outbox', 'readwrite', (os) => requestValue(os.add({ op })));
                return { seq, op };
            },
            async outboxDelete(seqs) {
                if (!seqs.length) {
                    return;
                }
                const db = await open();
                if (!db) {
                    seqs.forEach((seq) => memory.outbox.delete(seq));
                    return;
                }
                await tx(db, 'outbox', 'readwrite', (os) => { seqs.forEach((seq) => os.delete(seq)); });
            },
        };
    })();

    // ------------------------------------------------------------------
    // Stan
    // ------------------------------------------------------------------
    const state = {
        snapshot: null,          // ostatni stan z serwera
        outbox: [],              // [{seq, op}] w kolejności wykonania
        meta: { selectedListId: null, lastSyncAt: null, user: '', csrfToken: '' },
        status: 'starting',      // starting | syncing | online | offline | auth | error
        syncing: false,
        syncAgain: false,
        openItem: null,          // uuid pozycji z otwartym edytorem
        confirmDelete: null,     // uuid pozycji czekającej na drugie dotknięcie "Usuń"
        confirmComplete: false,
        renderDeferred: false,
        storageOk: true,
        screen: 'lista',        // lista | spizarnia
        pantrySearch: '',
        scanner: null,          // {status, barcode, product, message}
        showDetails: false,
        showShop: false,
        pushOn: false,
        preparing: false,
        // Co jest zapisane w telefonie na wyjście z domu.
        offline: { checked: false, shell: false, files: 0, total: 0, sw: 'sprawdzam', error: '' },
    };

    // Ten sam wzorzec co w skanerze spiżarni: z http://192.168.x.x nie da się
    // ani włączyć aparatu, ani zapisać aplikacji w telefonie - podpowiadamy
    // adres HTTPS zamiast samego komunikatu o błędzie.
    function secureVersionUrl() {
        if (window.location.protocol !== 'http:'
            || ['localhost', '127.0.0.1', '[::1]'].includes(window.location.hostname)) {
            return '';
        }
        return `https://${window.location.hostname}${window.location.pathname}`;
    }

    function certificateUrl() {
        return `http://${window.location.hostname}/certyfikat`;
    }

    const newId = () => (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
            const r = crypto.getRandomValues(new Uint8Array(1))[0] % 16;
            return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
        });

    function currentView() {
        const view = state.snapshot
            ? JSON.parse(JSON.stringify(state.snapshot))
            : { lists: [], products: [], shops: [] };
        view.shops = view.shops || [];
        const pendingItems = new Set();
        for (const { op } of state.outbox) {
            applyOp(view, op);
            if (op.item) {
                pendingItems.add(op.item);
            }
            if (op.type === OP_ADD) {
                pendingItems.add(op.data.uuid);
            }
        }
        view.pendingItems = pendingItems;
        return view;
    }

    function findItem(view, uuid) {
        for (const list of view.lists) {
            const index = list.items.findIndex((item) => item.uuid === uuid);
            if (index !== -1) {
                return { list, item: list.items[index], index };
            }
        }
        return null;
    }

    function productByName(view, name) {
        const wanted = String(name || '').trim().toLocaleLowerCase('pl');
        return (view.products || []).find((product) => product.name.toLocaleLowerCase('pl') === wanted) || null;
    }

    function applyOp(view, op) {
        if (op.type === OP_PANTRY_MOVEMENT) {
            const product = (view.products || []).find((candidate) => candidate.id === op.product);
            if (!product) {
                return;
            }
            const step = Number(product.package) * op.count;
            let quantity = Number(product.quantity);
            let packages = product.packages;
            if (op.action === 'consume') {
                quantity = Math.max(0, quantity - step);
                packages = Math.max(0, packages - op.count);
                if (quantity === 0) {
                    packages = 0;
                }
            } else {
                quantity += step;
                packages += op.count;
            }
            product.quantity = quantity.toFixed(2);
            product.packages = packages;
            const minimum = Number(product.minimum);
            product.status = quantity <= 0 ? 'empty' : (minimum > 0 && quantity <= minimum ? 'low' : 'ok');
            return;
        }
        if (op.type === OP_SHOP_ADD) {
            if (!view.shops.some((shop) => shop.uuid === op.shop)) {
                view.shops.push({ uuid: op.shop, name: op.name, order: op.order || [] });
            }
            return;
        }
        if (op.type === OP_SHOP_ORDER) {
            const shop = view.shops.find((candidate) => candidate.uuid === op.shop);
            if (shop) {
                shop.order = op.order.concat(shop.order.filter((name) => !op.order.includes(name)));
            }
            return;
        }
        if (op.type === OP_LIST_SHOP) {
            const list = view.lists.find((candidate) => candidate.id === op.list);
            if (list) {
                list.shop = op.shop || '';
            }
            return;
        }
        if (op.type === OP_ADD) {
            const list = view.lists.find((candidate) => candidate.id === op.list);
            if (!list || list.items.some((item) => item.uuid === op.data.uuid)) {
                return;
            }
            const product = productByName(view, op.data.name);
            list.items.push({
                uuid: op.data.uuid,
                name: op.data.name,
                quantity: op.data.quantity,
                unit: op.data.unit,
                unit_label: unitLabels[op.data.unit] || op.data.unit,
                category: op.data.category || (product ? product.category : ''),
                note: op.data.note || '',
                is_purchased: false,
                purchased_by: '',
                in_pantry: Boolean(product),
                added_to_pantry: false,
            });
            return;
        }
        const found = findItem(view, op.item);
        if (!found) {
            return;
        }
        if (op.type === OP_SET_PURCHASED) {
            found.item.is_purchased = op.purchased;
            found.item.purchased_by = op.purchased ? state.meta.user : '';
            found.item.added_to_pantry = op.purchased && found.item.in_pantry;
        } else if (op.type === OP_SET_QUANTITY) {
            found.item.quantity = op.quantity;
        } else if (op.type === OP_DELETE) {
            found.list.items.splice(found.index, 1);
        }
    }

    async function saveMeta() {
        try {
            await store.set('meta', state.meta);
        } catch (error) {
            state.storageOk = false;
        }
    }

    // ------------------------------------------------------------------
    // Zmiany użytkownika
    // ------------------------------------------------------------------
    async function enqueue(op) {
        op.op_id = newId();
        op.at = new Date().toISOString();
        // Kolejne odhaczenia albo zmiany ilości tej samej pozycji zastępują
        // poprzednie, jeszcze niewysłane - liczy się ostatni stan.
        if (op.type === OP_SET_PURCHASED || op.type === OP_SET_QUANTITY || op.type === OP_SHOP_ORDER
            || op.type === OP_LIST_SHOP) {
            const key = op.type === OP_SHOP_ORDER ? 'shop' : (op.type === OP_LIST_SHOP ? 'list' : 'item');
            const superseded = state.outbox.filter((entry) => entry.op.type === op.type && entry.op[key] === op[key]);
            if (superseded.length && !state.syncing) {
                await store.outboxDelete(superseded.map((entry) => entry.seq));
                state.outbox = state.outbox.filter((entry) => !superseded.includes(entry));
            }
        }
        try {
            state.outbox.push(await store.outboxAdd(op));
        } catch (error) {
            state.storageOk = false;
            state.outbox.push({ seq: `mem-${op.op_id}`, op });
        }
        render();
        scheduleSync(350);
    }

    function formatQuantity(value) {
        const number = Number(value);
        if (!Number.isFinite(number)) {
            return String(value);
        }
        return number.toLocaleString('pl-PL', { maximumFractionDigits: 2 });
    }

    function quantityStep(unit) {
        if (unit === 'g' || unit === 'ml') {
            return 100;
        }
        if (unit === 'kg' || unit === 'l') {
            return 0.5;
        }
        return 1;
    }

    function normalizeQuantity(raw, unit) {
        const value = Number(String(raw).replace(',', '.'));
        if (!Number.isFinite(value) || value <= 0) {
            return null;
        }
        if (unit === 'szt' && !Number.isInteger(value)) {
            return null;
        }
        if (value > 99999999.99) {
            return null;
        }
        return (Math.round(value * 100) / 100).toFixed(2);
    }

    // ------------------------------------------------------------------
    // Synchronizacja
    // ------------------------------------------------------------------
    class SyncError extends Error {
        constructor(kind, message) {
            super(message || kind);
            this.kind = kind;
        }
    }

    async function request(url, options = {}) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
        let response;
        try {
            response = await fetch(url, {
                credentials: 'same-origin',
                cache: 'no-store',
                ...options,
                headers: { Accept: 'application/json', ...(options.headers || {}) },
                signal: controller.signal,
            });
        } catch (error) {
            throw new SyncError('offline');
        } finally {
            clearTimeout(timer);
        }
        if (response.status === 401) {
            throw new SyncError('auth');
        }
        if (response.status === 403) {
            throw new SyncError('csrf');
        }
        let data = null;
        try {
            data = await response.json();
        } catch (error) {
            // Odpowiedź nie od naszego serwera (np. strona logowania sieci Wi-Fi).
            throw new SyncError(response.ok ? 'offline' : 'server');
        }
        if (!response.ok) {
            throw new SyncError('server', data && data.message);
        }
        return data;
    }

    const fetchSnapshot = () => request(config.snapshotUrl);

    async function postJson(url, body) {
        const send = () => request(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': state.meta.csrfToken || '' },
            body: JSON.stringify(body),
        });
        try {
            return await send();
        } catch (error) {
            if (error.kind !== 'csrf') {
                throw error;
            }
            // Token wygasł (np. po ponownym zalogowaniu) - pobieramy nowy i ponawiamy raz.
            const fresh = await fetchSnapshot();
            state.meta.csrfToken = fresh.csrf_token;
            try {
                return await send();
            } catch (retryError) {
                throw retryError.kind === 'csrf' ? new SyncError('auth') : retryError;
            }
        }
    }

    function reportResults(results) {
        const problems = results.filter((result) => result.status !== 'applied' && !result.duplicate);
        const warnings = results.filter((result) => result.status === 'applied' && result.message
            && !result.duplicate && !/^Pozycja (już jest|była już)/.test(result.message));
        problems.forEach((result) => toast(result.message || 'Serwer pominął jedną zmianę.', 'warning'));
        warnings.forEach((result) => toast(result.message, 'info'));
    }

    async function acceptServerData(data) {
        state.snapshot = data.snapshot;
        state.meta.user = data.user;
        state.meta.csrfToken = data.csrf_token;
        state.meta.lastSyncAt = new Date().toISOString();
        try {
            await store.set('snapshot', state.snapshot);
        } catch (error) {
            state.storageOk = false;
        }
        await saveMeta();
        if (data.app_version && data.app_version !== config.version) {
            checkForUpdate();
        }
        checkOfflineReady();
    }

    async function sync() {
        if (state.syncing) {
            state.syncAgain = true;
            return;
        }
        state.syncing = true;
        setStatus('syncing');
        try {
            const batch = state.outbox.slice(0, MAX_BATCH);
            let data;
            if (batch.length) {
                data = await postJson(config.syncUrl, { ops: batch.map((entry) => entry.op) });
                reportResults(data.results || []);
                const sent = new Set(batch.map((entry) => entry.seq));
                await store.outboxDelete(batch.map((entry) => entry.seq).filter((seq) => typeof seq === 'number'));
                state.outbox = state.outbox.filter((entry) => !sent.has(entry.seq));
            } else {
                data = await fetchSnapshot();
            }
            await acceptServerData(data);
            setStatus('online');
            if (state.outbox.length) {
                state.syncAgain = true;
            }
        } catch (error) {
            const kind = error instanceof SyncError ? error.kind : 'error';
            setStatus(kind === 'auth' ? 'auth' : kind === 'server' ? 'error' : 'offline');
        } finally {
            state.syncing = false;
            render();
            if (state.syncAgain) {
                state.syncAgain = false;
                setTimeout(sync, 50);
            }
        }
    }

    let syncTimer = null;
    function scheduleSync(delay) {
        clearTimeout(syncTimer);
        syncTimer = setTimeout(sync, delay);
    }

    async function waitForSync() {
        for (let attempt = 0; attempt < 100 && state.syncing; attempt += 1) {
            await new Promise((resolve) => setTimeout(resolve, 100));
        }
    }

    async function completeList(list) {
        await waitForSync();
        if (state.outbox.length) {
            await sync();
            await waitForSync();
        }
        if (state.status !== 'online' || state.outbox.length) {
            toast('Zakończenie listy wymaga połączenia z domowym serwerem.', 'warning');
            return;
        }
        try {
            const data = await postJson(config.completeUrl.replace('/0/', `/${list.id}/`), {});
            await acceptServerData(data);
            toast(data.message || 'Lista zakończona.', 'success');
        } catch (error) {
            toast(error.kind === 'server' && error.message ? error.message
                : 'Nie udało się zakończyć listy. Spróbuj ponownie w domowej sieci.', 'warning');
        }
        state.confirmComplete = false;
        render();
    }

    // ------------------------------------------------------------------
    // Wygląd
    // ------------------------------------------------------------------
    const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[char]));

    function relativeSyncTime() {
        if (!state.meta.lastSyncAt) {
            return '';
        }
        const at = new Date(state.meta.lastSyncAt);
        const today = new Date();
        const sameDay = at.toDateString() === today.toDateString();
        return sameDay ? timeFormat.format(at) : `${dateFormat.format(at)}, ${timeFormat.format(at)}`;
    }

    function pendingLabel(count) {
        if (count === 1) {
            return '1 zmiana czeka';
        }
        const lastDigit = count % 10;
        const lastTwo = count % 100;
        if (lastDigit >= 2 && lastDigit <= 4 && (lastTwo < 12 || lastTwo > 14)) {
            return `${count} zmiany czekają`;
        }
        return `${count} zmian czeka`;
    }

    function setStatus(status) {
        state.status = status;
        renderStatus();
    }

    function renderStatus() {
        const button = el('[data-sync-button]');
        const text = el('[data-sync-text]');
        const pending = state.outbox.length;
        const when = relativeSyncTime();
        let label;
        switch (state.status) {
        case 'syncing':
            label = pending ? `Wysyłam · ${pendingLabel(pending)}` : 'Sprawdzam…';
            break;
        case 'online':
            label = pending ? pendingLabel(pending) : `Aktualna · ${when}`;
            break;
        case 'auth':
            label = 'Zaloguj się';
            break;
        case 'error':
            label = 'Błąd serwera';
            break;
        case 'offline':
            label = pending ? `Offline · ${pendingLabel(pending)}` : (when ? `Offline · stan z ${when}` : 'Offline');
            break;
        default:
            label = 'Uruchamiam…';
        }
        button.dataset.status = state.status;
        text.textContent = label;
        button.title = state.status === 'auth'
            ? 'Sesja wygasła. Dotknij, aby się zalogować - zmiany z kolejki zostaną zachowane.'
            : 'Dotknij, aby zsynchronizować teraz';
        app.classList.toggle('is-offline', state.status === 'offline');
    }

    function selectedList(view) {
        if (!view.lists.length) {
            return null;
        }
        const chosen = view.lists.find((list) => list.id === state.meta.selectedListId);
        if (chosen) {
            return chosen;
        }
        // Zapamiętujemy wybór, żeby lista nie przeskakiwała, gdy inna lista
        // zostanie zmieniona na serwerze i wyjdzie na początek.
        state.meta.selectedListId = view.lists[0].id;
        saveMeta();
        return view.lists[0];
    }

    function currentShop(view, list) {
        if (!list || !list.shop) {
            return null;
        }
        return (view.shops || []).find((shop) => shop.uuid === list.shop) || null;
    }

    function groupItems(items, shopOrder) {
        const groups = new Map();
        for (const item of items) {
            const key = item.category || '';
            if (!groups.has(key)) {
                groups.set(key, []);
            }
            groups.get(key).push(item);
        }
        const shopRank = (shopOrder || []).reduce((acc, name, index) => {
            acc[name] = index;
            return acc;
        }, {});
        const rank = (category) => {
            // Kolejność alejek sklepu wygrywa; reszta kategorii idzie dalej
            // w zwykłej kolejności, a produkty bez kategorii na końcu.
            if (category && shopRank[category] !== undefined) {
                return shopRank[category];
            }
            if (!category) {
                return 10000;
            }
            const index = categoryOrder.indexOf(category);
            return (shopOrder && shopOrder.length ? 1000 : 0) + (index === -1 ? 500 : index);
        };
        return [...groups.entries()]
            .sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0], 'pl'))
            .map(([category, groupItems]) => ({
                category: category || 'Bez kategorii',
                items: groupItems.sort((a, b) => a.name.localeCompare(b.name, 'pl')),
            }));
    }

    function itemHtml(item, view) {
        const pending = view.pendingItems.has(item.uuid);
        const open = state.openItem === item.uuid;
        const meta = [
            `<span class="sa-qty">${escapeHtml(formatQuantity(item.quantity))} ${escapeHtml(item.unit_label || unitLabels[item.unit] || item.unit)}</span>`,
        ];
        if (item.note) {
            meta.push(`<span class="sa-note">${escapeHtml(item.note)}</span>`);
        }
        if (item.is_purchased && item.added_to_pantry) {
            meta.push('<span class="sa-tag is-pantry" title="Zapas w spiżarni został uzupełniony"><i class="bi bi-house-check" aria-hidden="true"></i> w spiżarni</span>');
        } else if (!item.is_purchased && item.in_pantry) {
            meta.push('<span class="sa-tag" title="Po odhaczeniu zapas w spiżarni uzupełni się sam"><i class="bi bi-house" aria-hidden="true"></i> ze spiżarni</span>');
        }
        if (item.is_purchased && item.purchased_by && item.purchased_by !== state.meta.user) {
            meta.push(`<span class="sa-note">kupione przez ${escapeHtml(item.purchased_by)}</span>`);
        }
        if (pending) {
            meta.push('<span class="sa-tag is-pending" title="Zmiana czeka na wysłanie do domowego serwera"><i class="bi bi-cloud-arrow-up" aria-hidden="true"></i> czeka</span>');
        }
        const step = quantityStep(item.unit);
        const editor = open ? `
            <div class="sa-editor" data-editor="${escapeHtml(item.uuid)}">
                <div class="sa-stepper" role="group" aria-label="Ilość">
                    <button type="button" class="sa-step" data-action="dec" data-step="${step}" aria-label="Mniej"><i class="bi bi-dash-lg" aria-hidden="true"></i></button>
                    <input type="number" inputmode="decimal" min="0.01" step="${item.unit === 'szt' ? 1 : 0.01}"
                           value="${escapeHtml(Number(item.quantity))}" data-quantity-input aria-label="Ilość w ${escapeHtml(item.unit_label || item.unit)}">
                    <span class="sa-unit">${escapeHtml(item.unit_label || item.unit)}</span>
                    <button type="button" class="sa-step" data-action="inc" data-step="${step}" aria-label="Więcej"><i class="bi bi-plus-lg" aria-hidden="true"></i></button>
                </div>
                <button type="button" class="sa-delete ${state.confirmDelete === item.uuid ? 'is-confirm' : ''}" data-action="delete">
                    <i class="bi bi-trash3" aria-hidden="true"></i>
                    ${state.confirmDelete === item.uuid ? 'Na pewno usunąć?' : 'Usuń z listy'}
                </button>
                ${item.is_purchased && item.added_to_pantry ? '<p class="sa-editor-hint">Usunięcie z listy zostawia zakup w spiżarni. Aby go cofnąć, odznacz pozycję.</p>' : ''}
            </div>` : '';
        return `
            <li class="sa-item ${item.is_purchased ? 'is-purchased' : ''} ${open ? 'is-open' : ''}" data-uuid="${escapeHtml(item.uuid)}">
                <div class="sa-item-row">
                    <button type="button" class="sa-check" data-action="toggle" aria-pressed="${item.is_purchased}"
                            aria-label="${item.is_purchased ? 'Odznacz' : 'Odhacz'} ${escapeHtml(item.name)}">
                        <i class="bi bi-check-lg" aria-hidden="true"></i>
                    </button>
                    <button type="button" class="sa-item-body" data-action="edit" aria-expanded="${open}">
                        <span class="sa-item-name">${escapeHtml(item.name)}</span>
                        <span class="sa-item-meta">${meta.join('')}</span>
                    </button>
                </div>
                ${editor}
            </li>`;
    }

    function renderOfflineChip() {
        const chip = el('[data-offline-chip]');
        const text = el('[data-offline-text]');
        const ready = offlineReady();
        chip.dataset.state = state.preparing ? 'praca' : (!state.offline.checked ? 'sprawdzam' : (ready ? 'gotowe' : 'brak'));
        text.textContent = state.preparing ? 'Przygotowuję…'
            : (!state.offline.checked ? 'Sprawdzam…' : (ready ? 'Gotowe offline' : 'Tryb offline'));
        chip.title = ready
            ? 'Lista i aplikacja są zapisane w telefonie - możesz wyjść z domu'
            : 'Aplikacja nie jest jeszcze zapisana w telefonie. Dotknij, żeby zobaczyć szczegóły.';
        chip.setAttribute('aria-expanded', String(state.showDetails));
    }

    function renderDetails() {
        const container = el('[data-details]');
        if (!state.showDetails) {
            container.innerHTML = '';
            return;
        }
        const view = currentView();
        const items = view.lists.reduce((sum, list) => sum + list.items.length, 0);
        const standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
        const rows = [
            ['Adres', window.location.origin + window.location.pathname],
            ['Połączenie', window.isSecureContext
                ? 'bezpieczne (https)'
                : 'niezabezpieczone - tryb offline nie zadziała'],
            ['Uruchomiona', standalone ? 'z ikony na ekranie telefonu' : 'w przeglądarce'],
            ['Aplikacja zapisana w telefonie', state.offline.shell
                ? `tak (${state.offline.files} z ${state.offline.total} plików)`
                : 'nie - poza domem się nie otworzy'],
            ['Mechanizm offline', state.offline.error ? `błąd: ${state.offline.error}` : state.offline.sw],
            ['Lista zapisana w telefonie', state.snapshot
                ? `${items} pozycji z ${view.lists.length} list, zapis ${relativeSyncTime() || 'nieznany'}`
                : 'nie'],
            ['Zalogowany', state.meta.user || 'nie'],
            ['Zmiany czekające na wysłanie', String(state.outbox.length)],
            ['Zapis danych', state.storageOk ? 'pamięć telefonu (IndexedDB)' : 'tylko do zamknięcia karty'],
            ['Powiadomienia', !pushSupported
                ? 'niedostępne w tej przeglądarce'
                : (state.pushOn ? 'włączone na tym telefonie' : 'wyłączone')],
            ['Liczba na ikonie', !badgeSupported
                ? 'niedostępna w tej przeglądarce'
                : (badgeAllowed() ? 'włączona' : 'wyłączona')],
            ['Wersja aplikacji', config.version],
        ];
        const insecureUrl = secureVersionUrl();
        container.innerHTML = `
            <section class="sa-details">
                <div class="sa-details-head">
                    <strong>Stan aplikacji</strong>
                    <button type="button" class="sa-icon-button" data-details-close aria-label="Zamknij szczegóły">
                        <i class="bi bi-x-lg" aria-hidden="true"></i>
                    </button>
                </div>
                <dl>${rows.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`).join('')}</dl>
                ${pushSupported && !insecureUrl ? `
                    <button type="button" class="sa-secondary" data-push="${state.pushOn ? 'off' : 'on'}">
                        <i class="bi bi-bell${state.pushOn ? '-slash' : ''}" aria-hidden="true"></i>
                        ${state.pushOn ? 'Wyłącz powiadomienia' : 'Włącz powiadomienia'}
                    </button>` : ''}
                ${badgeSupported && !badgeAllowed() && !insecureUrl ? `
                    <button type="button" class="sa-secondary" data-badge>
                        <i class="bi bi-1-circle" aria-hidden="true"></i> Pokazuj liczbę na ikonie
                    </button>` : ''}
                ${insecureUrl ? `
                    <a class="sa-secondary" href="${escapeHtml(insecureUrl)}">
                        <i class="bi bi-shield-lock" aria-hidden="true"></i> Otwórz przez HTTPS
                    </a>
                    <p class="sa-details-hint">
                        Potem dodaj ten ekran do ekranu początkowego jeszcze raz - ikona zapamiętuje adres.
                        Pierwszy raz na tym telefonie? <a href="${escapeHtml(certificateUrl())}">Zainstaluj certyfikat</a>.
                    </p>` : `
                    <button type="button" class="sa-secondary" data-prepare ${state.preparing ? 'disabled' : ''}>
                        <i class="bi bi-arrow-repeat" aria-hidden="true"></i>
                        ${state.preparing ? 'Przygotowuję…' : 'Przygotuj tryb offline'}
                    </button>
                    <p class="sa-details-hint">Przygotowanie działa tylko w domowej sieci: pobiera listę i zapisuje aplikację w telefonie.</p>`}
            </section>`;
    }

    function renderShopPanel(view, list) {
        const container = el('[data-shop]');
        if (!state.showShop || !list) {
            container.innerHTML = '';
            return;
        }
        const shop = currentShop(view, list);
        const categories = groupItems(list.items.filter((item) => !item.is_purchased), shop && shop.order)
            .map((group) => group.category);
        const shopOptions = [{ uuid: '', name: 'Bez sklepu (alfabetycznie)' }, ...(view.shops || [])]
            .map((candidate) => `
                <label class="sa-shop-option ${(list.shop || '') === candidate.uuid ? 'is-active' : ''}">
                    <input type="radio" name="sa-shop" value="${escapeHtml(candidate.uuid)}"
                           ${(list.shop || '') === candidate.uuid ? 'checked' : ''} data-shop-pick>
                    <span>${escapeHtml(candidate.name)}</span>
                </label>`).join('');
        const orderRows = categories.map((category, index) => `
            <li>
                <span>${escapeHtml(category)}</span>
                <span class="sa-order-buttons">
                    <button type="button" class="sa-step" data-move="up" data-category="${escapeHtml(category)}"
                            aria-label="Wyżej: ${escapeHtml(category)}" ${index === 0 ? 'disabled' : ''}>
                        <i class="bi bi-chevron-up" aria-hidden="true"></i>
                    </button>
                    <button type="button" class="sa-step" data-move="down" data-category="${escapeHtml(category)}"
                            aria-label="Niżej: ${escapeHtml(category)}" ${index === categories.length - 1 ? 'disabled' : ''}>
                        <i class="bi bi-chevron-down" aria-hidden="true"></i>
                    </button>
                </span>
            </li>`).join('');
        container.innerHTML = `
            <section class="sa-details">
                <div class="sa-details-head">
                    <strong>Sklep i kolejność alejek</strong>
                    <button type="button" class="sa-icon-button" data-shop-close aria-label="Zamknij">
                        <i class="bi bi-x-lg" aria-hidden="true"></i>
                    </button>
                </div>
                <div class="sa-shop-options">${shopOptions}</div>
                <form class="sa-shop-new" data-shop-form>
                    <input type="text" maxlength="80" placeholder="Nowy sklep, np. Lidl" data-shop-name aria-label="Nazwa nowego sklepu">
                    <button type="submit" class="sa-secondary"><i class="bi bi-plus-lg" aria-hidden="true"></i> Dodaj</button>
                </form>
                ${shop ? `
                    <p class="sa-details-hint">Ustaw kategorie w kolejności alejek w sklepie „${escapeHtml(shop.name)}”. Zmiany zapisują się też bez połączenia.</p>
                    <ol class="sa-order-list">${orderRows}</ol>
                ` : '<p class="sa-details-hint">Wybierz sklep, żeby ustawić kolejność alejek.</p>'}
            </section>`;
    }

    function renderShopChip(view, list) {
        const chip = el('[data-shop-chip]');
        const shop = currentShop(view, list);
        chip.hidden = !list;
        chip.dataset.state = shop ? 'sklep' : 'brak';
        el('[data-shop-text]').textContent = shop ? shop.name : 'Sklep';
        chip.title = shop
            ? `Kolejność alejek według sklepu „${shop.name}” - dotknij, żeby zmienić`
            : 'Wybierz sklep, żeby ułożyć listę w kolejności alejek';
        chip.setAttribute('aria-expanded', String(state.showShop));
    }

    function renderBanners(view) {
        const banners = [];
        const secureUrl = secureVersionUrl();
        if (!window.isSecureContext) {
            banners.push(`
                <div class="sa-banner is-warning">
                    <i class="bi bi-shield-exclamation" aria-hidden="true"></i>
                    <div>
                        <strong>Ten adres nie działa bez połączenia z domem.</strong>
                        Telefon zapisuje aplikację tylko przy bezpiecznym połączeniu (https).
                        ${secureUrl ? 'Otwórz ten sam ekran przez HTTPS i dodaj go do ekranu początkowego jeszcze raz.' : ''}
                    </div>
                    ${secureUrl ? `<a class="sa-banner-action" href="${escapeHtml(secureUrl)}">Otwórz przez HTTPS</a>` : ''}
                </div>`);
        }
        if (window.isSecureContext && state.offline.checked && !state.offline.shell && state.status !== 'offline') {
            banners.push(`
                <div class="sa-banner is-warning">
                    <i class="bi bi-exclamation-triangle" aria-hidden="true"></i>
                    <div>
                        <strong>Aplikacja nie jest jeszcze zapisana w telefonie.</strong>
                        Poza domem nie uda się jej otworzyć.${state.offline.error ? ` Powód: ${escapeHtml(state.offline.error)}` : ''}
                    </div>
                    <button type="button" class="sa-banner-action" data-prepare ${state.preparing ? 'disabled' : ''}>
                        ${state.preparing ? 'Chwila…' : 'Przygotuj'}
                    </button>
                </div>`);
        }
        if (state.status === 'auth') {
            banners.push(`
                <div class="sa-banner is-warning">
                    <i class="bi bi-person-lock" aria-hidden="true"></i>
                    <div><strong>Sesja wygasła.</strong> Zmiany z telefonu są zapisane i wyślą się po zalogowaniu.</div>
                    <a class="sa-banner-action" href="${escapeHtml(config.loginUrl)}">Zaloguj</a>
                </div>`);
        }
        if (!state.snapshot && state.status === 'offline') {
            banners.push(`
                <div class="sa-banner">
                    <i class="bi bi-wifi-off" aria-hidden="true"></i>
                    <div><strong>Brak zapisanej listy.</strong> Otwórz aplikację raz w domowej sieci - lista zapisze się w telefonie i będzie dostępna także w sklepie.</div>
                </div>`);
        }
        if (!state.storageOk) {
            banners.push(`
                <div class="sa-banner is-warning">
                    <i class="bi bi-exclamation-triangle" aria-hidden="true"></i>
                    <div>Przeglądarka nie pozwala zapisać listy w telefonie (tryb prywatny?). Zmiany trzymam tylko do zamknięcia karty.</div>
                </div>`);
        }
        if (installHint.visible) {
            banners.push(`
                <div class="sa-banner is-install">
                    <i class="bi bi-phone" aria-hidden="true"></i>
                    <div>${installHint.html}</div>
                    ${installHint.canPrompt ? '<button type="button" class="sa-banner-action" data-install>Zainstaluj</button>' : ''}
                    <button type="button" class="sa-icon-button" data-install-dismiss aria-label="Ukryj"><i class="bi bi-x-lg" aria-hidden="true"></i></button>
                </div>`);
        }
        el('[data-banners]').innerHTML = banners.join('');
        void view;
    }

    function renderScreenSwitch(view) {
        const hasPantry = Boolean((view.products || []).length);
        const switcher = el('[data-screen-switch]');
        switcher.hidden = !hasPantry;
        switcher.querySelectorAll('[data-screen]').forEach((button) => {
            const active = button.dataset.screen === state.screen;
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-pressed', String(active));
        });
        app.classList.toggle('is-pantry', state.screen === 'spizarnia');
    }

    function render() {
        const active = document.activeElement;
        if (active && active.matches && active.matches('[data-quantity-input], [data-pantry-search]') && app.contains(active)) {
            // Nie przerysowujemy listy pod palcem, gdy ktoś wpisuje ilość.
            state.renderDeferred = true;
            renderStatus();
            return;
        }
        state.renderDeferred = false;
        const view = currentView();
        const list = selectedList(view);
        renderStatus();
        renderOfflineChip();
        renderScreenSwitch(view);
        renderShopChip(view, list);
        renderShopPanel(view, list);
        renderDetails();
        renderBanners(view);

        const select = el('[data-list-select]');
        const manyLists = view.lists.length > 1;
        select.hidden = !manyLists;
        el('[data-title-line]').classList.toggle('is-switchable', manyLists);
        el('[data-list-eyebrow]').textContent = manyLists && list
            ? `Lista zakupów · ${view.lists.indexOf(list) + 1} z ${view.lists.length}`
            : 'Lista zakupów';
        select.innerHTML = view.lists.map((candidate) => {
            const left = candidate.items.filter((item) => !item.is_purchased).length;
            return `<option value="${candidate.id}" ${list && candidate.id === list.id ? 'selected' : ''}>${escapeHtml(candidate.title)} (${left})</option>`;
        }).join('');

        const title = el('[data-list-title]');
        const container = el('[data-list]');
        const footer = el('[data-footer]');
        const progress = el('[data-progress]');
        renderSuggestions(view);

        if (!list) {
            title.textContent = state.snapshot ? 'Brak aktywnych list' : 'Lista zakupów';
            footer.hidden = true;
            progress.hidden = true;
            el('[data-progress-text]').textContent = '';
            renderScanner(view, list);
            container.innerHTML = state.snapshot ? `
                <div class="sa-empty">
                    <i class="bi bi-cart-check" aria-hidden="true"></i>
                    <h2>Nie ma aktywnej listy</h2>
                    <p>Utwórz listę w domu: <strong>Kuchnia → Lista zakupów</strong>. Pojawi się tu przy następnej synchronizacji.</p>
                </div>` : (state.status === 'offline' ? '' : '<div class="sa-empty"><div class="sa-spinner" aria-hidden="true"></div><p>Pobieram listę…</p></div>');
            updateBadge(view);
            return;
        }

        title.textContent = list.title;
        footer.hidden = false;
        if (state.screen === 'spizarnia') {
            container.innerHTML = renderPantry(view, list);
            progress.hidden = true;
            el('[data-progress-text]').textContent = `${(view.products || []).length} produktów`;
            // W spiżarni liczy się tylko skanowanie - reszta przycisków znika.
            el('[data-add-open]').hidden = true;
            el('[data-complete-button]').hidden = true;
            el('[data-scan-open]').classList.remove('is-icon');
            el('[data-scan-label]').textContent = 'Skanuj kod';
            renderScanner(view, list);
            updateBadge(view);
            return;
        }
        const total = list.items.length;
        const done = list.items.filter((item) => item.is_purchased).length;
        progress.hidden = total === 0;
        el('[data-progress-bar]').style.width = total ? `${Math.round((done / total) * 100)}%` : '0';
        el('[data-progress-text]').textContent = total ? `${done} z ${total} w koszyku` : '';

        const toBuy = list.items.filter((item) => !item.is_purchased);
        const bought = list.items.filter((item) => item.is_purchased);
        const shop = currentShop(view, list);
        const sections = groupItems(toBuy, shop && shop.order).map((group) => `
            <section class="sa-group">
                <h2 class="sa-group-title">${escapeHtml(group.category)} <span>${group.items.length}</span></h2>
                <ul class="sa-items">${group.items.map((item) => itemHtml(item, view)).join('')}</ul>
            </section>`);
        if (!toBuy.length && total) {
            sections.push(`
                <div class="sa-empty is-done">
                    <i class="bi bi-bag-check-fill" aria-hidden="true"></i>
                    <h2>Wszystko w koszyku</h2>
                    <p>Zakończ listę w domu albo teraz, jeśli masz połączenie.</p>
                </div>`);
        }
        if (!total) {
            sections.push(`
                <div class="sa-empty">
                    <i class="bi bi-basket" aria-hidden="true"></i>
                    <h2>Lista jest pusta</h2>
                    <p>Dodaj pierwszy produkt przyciskiem na dole.</p>
                </div>`);
        }
        if (bought.length) {
            sections.push(`
                <section class="sa-group is-bought">
                    <h2 class="sa-group-title">W koszyku <span>${bought.length}</span></h2>
                    <ul class="sa-items">${bought.sort((a, b) => a.name.localeCompare(b.name, 'pl')).map((item) => itemHtml(item, view)).join('')}</ul>
                </section>`);
        }
        container.innerHTML = sections.join('');
        renderScanner(view, list);
        updateBadge(view);

        const completeButton = el('[data-complete-button]');
        el('[data-add-open]').hidden = false;
        completeButton.hidden = false;
        // Na liście skaner jest tylko ikoną, żeby zmieściły się trzy przyciski.
        el('[data-scan-open]').classList.add('is-icon');
        el('[data-scan-label]').textContent = '';
        const canComplete = state.status === 'online' && !state.outbox.length && done > 0;
        completeButton.disabled = !canComplete;
        completeButton.classList.toggle('is-confirm', state.confirmComplete);
        el('[data-complete-label]').textContent = state.confirmComplete ? 'Na pewno zakończyć?' : 'Zakończ listę';
        completeButton.title = canComplete ? 'Kończy listę; kupione produkty są już w spiżarni'
            : (done ? 'Zakończenie listy wymaga połączenia z domowym serwerem' : 'Odhacz najpierw kupione produkty');
    }

    let lastSuggestionsKey = '';
    function renderSuggestions(view) {
        const products = view.products || [];
        const key = products.map((product) => product.name).join('|');
        if (key === lastSuggestionsKey) {
            return;
        }
        lastSuggestionsKey = key;
        el('[data-product-suggestions]').innerHTML = products
            .map((product) => `<option value="${escapeHtml(product.name)}"></option>`).join('');
    }

    // ------------------------------------------------------------------
    // Spiżarnia w telefonie
    // ------------------------------------------------------------------
    const STATUS_LABELS = { empty: 'brak', low: 'mało', ok: 'jest' };

    function pantryProducts(view) {
        const search = state.pantrySearch.trim().toLocaleLowerCase('pl');
        return (view.products || [])
            .filter((product) => !search || product.name.toLocaleLowerCase('pl').includes(search))
            .sort((a, b) => a.name.localeCompare(b.name, 'pl'));
    }

    function productOnList(view, list, product) {
        if (!list) {
            return null;
        }
        return list.items.find(
            (item) => item.name.toLocaleLowerCase('pl') === product.name.toLocaleLowerCase('pl'),
        ) || null;
    }

    function renderPantry(view, list) {
        const products = pantryProducts(view);
        const rows = products.map((product) => {
            const onList = productOnList(view, list, product);
            return `
                <li class="sa-item sa-pantry-item" data-product="${product.id}">
                    <div class="sa-item-row">
                        <div class="sa-item-body">
                            <span class="sa-item-name">${escapeHtml(product.name)}</span>
                            <span class="sa-item-meta">
                                <span class="sa-qty">${escapeHtml(formatQuantity(product.quantity))} ${escapeHtml(product.unit_label)}</span>
                                <span class="sa-tag is-stock-${escapeHtml(product.status)}">${STATUS_LABELS[product.status] || product.status}</span>
                                ${product.tracks_packages ? `<span class="sa-note">${product.packages} opak.</span>` : ''}
                                ${onList ? '<span class="sa-tag"><i class="bi bi-cart" aria-hidden="true"></i> na liście</span>' : ''}
                            </span>
                        </div>
                        <div class="sa-pantry-actions">
                            <button type="button" class="sa-step" data-pantry="consume" aria-label="Zużyto jedno opakowanie: ${escapeHtml(product.name)}">
                                <i class="bi bi-dash-lg" aria-hidden="true"></i>
                            </button>
                            <button type="button" class="sa-step" data-pantry="purchase" aria-label="Dokupiono jedno opakowanie: ${escapeHtml(product.name)}">
                                <i class="bi bi-plus-lg" aria-hidden="true"></i>
                            </button>
                            ${onList ? '' : `<button type="button" class="sa-step" data-pantry="to-list" aria-label="Dopisz do listy: ${escapeHtml(product.name)}">
                                <i class="bi bi-cart-plus" aria-hidden="true"></i>
                            </button>`}
                        </div>
                    </div>
                </li>`;
        }).join('');
        return `
            <div class="sa-pantry-search">
                <i class="bi bi-search" aria-hidden="true"></i>
                <input type="search" value="${escapeHtml(state.pantrySearch)}" placeholder="Szukaj produktu"
                       aria-label="Szukaj w spiżarni" data-pantry-search>
            </div>
            ${products.length ? `<ul class="sa-items">${rows}</ul>` : `
                <div class="sa-empty">
                    <i class="bi bi-box-seam" aria-hidden="true"></i>
                    <h2>${state.pantrySearch ? 'Nic nie znaleziono' : 'Spiżarnia jest pusta'}</h2>
                    <p>${state.pantrySearch ? 'Zmień wyszukiwanie.' : 'Produkty dodajesz w domu, w zakładce Spiżarnia.'}</p>
                </div>`}`;
    }

    // ------------------------------------------------------------------
    // Skaner kodów (ta sama biblioteka co w spiżarni, ładowana na żądanie)
    // ------------------------------------------------------------------
    let zxingPromise = null;
    let scannerControls = null;
    let scannerStream = null;

    function loadZxing() {
        if (window.ZXingBrowser) {
            return Promise.resolve(window.ZXingBrowser);
        }
        if (!zxingPromise) {
            zxingPromise = new Promise((resolve, reject) => {
                const script = document.createElement('script');
                script.src = config.zxingUrl;
                script.async = true;
                script.onload = () => (window.ZXingBrowser
                    ? resolve(window.ZXingBrowser)
                    : reject(new Error('Nie udało się uruchomić czytnika kodów.')));
                script.onerror = () => reject(new Error('Nie udało się wczytać czytnika kodów.'));
                document.head.appendChild(script);
            });
        }
        return zxingPromise;
    }

    function cameraErrorMessage(error) {
        if (error && error.name === 'NotAllowedError') {
            return 'Brak dostępu do aparatu. Zezwól na kamerę w ustawieniach telefonu.';
        }
        if (error && error.name === 'NotFoundError') {
            return 'Nie znaleziono aparatu w tym urządzeniu.';
        }
        if (error && error.name === 'NotReadableError') {
            return 'Aparat jest zajęty przez inną aplikację.';
        }
        return (error && error.message) ? error.message : 'Nie udało się włączyć aparatu.';
    }

    function stopScannerStream() {
        if (scannerControls) {
            scannerControls.stop();
            scannerControls = null;
        }
        if (scannerStream) {
            scannerStream.getTracks().forEach((track) => track.stop());
            scannerStream = null;
        }
    }

    async function openScanner() {
        if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            toast('Aparat wymaga bezpiecznego połączenia (https).', 'warning');
            return;
        }
        stopScannerStream();
        state.scanner = { status: 'start' };
        render();
        try {
            // Obraz bierzemy sami (tak jak skaner spiżarni), a czytnik dostaje
            // gotowy strumień: inaczej sam szuka kamery, zanim przeglądarka da
            // do niej dostęp, i kończy się to błędem "nie znaleziono urządzenia".
            let stream;
            try {
                stream = await navigator.mediaDevices.getUserMedia({
                    audio: false,
                    video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } },
                });
            } catch (error) {
                // Część urządzeń odrzuca dokładne wymagania (albo aparat jest
                // zajęty przez chwilę po odblokowaniu) - próbujemy prościej,
                // tak samo jak skaner w spiżarni.
                stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: true });
            }
            scannerStream = stream;
            const ZXingBrowser = await loadZxing();
            if (!state.scanner) {
                stopScannerStream();
                return;
            }
            const video = el('[data-scanner-video]');
            video.srcObject = stream;
            await video.play().catch(() => null);
            const reader = new ZXingBrowser.BrowserMultiFormatReader();
            scannerControls = await reader.decodeFromStream(stream, video, (result, error, activeControls) => {
                if (!result || !state.scanner || state.scanner.status !== 'scan') {
                    return;
                }
                const barcode = typeof result.getText === 'function' ? result.getText() : result.text;
                activeControls.stop();
                scannerControls = null;
                handleBarcode(barcode);
            });
            if (!state.scanner) {
                stopScannerStream();
                return;
            }
            state.scanner.status = 'scan';
            render();
        } catch (error) {
            stopScannerStream();
            state.scanner = { status: 'error', message: cameraErrorMessage(error) };
            render();
        }
    }

    function closeScanner() {
        stopScannerStream();
        state.scanner = null;
        render();
    }

    function handleBarcode(barcode) {
        const view = currentView();
        const product = (view.products || []).find(
            (candidate) => candidate.barcode && candidate.barcode === barcode,
        );
        if (navigator.vibrate) {
            navigator.vibrate(18);
        }
        state.scanner = { status: 'result', barcode, product: product || null };
        render();
    }

    async function scannerAction(action) {
        const product = state.scanner && state.scanner.product;
        if (!product) {
            return;
        }
        const view = currentView();
        const list = selectedList(view);
        if (action === 'check') {
            const item = productOnList(view, list, product);
            if (item) {
                await enqueue({ type: OP_SET_PURCHASED, item: item.uuid, purchased: true });
                toast(`Odhaczono: ${product.name}`, 'success');
            }
        } else {
            await enqueue({ type: OP_PANTRY_MOVEMENT, product: product.id, action, count: 1 });
            toast(action === 'consume' ? `Zużyto: ${product.name}` : `Dokupiono: ${product.name}`, 'success');
        }
        // W sklepie skanuje się kilka rzeczy z rzędu, więc wracamy do aparatu.
        openScanner();
    }

    // Podgląd z aparatu rysujemy RAZ: każde ponowne wstawienie HTML zabrałoby
    // elementowi wideo strumień z kamery i skanowanie stawałoby w miejscu.
    let scannerMarkup = null;

    function scannerHintText(scanner) {
        if (scanner.status === 'error') {
            return scanner.message;
        }
        return scanner.status === 'start' ? 'Włączam aparat…' : 'Skieruj aparat na kod kreskowy';
    }

    function renderScanner(view, list) {
        const container = el('[data-scanner]');
        if (!state.scanner) {
            container.hidden = true;
            container.innerHTML = '';
            scannerMarkup = null;
            return;
        }
        container.hidden = false;
        const scanner = state.scanner;
        if (scanner.status === 'result') {
            const product = scanner.product;
            const item = product ? productOnList(view, list, product) : null;
            container.innerHTML = `
                <div class="sa-scanner-sheet">
                    ${product ? `
                        <strong>${escapeHtml(product.name)}</strong>
                        <p>W spiżarni: ${escapeHtml(formatQuantity(product.quantity))} ${escapeHtml(product.unit_label)}
                           · opakowanie ${escapeHtml(formatQuantity(product.package))} ${escapeHtml(product.unit_label)}</p>
                        <div class="sa-scanner-actions">
                            ${item && !item.is_purchased ? '<button type="button" class="sa-primary" data-scan-action="check"><i class="bi bi-check-lg" aria-hidden="true"></i> Odhacz z listy</button>' : ''}
                            <button type="button" class="sa-secondary" data-scan-action="purchase"><i class="bi bi-plus-lg" aria-hidden="true"></i> Dokupiono</button>
                            <button type="button" class="sa-secondary" data-scan-action="consume"><i class="bi bi-dash-lg" aria-hidden="true"></i> Zużyto</button>
                        </div>` : `
                        <strong>Nieznany kod</strong>
                        <p>${escapeHtml(scanner.barcode)} — tego kodu nie ma jeszcze w spiżarni. Dodasz go w domu, w zakładce Spiżarnia.</p>`}
                    <div class="sa-scanner-actions">
                        <button type="button" class="sa-secondary" data-scan-again><i class="bi bi-upc-scan" aria-hidden="true"></i> Skanuj dalej</button>
                        <button type="button" class="sa-secondary" data-scan-close>Zamknij</button>
                    </div>
                </div>`;
            scannerMarkup = 'result';
            return;
        }
        if (scannerMarkup !== 'camera') {
            container.innerHTML = `
                <div class="sa-scanner-camera">
                    <video data-scanner-video playsinline muted autoplay></video>
                    <p class="sa-scanner-hint"></p>
                    <button type="button" class="sa-secondary" data-scan-close>Zamknij</button>
                </div>`;
            scannerMarkup = 'camera';
        }
        const hint = container.querySelector('.sa-scanner-hint');
        if (hint) {
            hint.textContent = scannerHintText(scanner);
        }
    }

    // ------------------------------------------------------------------
    // Liczba na ikonie aplikacji
    // ------------------------------------------------------------------
    const badgeSupported = 'setAppBadge' in navigator;

    function badgeAllowed() {
        return 'Notification' in window && Notification.permission === 'granted';
    }

    function updateBadge(view) {
        if (!badgeSupported) {
            return;
        }
        const left = view.lists.reduce(
            (sum, list) => sum + list.items.filter((item) => !item.is_purchased).length,
            0,
        );
        try {
            if (left > 0) {
                navigator.setAppBadge(left).catch(() => null);
            } else if (navigator.clearAppBadge) {
                navigator.clearAppBadge().catch(() => null);
            }
        } catch (error) {
            /* iPhone pokazuje liczbę dopiero po zgodzie na powiadomienia */
        }
    }

    // ------------------------------------------------------------------
    // Powiadomienia push
    // ------------------------------------------------------------------
    const pushSupported = 'serviceWorker' in navigator && 'PushManager' in window
        && 'Notification' in window && Boolean(config.vapidPublicKey);

    function urlBase64ToUint8Array(base64) {
        const padded = (base64 + '='.repeat((4 - (base64.length % 4)) % 4)).replace(/-/g, '+').replace(/_/g, '/');
        const raw = atob(padded);
        return Uint8Array.from([...raw].map((char) => char.charCodeAt(0)));
    }

    async function currentPushSubscription() {
        if (!pushSupported) {
            return null;
        }
        try {
            const reg = await navigator.serviceWorker.getRegistration(config.swScope || config.scope);
            return reg ? await reg.pushManager.getSubscription() : null;
        } catch (error) {
            return null;
        }
    }

    async function refreshPushState() {
        state.pushOn = Boolean(await currentPushSubscription());
        render();
    }

    async function enablePush() {
        if (!pushSupported) {
            toast('Ten telefon nie obsługuje powiadomień z aplikacji.', 'warning');
            return;
        }
        // iPhone pyta o zgodę tylko w odpowiedzi na dotknięcie przycisku.
        const permission = await Notification.requestPermission().catch(() => 'denied');
        if (permission !== 'granted') {
            toast('Bez zgody na powiadomienia nic nie przyjdzie.', 'warning');
            return;
        }
        try {
            const reg = await navigator.serviceWorker.ready;
            const subscription = await reg.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: urlBase64ToUint8Array(config.vapidPublicKey),
            });
            const data = await postJson(config.pushSubscribeUrl, { subscription: subscription.toJSON() });
            await acceptServerData(data);
            state.pushOn = true;
            updateBadge(currentView());
            toast(data.message || 'Powiadomienia włączone.', 'success');
        } catch (error) {
            toast('Nie udało się włączyć powiadomień: ' + ((error && error.message) || 'błąd'), 'warning');
        }
        render();
    }

    async function disablePush() {
        const subscription = await currentPushSubscription();
        if (!subscription) {
            state.pushOn = false;
            render();
            return;
        }
        const endpoint = subscription.endpoint;
        await subscription.unsubscribe().catch(() => null);
        try {
            await postJson(config.pushUnsubscribeUrl, { endpoint });
            toast('Powiadomienia wyłączone na tym telefonie.', 'info');
        } catch (error) {
            toast('Wyłączone na telefonie, ale serwer jeszcze o tym nie wie.', 'warning');
        }
        state.pushOn = false;
        render();
    }

    async function askForBadge() {
        if (!('Notification' in window)) {
            toast('Ta przeglądarka nie pokazuje liczby na ikonie.', 'warning');
            return;
        }
        // iPhone pyta o zgodę tylko w odpowiedzi na dotknięcie przycisku.
        const result = await Notification.requestPermission().catch(() => 'denied');
        if (result === 'granted') {
            updateBadge(currentView());
            toast('Gotowe. Na ikonie zobaczysz, ile produktów zostało.', 'success');
        } else {
            toast('Bez zgody na powiadomienia iPhone nie pokaże liczby na ikonie.', 'warning');
        }
        render();
    }

    function toast(message, kind = 'info') {
        const container = el('[data-toasts]');
        const node = document.createElement('div');
        node.className = `sa-toast is-${kind}`;
        node.setAttribute('role', kind === 'warning' ? 'alert' : 'status');
        node.textContent = message;
        container.appendChild(node);
        setTimeout(() => node.classList.add('is-leaving'), 4200);
        setTimeout(() => node.remove(), 4700);
    }

    // ------------------------------------------------------------------
    // Dodawanie produktu
    // ------------------------------------------------------------------
    const addPanel = el('[data-add-panel]');
    const addForm = el('[data-add-form]');
    const addName = el('[data-add-name]');
    const addQuantity = el('[data-add-quantity]');
    const addUnit = el('[data-add-unit]');
    const addCategory = el('[data-add-category]');
    const addHint = el('[data-add-hint]');
    const addError = el('[data-add-error]');

    addUnit.innerHTML = config.units.map((unit) => `<option value="${escapeHtml(unit.value)}">${escapeHtml(unit.label)}</option>`).join('');
    addCategory.innerHTML = '<option value="">Bez kategorii</option>' + config.categoryGroups.map((group) => {
        const options = group.categories.map((category) => `<option value="${escapeHtml(category)}">${escapeHtml(category)}</option>`).join('');
        return group.label ? `<optgroup label="${escapeHtml(group.label)}">${options}</optgroup>` : options;
    }).join('');

    function syncAddUnitStep() {
        addQuantity.step = addUnit.value === 'szt' ? '1' : '0.01';
    }

    function openAddPanel() {
        addPanel.hidden = false;
        app.classList.add('is-adding');
        addError.hidden = true;
        // Fokus od razu, w obsłudze dotknięcia - iOS otwiera klawiaturę tylko wtedy.
        addName.focus();
    }

    function closeAddPanel() {
        addPanel.hidden = true;
        app.classList.remove('is-adding');
    }

    addQuantity.addEventListener('input', () => { addQuantity.dataset.touched = '1'; });
    addName.addEventListener('input', () => {
        const product = productByName(currentView(), addName.value);
        if (product) {
            addUnit.value = product.unit;
            addCategory.value = product.category || '';
            // Bez ręcznie wpisanej ilości podpowiadamy jedno opakowanie
            // (np. 200 g masła zamiast 1 g).
            if (!addQuantity.dataset.touched && product.package) {
                addQuantity.value = String(Number(product.package));
            }
            addHint.hidden = false;
            addHint.innerHTML = '<i class="bi bi-house" aria-hidden="true"></i> Produkt ze spiżarni - po odhaczeniu zapas uzupełni się sam.';
            syncAddUnitStep();
        } else {
            addHint.hidden = true;
        }
    });
    addUnit.addEventListener('change', syncAddUnitStep);

    addForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        const view = currentView();
        const list = selectedList(view);
        const name = addName.value.trim();
        const unit = addUnit.value;
        const quantity = normalizeQuantity(addQuantity.value || '1', unit);
        addError.hidden = true;
        if (!list) {
            addError.textContent = 'Nie ma aktywnej listy, do której można dodać produkt.';
            addError.hidden = false;
            return;
        }
        if (!name) {
            addError.textContent = 'Wpisz nazwę produktu.';
            addError.hidden = false;
            addName.focus();
            return;
        }
        if (!quantity) {
            addError.textContent = unit === 'szt' ? 'Dla sztuk podaj liczbę całkowitą większą od zera.' : 'Podaj ilość większą od zera.';
            addError.hidden = false;
            addQuantity.focus();
            return;
        }
        const operation = {
            type: OP_ADD,
            list: list.id,
            data: { uuid: newId(), name, quantity, unit, category: addCategory.value, note: '' },
        };
        // Formularz czyścimy przed zapisem, żeby następny produkt można było
        // wpisywać od razu, bez ryzyka, że reset skasuje już wpisany tekst.
        addForm.reset();
        delete addQuantity.dataset.touched;
        addUnit.value = config.units[0].value;
        addHint.hidden = true;
        syncAddUnitStep();
        addName.focus();
        await enqueue(operation);
        toast(`Dodano: ${name}`, 'success');
    });

    // ------------------------------------------------------------------
    // Zdarzenia
    // ------------------------------------------------------------------
    el('[data-list]').addEventListener('click', async (event) => {
        const button = event.target.closest('[data-action]');
        if (!button) {
            return;
        }
        const row = button.closest('[data-uuid]');
        const uuid = row && row.dataset.uuid;
        const view = currentView();
        const found = uuid && findItem(view, uuid);
        if (!found) {
            return;
        }
        const { item } = found;
        const action = button.dataset.action;
        if (action === 'toggle') {
            if (navigator.vibrate) {
                navigator.vibrate(12);
            }
            state.confirmDelete = null;
            await enqueue({ type: OP_SET_PURCHASED, item: uuid, purchased: !item.is_purchased });
        } else if (action === 'edit') {
            state.openItem = state.openItem === uuid ? null : uuid;
            state.confirmDelete = null;
            render();
        } else if (action === 'inc' || action === 'dec') {
            const step = Number(button.dataset.step);
            const current = Number(item.quantity);
            let next = action === 'inc' ? current + step : current - step;
            if (next <= 0) {
                next = item.unit === 'szt' || item.unit === 'opak' ? 1 : Math.max(0.01, current / 2);
            }
            const quantity = normalizeQuantity(next, item.unit);
            if (quantity && quantity !== Number(item.quantity).toFixed(2)) {
                await enqueue({ type: OP_SET_QUANTITY, item: uuid, quantity });
            }
        } else if (action === 'delete') {
            if (state.confirmDelete !== uuid) {
                state.confirmDelete = uuid;
                render();
                return;
            }
            state.confirmDelete = null;
            state.openItem = null;
            await enqueue({ type: OP_DELETE, item: uuid });
            toast(`Usunięto: ${item.name}`, 'info');
        }
    });

    el('[data-list]').addEventListener('change', async (event) => {
        const input = event.target.closest('[data-quantity-input]');
        if (!input) {
            return;
        }
        const uuid = input.closest('[data-uuid]').dataset.uuid;
        const found = findItem(currentView(), uuid);
        if (!found) {
            return;
        }
        const quantity = normalizeQuantity(input.value, found.item.unit);
        if (!quantity) {
            toast(found.item.unit === 'szt' ? 'Dla sztuk podaj liczbę całkowitą.' : 'Podaj ilość większą od zera.', 'warning');
            input.value = Number(found.item.quantity);
            return;
        }
        input.blur();
        if (quantity !== Number(found.item.quantity).toFixed(2)) {
            await enqueue({ type: OP_SET_QUANTITY, item: uuid, quantity });
        } else {
            render();
        }
    });

    el('[data-list]').addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && event.target.matches('[data-quantity-input]')) {
            event.target.blur();
        }
    });

    el('[data-list]').addEventListener('focusout', (event) => {
        if (event.target.matches('[data-quantity-input]')) {
            setTimeout(() => {
                if (state.renderDeferred) {
                    render();
                }
            }, 0);
        }
    });

    el('[data-list-select]').addEventListener('change', async (event) => {
        state.meta.selectedListId = Number(event.target.value);
        state.openItem = null;
        await saveMeta();
        render();
    });

    el('[data-screen-switch]').addEventListener('click', (event) => {
        const button = event.target.closest('[data-screen]');
        if (!button) {
            return;
        }
        state.screen = button.dataset.screen;
        state.openItem = null;
        state.showShop = false;
        render();
    });

    el('[data-list]').addEventListener('input', (event) => {
        if (event.target.matches('[data-pantry-search]')) {
            state.pantrySearch = event.target.value;
            const items = el('[data-list]').querySelector('.sa-items, .sa-empty');
            const view = currentView();
            const fresh = document.createElement('div');
            fresh.innerHTML = renderPantry(view, selectedList(view));
            const replacement = fresh.querySelector('.sa-items, .sa-empty');
            if (items && replacement) {
                items.replaceWith(replacement);
            }
        }
    });

    el('[data-list]').addEventListener('click', async (event) => {
        const button = event.target.closest('[data-pantry]');
        if (!button) {
            return;
        }
        const row = button.closest('[data-product]');
        const view = currentView();
        const list = selectedList(view);
        const product = (view.products || []).find((candidate) => candidate.id === Number(row.dataset.product));
        if (!product) {
            return;
        }
        const action = button.dataset.pantry;
        if (action === 'to-list') {
            if (!list) {
                toast('Nie ma aktywnej listy.', 'warning');
                return;
            }
            await enqueue({
                type: OP_ADD,
                list: list.id,
                data: {
                    uuid: newId(), name: product.name, quantity: product.package,
                    unit: product.unit, category: product.category, note: '',
                },
            });
            toast(`Dopisano do listy: ${product.name}`, 'success');
            return;
        }
        await enqueue({ type: OP_PANTRY_MOVEMENT, product: product.id, action, count: 1 });
    });

    el('[data-scanner]').addEventListener('click', (event) => {
        if (event.target.closest('[data-scan-close]')) {
            closeScanner();
        } else if (event.target.closest('[data-scan-again]')) {
            openScanner();
        } else {
            const action = event.target.closest('[data-scan-action]');
            if (action) {
                scannerAction(action.dataset.scanAction);
            }
        }
    });

    el('[data-scan-open]').addEventListener('click', openScanner);

    el('[data-shop-chip]').addEventListener('click', () => {
        state.showShop = !state.showShop;
        state.showDetails = false;
        render();
    });

    el('[data-shop]').addEventListener('click', async (event) => {
        if (event.target.closest('[data-shop-close]')) {
            state.showShop = false;
            render();
            return;
        }
        const pick = event.target.closest('[data-shop-pick]');
        if (pick) {
            const list = selectedList(currentView());
            if (list) {
                await enqueue({ type: OP_LIST_SHOP, list: list.id, shop: pick.value });
            }
            return;
        }
        const move = event.target.closest('[data-move]');
        if (move) {
            const view = currentView();
            const list = selectedList(view);
            const shop = currentShop(view, list);
            if (!shop) {
                return;
            }
            const categories = groupItems(list.items.filter((item) => !item.is_purchased), shop.order)
                .map((group) => group.category)
                .filter((category) => category !== 'Bez kategorii');
            const index = categories.indexOf(move.dataset.category);
            const target = move.dataset.move === 'up' ? index - 1 : index + 1;
            if (index === -1 || target < 0 || target >= categories.length) {
                return;
            }
            [categories[index], categories[target]] = [categories[target], categories[index]];
            await enqueue({ type: OP_SHOP_ORDER, shop: shop.uuid, order: categories });
        }
    });

    el('[data-shop]').addEventListener('submit', async (event) => {
        event.preventDefault();
        const input = el('[data-shop-name]');
        const name = input.value.trim();
        const view = currentView();
        const list = selectedList(view);
        if (!name || !list) {
            return;
        }
        if ((view.shops || []).some((shop) => shop.name.toLocaleLowerCase('pl') === name.toLocaleLowerCase('pl'))) {
            toast('Taki sklep już jest na liście.', 'warning');
            return;
        }
        const shopId = newId();
        input.value = '';
        await enqueue({ type: OP_SHOP_ADD, shop: shopId, name, order: [] });
        await enqueue({ type: OP_LIST_SHOP, list: list.id, shop: shopId });
        toast(`Dodano sklep: ${name}`, 'success');
    });

    el('[data-offline-chip]').addEventListener('click', () => {
        state.showShop = false;
        state.showDetails = !state.showDetails;
        if (state.showDetails) {
            checkOfflineReady();
        }
        render();
    });

    app.addEventListener('click', (event) => {
        if (event.target.closest('[data-details-close]')) {
            state.showDetails = false;
            render();
        } else if (event.target.closest('[data-prepare]')) {
            prepareOffline();
        } else if (event.target.closest('[data-badge]')) {
            askForBadge();
        } else if (event.target.closest('[data-push]')) {
            const button = event.target.closest('[data-push]');
            if (button.dataset.push === 'on') {
                enablePush();
            } else {
                disablePush();
            }
        }
    });

    el('[data-sync-button]').addEventListener('click', () => {
        if (state.status === 'auth') {
            window.location.href = config.loginUrl;
            return;
        }
        sync();
    });

    el('[data-add-open]').addEventListener('click', openAddPanel);
    el('[data-add-close]').addEventListener('click', closeAddPanel);
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && !addPanel.hidden) {
            closeAddPanel();
        }
    });

    el('[data-complete-button]').addEventListener('click', async () => {
        const list = selectedList(currentView());
        if (!list) {
            return;
        }
        if (!state.confirmComplete) {
            state.confirmComplete = true;
            render();
            setTimeout(() => {
                if (state.confirmComplete) {
                    state.confirmComplete = false;
                    render();
                }
            }, 5000);
            return;
        }
        await completeList(list);
    });

    // Linki poza tryb zakupów działają tylko z połączeniem - bez niego
    // przeglądarka pokazałaby własny ekran błędu.
    app.addEventListener('click', (event) => {
        const link = event.target.closest('[data-online-link]');
        if (link && state.status === 'offline') {
            event.preventDefault();
            toast('Bez połączenia z domowym serwerem - reszta strony jest niedostępna. Lista działa dalej.', 'warning');
        }
    });

    // ------------------------------------------------------------------
    // Instalacja na ekranie początkowym
    // ------------------------------------------------------------------
    const installHint = { visible: false, html: '', canPrompt: false, promptEvent: null };
    const standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;

    async function setupInstallHint() {
        if (standalone || (await store.get('installHintDismissed'))) {
            return;
        }
        const ua = navigator.userAgent;
        const isIos = /iPhone|iPad|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
        if (isIos) {
            installHint.visible = true;
            installHint.html = '<strong>Dodaj do ekranu początkowego</strong>, żeby lista zawsze była pod ręką: '
                + 'w Safari dotknij <i class="bi bi-box-arrow-up" aria-label="Udostępnij"></i> i wybierz „Do ekranu początkowego”. '
                + 'Potem <strong>uruchom aplikację z ikony jeszcze raz w domu</strong> - iPhone traktuje ją jak osobną '
                + 'aplikację z własną pamięcią i dopiero wtedy zapisze listę na wyjście do sklepu.';
            render();
        }
    }

    window.addEventListener('beforeinstallprompt', (event) => {
        event.preventDefault();
        installHint.promptEvent = event;
        installHint.canPrompt = true;
        installHint.visible = !standalone;
        installHint.html = '<strong>Zainstaluj listę zakupów</strong> - otworzysz ją z ekranu telefonu, także bez internetu.';
        render();
    });

    app.addEventListener('click', async (event) => {
        if (event.target.closest('[data-install]') && installHint.promptEvent) {
            installHint.promptEvent.prompt();
            await installHint.promptEvent.userChoice.catch(() => null);
            installHint.promptEvent = null;
            installHint.visible = false;
            render();
        } else if (event.target.closest('[data-install-dismiss]')) {
            installHint.visible = false;
            await store.set('installHintDismissed', true).catch(() => null);
            render();
        }
    });

    // ------------------------------------------------------------------
    // Service worker, aktualizacje i gotowość na wyjście z domu
    // ------------------------------------------------------------------
    let registration = null;
    function checkForUpdate() {
        if (registration) {
            registration.update().catch(() => null);
        }
    }

    async function readCacheState() {
        const result = { shell: false, files: 0, total: 0 };
        if (!('caches' in window)) {
            return result;
        }
        try {
            const names = (await caches.keys()).filter((name) => name.startsWith('zakupy-'));
            for (const name of names) {
                const cache = await caches.open(name);
                const keys = await cache.keys();
                result.files = Math.max(result.files, keys.length);
                if (keys.some((request) => new URL(request.url).pathname === config.scope)) {
                    result.shell = true;
                }
            }
        } catch (error) {
            result.error = String(error && error.message);
        }
        return result;
    }

    async function checkOfflineReady() {
        const cacheState = await readCacheState();
        let sw = 'brak obsługi';
        if ('serviceWorker' in navigator) {
            if (state.offline.error) {
                sw = 'błąd';
            } else if (navigator.serviceWorker.controller) {
                sw = 'działa';
            } else if (registration && (registration.installing || registration.waiting)) {
                sw = 'instaluję';
            } else {
                sw = 'nieaktywny';
            }
        }
        state.offline = {
            ...state.offline,
            checked: true,
            shell: cacheState.shell,
            files: cacheState.files,
            total: config.assetCount || cacheState.files,
            sw,
        };
        render();
    }

    function offlineReady() {
        return state.offline.checked && state.offline.shell && Boolean(state.snapshot);
    }

    async function prepareOffline() {
        if (!window.isSecureContext) {
            toast('Najpierw otwórz aplikację przez HTTPS - bez tego telefon nie zapisze jej na wyjście z domu.', 'warning');
            return;
        }
        state.preparing = true;
        render();
        try {
            if ('serviceWorker' in navigator) {
                const existing = await navigator.serviceWorker.getRegistration(config.swScope || config.scope);
                if (existing) {
                    await existing.update().catch(() => null);
                } else {
                    registration = await navigator.serviceWorker.register(config.serviceWorkerUrl, { scope: config.swScope || config.scope });
                }
                await navigator.serviceWorker.ready;
            }
            await sync();
            await checkOfflineReady();
            toast(offlineReady()
                ? 'Gotowe. Lista i aplikacja są zapisane w telefonie.'
                : 'Nie udało się jeszcze zapisać wszystkiego. Spróbuj odświeżyć stronę w domowej sieci.',
            offlineReady() ? 'success' : 'warning');
        } catch (error) {
            state.offline.error = String(error && error.message);
            toast('Nie udało się przygotować trybu offline: ' + (error && error.message), 'warning');
        } finally {
            state.preparing = false;
            render();
        }
    }

    function setupServiceWorker() {
        if (!window.isSecureContext || !('serviceWorker' in navigator)) {
            state.offline.error = window.isSecureContext
                ? 'Ta przeglądarka nie obsługuje trybu offline.'
                : 'Ten adres nie jest bezpiecznym połączeniem (http), więc telefon nie pozwala zapisać aplikacji.';
            checkOfflineReady();
            return;
        }
        const hadController = Boolean(navigator.serviceWorker.controller);
        let reloading = false;
        navigator.serviceWorker.addEventListener('controllerchange', () => {
            // Nowa wersja aplikacji przejęła stronę. Dane i kolejka są w
            // IndexedDB, więc przeładowanie niczego nie gubi.
            if (hadController && !reloading) {
                reloading = true;
                window.location.reload();
            }
        });
        navigator.serviceWorker.register(config.serviceWorkerUrl, { scope: config.swScope || config.scope })
            .then((reg) => {
                registration = reg;
                return navigator.serviceWorker.ready;
            })
            .then(() => checkOfflineReady())
            .catch((error) => {
                // Bez service workera aplikacja nie otworzy się poza domem -
                // pokazujemy to wprost, zamiast udawać, że wszystko gra.
                state.offline.error = String(error && error.message);
                checkOfflineReady();
            });
    }

    // ------------------------------------------------------------------
    // Start
    // ------------------------------------------------------------------
    async function start() {
        setupServiceWorker();
        if (navigator.storage && navigator.storage.persist) {
            navigator.storage.persist().catch(() => null);
        }
        try {
            state.storageOk = await store.persistent();
            state.snapshot = (await store.get('snapshot')) || null;
            state.meta = { ...state.meta, ...((await store.get('meta')) || {}) };
            state.outbox = ((await store.outboxAll()) || []).sort((a, b) => a.seq - b.seq);
        } catch (error) {
            state.storageOk = false;
        }
        render();
        await setupInstallHint();
        refreshPushState();
        sync();

        setInterval(() => {
            if (document.visibilityState === 'visible') {
                sync();
            }
        }, SYNC_INTERVAL_MS);
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                sync();
            }
        });
        window.addEventListener('online', () => sync());
        window.addEventListener('focus', () => scheduleSync(200));
        window.addEventListener('pageshow', (event) => {
            if (event.persisted) {
                sync();
            }
        });
    }

    // Pozwala testom sprawdzić stan bez zaglądania do IndexedDB.
    window.__shoppingApp = { state, sync, currentView };
    start();
})();
