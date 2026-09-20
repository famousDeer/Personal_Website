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
                    return memory.kv.get(key);
                }
                return tx(db, 'kv', 'readonly', (os) => requestValue(os.get(key)));
            },
            async set(key, value) {
                const db = await open();
                if (!db) {
                    memory.kv.set(key, value);
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
    };

    const newId = () => (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
            const r = crypto.getRandomValues(new Uint8Array(1))[0] % 16;
            return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
        });

    function currentView() {
        const view = state.snapshot
            ? JSON.parse(JSON.stringify(state.snapshot))
            : { lists: [], products: [] };
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
        if (op.type === OP_SET_PURCHASED || op.type === OP_SET_QUANTITY) {
            const superseded = state.outbox.filter((entry) => entry.op.type === op.type && entry.op.item === op.item);
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

    function groupItems(items) {
        const groups = new Map();
        for (const item of items) {
            const key = item.category || '';
            if (!groups.has(key)) {
                groups.set(key, []);
            }
            groups.get(key).push(item);
        }
        const rank = (category) => {
            if (!category) {
                return 10000;
            }
            const index = categoryOrder.indexOf(category);
            return index === -1 ? 5000 : index;
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

    function renderBanners(view) {
        const banners = [];
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

    function render() {
        const active = document.activeElement;
        if (active && active.matches && active.matches('[data-quantity-input]') && app.contains(active)) {
            // Nie przerysowujemy listy pod palcem, gdy ktoś wpisuje ilość.
            state.renderDeferred = true;
            renderStatus();
            return;
        }
        state.renderDeferred = false;
        const view = currentView();
        const list = selectedList(view);
        renderStatus();
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
            container.innerHTML = state.snapshot ? `
                <div class="sa-empty">
                    <i class="bi bi-cart-check" aria-hidden="true"></i>
                    <h2>Nie ma aktywnej listy</h2>
                    <p>Utwórz listę w domu: <strong>Kuchnia → Lista zakupów</strong>. Pojawi się tu przy następnej synchronizacji.</p>
                </div>` : (state.status === 'offline' ? '' : '<div class="sa-empty"><div class="sa-spinner" aria-hidden="true"></div><p>Pobieram listę…</p></div>');
            return;
        }

        title.textContent = list.title;
        footer.hidden = false;
        const total = list.items.length;
        const done = list.items.filter((item) => item.is_purchased).length;
        progress.hidden = total === 0;
        el('[data-progress-bar]').style.width = total ? `${Math.round((done / total) * 100)}%` : '0';
        el('[data-progress-text]').textContent = total ? `${done} z ${total} w koszyku` : '';

        const toBuy = list.items.filter((item) => !item.is_purchased);
        const bought = list.items.filter((item) => item.is_purchased);
        const sections = groupItems(toBuy).map((group) => `
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

        const completeButton = el('[data-complete-button]');
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
    // Komunikaty
    // ------------------------------------------------------------------
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
                + 'Tylko wtedy iPhone trwale przechowuje listę offline.';
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
    // Service worker i aktualizacje
    // ------------------------------------------------------------------
    let registration = null;
    function checkForUpdate() {
        if (registration) {
            registration.update().catch(() => null);
        }
    }

    function setupServiceWorker() {
        if (!('serviceWorker' in navigator)) {
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
        navigator.serviceWorker.register(config.serviceWorkerUrl, { scope: config.scope })
            .then((reg) => { registration = reg; })
            .catch(() => null);
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
