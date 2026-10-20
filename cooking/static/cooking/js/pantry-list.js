/* Lista spiżarni: wyszukiwanie na żywo, filtry, sortowanie, grupowanie,
 * układ Lista/Kafelki i panel szczegółów - bez przeładowania strony.
 *
 * Wiersze (pantry_row.html) niosą dane w atrybutach data-*: flagi stanu,
 * kategorię, tekst do wyszukiwania i klucze sortowania. Kafelek #product-N
 * jest treścią panelu szczegółów: przy otwarciu przenosimy go do panelu,
 * przy zamknięciu - z powrotem. Formularze kafelka i wiersza wysyłają się
 * w tle (app-actions.js, data-partial); po podmianie fragmentów filtry
 * nakładamy jeszcze raz (zdarzenie partial:swapped).
 *
 * Bez JavaScriptu pasek filtrów to zwykły formularz GET (q, status, category).
 */
(function () {
    'use strict';

    const list = document.querySelector('[data-pantry-list]');
    const toolbar = document.querySelector('[data-pantry-toolbar]');
    if (!list || !toolbar) {
        return;
    }

    const STORAGE_KEY = 'spizarnia-lista';
    const SERVER_STATUS = { low: 'buy', jednorazowe: 'oneoff' };
    const select = (selector, scope = document) => scope.querySelector(selector);
    const selectAll = (selector, scope = document) => [...scope.querySelectorAll(selector)];

    const searchInput = select('[data-filter-search]', toolbar);
    const sortSelect = select('[data-filter-sort]', toolbar);
    const groupedBox = select('[data-filter-grouped]', toolbar);
    const resultLine = select('[data-filter-result]', toolbar);
    const flatSection = select('[data-flat-section]', list);
    const flatRows = select('[data-flat-rows]', list);
    const flatTiles = select('[data-flat-tiles]', list);
    const emptyState = select('[data-filter-empty]', list);
    const detail = document.querySelector('[data-pantry-detail]');
    const detailBody = detail ? select('[data-pantry-detail-body]', detail) : null;
    const detailTitle = detail ? select('#pantry-detail-title', detail) : null;

    function loadSaved() {
        try {
            return JSON.parse(window.localStorage.getItem(STORAGE_KEY) || '{}') || {};
        } catch (error) {
            return {};
        }
    }

    function save() {
        try {
            window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
                status: state.status, categories: [...state.categories], sort: state.sort,
                layout: state.layout, grouped: state.grouped,
            }));
        } catch (error) {
            // Tryb prywatny - ustawienia po prostu się nie zapamiętają.
        }
    }

    const saved = loadSaved();
    const params = new URLSearchParams(window.location.search);
    const serverStatus = params.get('status');
    const serverCategory = params.get('category');
    const state = {
        q: (searchInput?.value || '').trim().toLocaleLowerCase('pl'),
        status: serverStatus !== null ? (SERVER_STATUS[serverStatus] ?? serverStatus) : (saved.status || ''),
        categories: new Set(serverCategory ? [serverCategory] : (saved.categories || [])),
        sort: saved.sort || 'category',
        layout: saved.layout || 'list',
        grouped: saved.grouped !== false,
    };

    // Kolejność z serwera (kategorie w ustalonym porządku) - dla sortowania „kategorie”.
    const homeIndex = new Map();
    const homeSection = new Map();
    selectAll('[data-pantry-row]', list).forEach((row, index) => {
        homeIndex.set(row.dataset.pantryRow, index);
        homeSection.set(row.dataset.pantryRow, row.closest('[data-section]')?.dataset.section || '');
    });
    const total = homeIndex.size;

    const cardOf = (id) => document.getElementById(`product-${id}`);
    const sectionOf = (key) => select(`[data-section="${CSS.escape(key)}"]`, list);

    function plural(count, one, few, many) {
        if (count === 1) {
            return one;
        }
        const tens = count % 100;
        return count % 10 >= 2 && count % 10 <= 4 && (tens < 12 || tens > 14) ? few : many;
    }

    function matches(row) {
        const flags = (row.dataset.flags || '').split(' ');
        if (state.status && !flags.includes(state.status)) {
            return false;
        }
        if (state.categories.size && !state.categories.has(row.dataset.category)) {
            return false;
        }
        if (state.q) {
            const words = state.q.split(/\s+/).filter(Boolean);
            const haystack = `${row.dataset.search || ''} ${row.dataset.name || ''}`;
            return words.every((word) => haystack.includes(word));
        }
        return true;
    }

    const SORTS = {
        category: (row) => homeIndex.get(row.dataset.pantryRow) ?? 0,
        name: (row) => row.dataset.name || '',
        stock: (row) => Number(row.dataset.daysLeft || 9999),
        expires: (row) => row.dataset.expires || '9999',
        buy: (row) => row.dataset.buy || '9999',
        last: (row) => row.dataset.last || '0000',
        usage: (row) => Number(row.dataset.usage || 0),
        price: (row) => Number(row.dataset.priceChange || 0),
    };
    const DESCENDING = new Set(['last', 'usage', 'price']);

    function compare(a, b) {
        const key = SORTS[state.sort] || SORTS.category;
        const left = key(a);
        const right = key(b);
        let result;
        if (typeof left === 'number') {
            result = left - right;
        } else {
            result = String(left).localeCompare(String(right), 'pl');
        }
        if (DESCENDING.has(state.sort)) {
            result = -result;
        }
        return result || SORTS.category(a) - SORTS.category(b);
    }

    function placeRows() {
        const rows = selectAll('[data-pantry-row]', list);
        const inDetail = new Set(openCardId ? [`product-${openCardId}`] : []);
        rows.sort(compare);
        rows.forEach((row) => {
            const id = row.dataset.pantryRow;
            const section = sectionOf(homeSection.get(id));
            const rowsHome = state.grouped ? select('[data-section-rows]', section) : flatRows;
            const tilesHome = state.grouped ? select('[data-section-tiles]', section) : flatTiles;
            rowsHome.appendChild(row);
            const card = cardOf(id);
            if (card && !inDetail.has(card.id)) {
                tilesHome.appendChild(card);
            }
        });
    }

    function apply() {
        let visible = 0;
        placeRows();
        selectAll('[data-pantry-row]', list).forEach((row) => {
            const show = matches(row);
            row.hidden = !show;
            const card = cardOf(row.dataset.pantryRow);
            if (card && card.id !== `product-${openCardId}`) {
                card.hidden = !show;
            }
            if (show) {
                visible += 1;
            }
        });
        selectAll('[data-section]', list).forEach((section) => {
            const count = selectAll('[data-pantry-row]', section).filter((row) => !row.hidden).length;
            const counter = select('[data-section-count]', section);
            if (counter) {
                counter.textContent = String(count);
            }
            section.hidden = !state.grouped || count === 0;
        });
        flatSection.hidden = state.grouped || visible === 0;
        emptyState.hidden = visible > 0;
        list.dataset.layout = state.layout;

        const filtered = state.q || state.status || state.categories.size;
        resultLine.textContent = filtered
            ? `${visible} z ${total} ${plural(total, 'produktu', 'produktów', 'produktów')}`
            : `${total} ${plural(total, 'produkt', 'produkty', 'produktów')}`;
        selectAll('[data-filter-clear]').forEach((button) => {
            button.hidden = !filtered;
        });
        selectAll('[data-filter-status]', toolbar).forEach((chip) => {
            const active = chip.dataset.filterStatus === state.status;
            chip.classList.toggle('is-active', active);
            chip.setAttribute('aria-pressed', String(active));
        });
        selectAll('[data-filter-category]', toolbar).forEach((chip) => {
            const active = state.categories.has(chip.dataset.filterCategory);
            chip.classList.toggle('is-active', active);
            chip.setAttribute('aria-pressed', String(active));
        });
        selectAll('[data-preset]').forEach((item) => {
            item.classList.toggle('is-active', item.dataset.preset === state.status);
        });
        selectAll('[data-filter-layout]', toolbar).forEach((button) => {
            const active = button.dataset.filterLayout === state.layout;
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-pressed', String(active));
        });
        if (sortSelect.value !== state.sort) {
            sortSelect.value = state.sort;
        }
        groupedBox.checked = state.grouped;
        save();
    }

    // --- zdarzenia paska filtrów ------------------------------------------
    toolbar.addEventListener('submit', (event) => event.preventDefault());
    let searchTimer = null;
    searchInput?.addEventListener('input', () => {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(() => {
            state.q = searchInput.value.trim().toLocaleLowerCase('pl');
            apply();
        }, 120);
    });
    toolbar.addEventListener('click', (event) => {
        const statusChip = event.target.closest('[data-filter-status]');
        const categoryChip = event.target.closest('[data-filter-category]');
        const layoutButton = event.target.closest('[data-filter-layout]');
        if (statusChip) {
            event.preventDefault();
            const value = statusChip.dataset.filterStatus;
            state.status = state.status === value ? '' : value;
            apply();
        } else if (categoryChip) {
            event.preventDefault();
            const value = categoryChip.dataset.filterCategory;
            if (state.categories.has(value)) {
                state.categories.delete(value);
            } else {
                state.categories.add(value);
            }
            apply();
        } else if (layoutButton) {
            state.layout = layoutButton.dataset.filterLayout;
            apply();
        }
    });
    sortSelect.addEventListener('change', () => {
        state.sort = sortSelect.value;
        apply();
    });
    groupedBox.addEventListener('change', () => {
        state.grouped = groupedBox.checked;
        apply();
    });
    document.addEventListener('click', (event) => {
        const clear = event.target.closest('[data-filter-clear]');
        if (clear) {
            event.preventDefault();
            state.q = '';
            state.status = '';
            state.categories.clear();
            if (searchInput) {
                searchInput.value = '';
            }
            apply();
            return;
        }
        const preset = event.target.closest('[data-preset]');
        if (preset) {
            event.preventDefault();
            state.status = state.status === preset.dataset.preset ? '' : preset.dataset.preset;
            apply();
            toolbar.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
    });

    // Zmiana daty w wierszu („Ważne do…”) zapisuje się od razu.
    document.addEventListener('change', (event) => {
        const field = event.target.closest('[data-autosubmit]');
        if (field && field.value && field.form) {
            field.form.requestSubmit();
        }
    });

    // --- panel szczegółów ---------------------------------------------------
    let openCardId = null;

    function returnCard() {
        if (!openCardId) {
            return;
        }
        openCardId = null;
        apply();
    }

    function openDetail(id) {
        const card = cardOf(id);
        if (!card || !detail || !window.bootstrap?.Offcanvas) {
            return false;
        }
        returnCard();
        const wide = window.matchMedia('(min-width: 768px)').matches;
        detail.classList.toggle('offcanvas-bottom', !wide);
        detail.classList.toggle('offcanvas-end', wide);
        card.hidden = false;
        detailBody.replaceChildren(card);
        openCardId = id;
        const row = select(`[data-pantry-row="${CSS.escape(id)}"] .pt-name`, list);
        detailTitle.textContent = row ? row.textContent.trim() : 'Szczegóły';
        window.bootstrap.Offcanvas.getOrCreateInstance(detail).show();
        return true;
    }

    list.addEventListener('click', (event) => {
        const opener = event.target.closest('[data-open-detail]');
        if (opener && list.dataset.layout === 'list' && openDetail(opener.dataset.openDetail)) {
            event.preventDefault();
        }
    });
    detail?.addEventListener('hidden.bs.offcanvas', returnCard);

    document.addEventListener('partial:swapped', () => apply());

    apply();
})();
