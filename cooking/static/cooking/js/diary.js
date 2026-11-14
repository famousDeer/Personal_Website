/* Dziennik kalorii: wyszukiwarka produktów na żywo i podgląd kcal.
 *
 * Pole „Co zjadłeś?” pyta serwer (data-search-url) o spiżarnię, przepisy
 * i bazę produktów. Wybrana pozycja trafia do ukrytych pól food/product/
 * recipe, lista miar zawęża się do tych, które produkt zna, a obok „Dodaj”
 * widać, ile to kcal. Zmiana tekstu po wyborze czyści wybór - wtedy serwer
 * szuka po samej nazwie (tak działa też formularz bez JS).
 *
 * Produkty z etykietą: wiersz „Szukaj w Open Food Facts” dociąga wyniki
 * po nazwie, a przycisk skanera czyta kod aparatem (barcode-camera.js).
 * W obu przypadkach produkt pobiera serwer po kodzie (data-barcode-url)
 * i zapisuje go w bazie produktów.
 */
(function () {
    'use strict';

    const form = document.querySelector('[data-diary-add]');
    if (!form) { return; }
    const input = form.querySelector('[data-diary-search]');
    const list = form.querySelector('[data-diary-results]');
    const preview = form.querySelector('[data-diary-preview]');
    const quantity = form.querySelector('[name="quantity"]');
    const unit = form.querySelector('[name="unit"]');
    const manualKcal = form.querySelector('[name="kcal"]');
    const picks = form.querySelectorAll('[data-diary-pick]');
    const allUnits = unit.innerHTML;
    const status = form.querySelector('[data-diary-status]');
    const scanButton = form.querySelector('[data-diary-scan]');
    const cameraBox = form.querySelector('[data-diary-camera]');
    const foodAddUrl = (name, barcode) => `${form.dataset.foodAddUrl}?name=${encodeURIComponent(name)}`
        + (barcode ? `&barcode=${encodeURIComponent(barcode)}` : '')
        + `&next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
    let picked = null;
    let timer = null;
    let requestNumber = 0;

    function number(value) {
        const parsed = parseFloat(String(value).replace(/\s/g, '').replace(',', '.'));
        return Number.isFinite(parsed) ? parsed : null;
    }

    function setPick(result) {
        picked = result;
        picks.forEach((field) => {
            field.value = result && field.dataset.diaryPick === result.kind ? result.id : '';
        });
    }

    function hideList() {
        list.hidden = true;
        list.replaceChildren();
    }

    function updatePreview() {
        const amount = number(quantity.value);
        const option = picked && picked.units.find((item) => item.value === unit.value);
        if (!picked || !option || amount === null || number(manualKcal.value) !== null) {
            preview.textContent = '';
            return;
        }
        const factor = amount * option.grams / 100;
        const round = (value) => Math.round(value * factor);
        preview.textContent = `≈ ${round(picked.per_100.kcal)} kcal · B ${round(picked.per_100.protein)} · `
            + `T ${round(picked.per_100.fat)} · W ${round(picked.per_100.carbs)} g`;
    }

    // Komunikat pod polem szukania; z linkiem „dodaj własny produkt”, gdy podano nazwę albo kod.
    function showStatus(text, add) {
        status.replaceChildren(text ? document.createTextNode(`${text} `) : '');
        if (add) {
            const link = document.createElement('a');
            link.href = foodAddUrl(add.name || '', add.barcode);
            link.textContent = 'Dodaj własny produkt';
            status.append(link);
        }
        status.hidden = !text;
    }

    // Produkt po kodzie kreskowym: ze skanera albo z listy Open Food Facts.
    function loadBarcode(barcode) {
        showStatus('Szukam produktu…');
        return fetch(`${form.dataset.barcodeUrl}?barcode=${encodeURIComponent(barcode)}`, { headers: { Accept: 'application/json' } })
            .then((response) => response.json())
            .then((data) => {
                if (data.found) {
                    choose(data.result);
                } else {
                    showStatus(data.message || 'Nie znam tego produktu.', { name: data.name, barcode: data.barcode });
                }
            })
            .catch(() => showStatus('Nie udało się sprawdzić kodu. Spróbuj ponownie.'));
    }

    function choose(result) {
        showStatus('');
        setPick(result);
        input.value = result.name;
        unit.replaceChildren(...result.units.map((item) => new Option(item.label, item.value)));
        quantity.value = result.quantity;
        hideList();
        updatePreview();
        quantity.focus();
        quantity.select();
    }

    function clearPick() {
        if (!picked) { return; }
        setPick(null);
        unit.innerHTML = allUnits;
        updatePreview();
    }

    function row(title, note) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'list-group-item list-group-item-action d-flex justify-content-between gap-3';
        const name = document.createElement('span');
        name.textContent = title;
        const side = document.createElement('small');
        side.className = 'text-muted text-nowrap';
        side.textContent = note;
        button.append(name, side);
        return button;
    }

    function render(results, query) {
        list.replaceChildren();
        results.forEach((result) => {
            const button = row(result.name, result.note);
            button.addEventListener('click', () => choose(result));
            list.append(button);
        });
        // Nie ma na liście: własny produkt do wspólnej bazy.
        if (form.dataset.offSearchUrl) {
            const more = row(`Szukaj „${query}” w Open Food Facts`, 'produkty z etykietą');
            more.classList.add('text-primary');
            more.dataset.skipEnter = '1';
            more.addEventListener('click', () => searchOff(query, more));
            list.append(more);
        }
        const add = document.createElement('a');
        add.className = 'list-group-item list-group-item-action text-primary';
        add.href = foodAddUrl(query);
        add.textContent = results.length ? `Nie ma na liście? Dodaj własny produkt „${query}”` : `Brak wyników. Dodaj własny produkt „${query}”`;
        list.append(add);
        list.hidden = false;
    }

    // Wyniki z Open Food Facts wchodzą w miejsce wiersza „Szukaj w…”.
    function searchOff(query, button) {
        button.disabled = true;
        button.firstChild.textContent = 'Szukam w Open Food Facts…';
        fetch(`${form.dataset.offSearchUrl}?q=${encodeURIComponent(query)}`, { headers: { Accept: 'application/json' } })
            .then((response) => response.json())
            .then((data) => {
                const rows = data.results.map((item) => {
                    const found = row(item.name, item.note);
                    found.addEventListener('click', () => { hideList(); loadBarcode(item.barcode); });
                    return found;
                });
                if (!rows.length) {
                    const empty = document.createElement('div');
                    empty.className = 'list-group-item text-muted';
                    empty.textContent = data.error || 'Open Food Facts nie ma takiego produktu z kaloriami.';
                    rows.push(empty);
                }
                button.replaceWith(...rows);
            })
            .catch(() => { button.disabled = false; button.firstChild.textContent = 'Nie udało się. Spróbuj ponownie'; });
    }

    function search() {
        const query = input.value.trim();
        if (query.length < 2) { hideList(); return; }
        const current = ++requestNumber;
        fetch(`${form.dataset.searchUrl}?q=${encodeURIComponent(query)}`, { headers: { Accept: 'application/json' } })
            .then((response) => (response.ok ? response.json() : { results: [] }))
            .then((data) => {
                // Odpowiedź na starsze zapytanie nie nadpisuje nowszej.
                if (current === requestNumber && input.value.trim() === query) { render(data.results, query); }
            })
            .catch(hideList);
    }

    input.addEventListener('input', () => {
        showStatus('');
        clearPick();
        clearTimeout(timer);
        timer = setTimeout(search, 200);
    });
    input.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') { hideList(); }
        // Enter przy otwartej liście wybiera pierwszy wynik zamiast wysyłać formularz.
        if (event.key === 'Enter' && !list.hidden) {
            const first = list.querySelector('button:not([data-skip-enter])');
            if (first) { event.preventDefault(); first.click(); }
        }
    });
    document.addEventListener('click', (event) => {
        if (!form.contains(event.target)) { hideList(); }
    });
    // Skaner kodów: przycisk pokazuje się tylko tam, gdzie jest aparat.
    if (scanButton && window.BarcodeCamera && navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        const camera = window.BarcodeCamera.create({
            video: cameraBox.querySelector('[data-diary-video]'),
            frame: cameraBox.querySelector('.rb-frame'),
            zxingUrl: form.dataset.zxingUrl,
            onCode: (barcode) => { stopCamera(); return loadBarcode(barcode); },
        });
        function stopCamera() {
            camera.stop();
            cameraBox.hidden = true;
            scanButton.classList.remove('active');
        }
        scanButton.hidden = false;
        scanButton.addEventListener('click', () => {
            if (camera.active || !cameraBox.hidden) { stopCamera(); return; }
            hideList();
            cameraBox.hidden = false;
            scanButton.classList.add('active');
            showStatus('Skieruj aparat na kod kreskowy.');
            camera.start().catch((error) => {
                stopCamera();
                showStatus(error.message || 'Nie udało się uruchomić aparatu.');
            });
        });
        window.addEventListener('pagehide', () => camera.stop());
    }

    [quantity, unit, manualKcal].forEach((field) => {
        field.addEventListener('input', updatePreview);
        field.addEventListener('change', updatePreview);
    });
}());
