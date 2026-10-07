/*
 * Wybór sugestii ze spiżarni (strona "Listy zakupów").
 *
 * Bez skryptu formularz też działa: zaznaczone pola idą do serwera, który
 * pomija to, co już jest na wybranej liście. Skrypt dodaje tylko wygodę:
 * licznik na przycisku, szybkie zaznaczanie, przełączanie celu i wyszarzenie
 * produktów, które na wybranej liście już są.
 */
(() => {
    const form = document.querySelector('[data-suggestion-picker]');
    if (!form) {
        return;
    }
    const rows = [...form.querySelectorAll('[data-suggestion-row]')];
    const submit = form.querySelector('[data-suggestion-submit]');
    const label = form.querySelector('[data-suggestion-submit-label]');
    const titleInput = form.querySelector('[data-target-new]');
    const listSelect = form.querySelector('[data-target-list]');

    const pickOf = (row) => row.querySelector('[data-suggestion-pick]');
    const target = () => {
        const choice = form.querySelector('[data-target-choice]:checked');
        return choice && choice.value === 'existing' && listSelect ? listSelect.value : 'new';
    };
    const onTarget = (row) => {
        const lists = (row.dataset.onLists || '').split(' ').filter(Boolean);
        const current = target();
        return current !== 'new' && lists.includes(current);
    };

    function plural(count, one, few, many) {
        if (count === 1) {
            return one;
        }
        const tens = count % 100;
        return count % 10 >= 2 && count % 10 <= 4 && (tens < 12 || tens > 14) ? few : many;
    }

    function update() {
        const isNew = target() === 'new';
        if (titleInput) {
            titleInput.hidden = !isNew;
        }
        if (listSelect) {
            listSelect.hidden = isNew;
        }
        let count = 0;
        rows.forEach((row) => {
            const present = onTarget(row);
            const pick = pickOf(row);
            row.classList.toggle('is-on-list', present);
            row.querySelector('[data-on-list-note]').hidden = !present;
            pick.disabled = present;
            row.querySelector('[data-suggestion-qty]').disabled = present || !pick.checked;
            row.classList.toggle('is-picked', pick.checked && !present);
            if (pick.checked && !present) {
                count += 1;
            }
        });
        submit.disabled = count === 0;
        const what = `${count} ${plural(count, 'pozycję', 'pozycje', 'pozycji')}`;
        label.textContent = count === 0
            ? 'Zaznacz produkty'
            : (isNew ? `Utwórz listę (${count})` : `Dodaj ${what}`);
    }

    form.addEventListener('change', update);
    form.addEventListener('click', (event) => {
        const button = event.target.closest('[data-pick]');
        if (!button) {
            return;
        }
        const mode = button.dataset.pick;
        rows.forEach((row) => {
            const pick = pickOf(row);
            if (!pick.disabled) {
                pick.checked = mode === 'all' || (mode === 'empty' && row.dataset.status === 'empty');
            }
        });
        update();
    });
    // Produkt, który już jest na wybranej liście, ma wyłączone pole, więc nie
    // trafia do formularza; po zmianie celu wraca z wcześniejszym zaznaczeniem.
    update();
})();
