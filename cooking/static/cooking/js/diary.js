/* Dziennik kalorii: wyszukiwarka produktów na żywo i podgląd kcal.
 *
 * Pole „Co zjadłeś?” pyta serwer (data-search-url) o spiżarnię, przepisy
 * i bazę produktów. Wybrana pozycja trafia do ukrytych pól food/product/
 * recipe, lista miar zawęża się do tych, które produkt zna, a obok „Dodaj”
 * widać, ile to kcal. Zmiana tekstu po wyborze czyści wybór - wtedy serwer
 * szuka po samej nazwie (tak działa też formularz bez JS).
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

    function choose(result) {
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
        const add = document.createElement('a');
        add.className = 'list-group-item list-group-item-action text-primary';
        add.href = `${form.dataset.foodAddUrl}?name=${encodeURIComponent(query)}`
            + `&next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
        add.textContent = results.length ? `Nie ma na liście? Dodaj własny produkt „${query}”` : `Brak wyników. Dodaj własny produkt „${query}”`;
        list.append(add);
        list.hidden = false;
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
        clearPick();
        clearTimeout(timer);
        timer = setTimeout(search, 200);
    });
    input.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') { hideList(); }
        // Enter przy otwartej liście wybiera pierwszy wynik zamiast wysyłać formularz.
        if (event.key === 'Enter' && !list.hidden) {
            const first = list.querySelector('button');
            if (first) { event.preventDefault(); first.click(); }
        }
    });
    document.addEventListener('click', (event) => {
        if (!form.contains(event.target)) { hideList(); }
    });
    [quantity, unit, manualKcal].forEach((field) => {
        field.addEventListener('input', updatePreview);
        field.addEventListener('change', updatePreview);
    });
}());
