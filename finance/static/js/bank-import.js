/* =========================================================================
   Podgląd importu wyciągu (finance/templates/finance/import_bank_transactions.html)
   =========================================================================
   - Pozycja to zwięzły wiersz (<details>): po dotknięciu rozwija się edycja,
     a wiersz na bieżąco pokazuje zmieniony opis, kategorię i kwotę.
   - Filtry: Wszystkie / Do sprawdzenia („Inne”, możliwe duplikaty) /
     Wydatki / Przychody / Duplikaty, z licznikami. „Zaznacz/Odznacz
     widoczne” działa na tym, co pokazuje filtr.
   - Przypięty pasek: liczba i suma zaznaczonych.
   - Zmiana kategorii jednej pozycji proponuje tę samą zmianę dla pozostałych
     pozycji z tego samego sklepu.
   - Błąd w zwiniętej pozycji (np. pusta kwota) ją rozwija.
   - „Zapamiętaj: sklep → kategoria” pokazuje bieżącą kategorię; regułę
     zapisuje serwer przy imporcie (finance.import_rules.remember_rules).
   Bez JavaScriptu wszystko się wysyła - edycję otwiera kliknięcie wiersza.
   ========================================================================= */
(function () {
    'use strict';

    const form = document.querySelector('[data-bip-form]');
    if (!form) {
        return;
    }

    const rows = Array.from(form.querySelectorAll('[data-bip-row]'));
    const empty = form.querySelector('[data-bip-empty]');
    const total = form.querySelector('[data-bip-total]');
    const submit = form.querySelector('[data-bip-submit]');
    const submitLabel = form.querySelector('[data-bip-submit-label]');
    const investmentNode = document.getElementById('bank-import-investment-category');
    const investment = investmentNode ? JSON.parse(investmentNode.textContent) : '';
    let filter = 'all';

    const field = (row, name) => row.querySelector(`[data-bip-${name}]`);
    const fold = (value) => String(value || '').trim().toLocaleLowerCase('pl-PL');
    // Grupowanie także dla 4 cyfr (1 234,00) - tak jak liczby z serwera w całej aplikacji.
    const money = (value) => value.toLocaleString('pl-PL', { minimumFractionDigits: 2, maximumFractionDigits: 2, useGrouping: 'always' });
    const amountOf = (row) => Number.parseFloat(String(field(row, 'amount').value || '0').replace(',', '.')) || 0;
    const labelOf = (row) => field(row, 'category').value.trim();
    const storeOf = (row) => (field(row, 'store') ? field(row, 'store').value.trim() : '');
    const isExpense = (row) => row.dataset.kind === 'expense';
    const isDuplicate = (row) => row.dataset.duplicate === '1';
    const isPossibleDuplicate = (row) => row.classList.contains('is-possible-duplicate');
    const isSelected = (row) => field(row, 'select').checked;

    function needsReview(row) {
        return !isDuplicate(row) && (isPossibleDuplicate(row) || ['', 'inne'].includes(fold(labelOf(row))));
    }

    function plural(count, one, few, many) {
        if (count === 1) {
            return one;
        }
        const tens = count % 100;
        return count % 10 >= 2 && count % 10 <= 4 && (tens < 12 || tens > 14) ? few : many;
    }

    function shortDate(value) {
        const parsed = new Date(`${value}T12:00:00`);
        if (Number.isNaN(parsed.getTime())) {
            return value;
        }
        const weekday = parsed.toLocaleDateString('pl-PL', { weekday: 'short' }).replace('.', '');
        const day = String(parsed.getDate()).padStart(2, '0');
        const month = String(parsed.getMonth() + 1).padStart(2, '0');
        return `${weekday} ${day}.${month}`;
    }

    // ---------------------------------------------------------------------
    // Zwięzły wiersz: odświeżanie po edycji
    // ---------------------------------------------------------------------
    function syncSummary(row) {
        const title = field(row, 'title').value.trim();
        row.querySelector('[data-sum-title]').textContent = title || '(bez opisu)';
        const label = labelOf(row) || 'Inne';
        const chip = row.querySelector('[data-sum-label]');
        chip.textContent = label;
        const remembered = row.querySelector('[data-bip-remember-label]');
        if (remembered) {
            remembered.textContent = label;
        }
        chip.classList.toggle('is-review', fold(label) === 'inne');
        row.querySelector('[data-sum-date]').textContent = shortDate(field(row, 'date').value);
        const sign = isExpense(row) ? '−' : '+';
        row.querySelector('[data-sum-amount]').textContent = `${sign}${money(amountOf(row))} zł`;

        if (isExpense(row)) {
            const store = storeOf(row);
            let storeNode = row.querySelector('[data-sum-store]');
            if (store && fold(store) !== fold(title)) {
                if (!storeNode) {
                    storeNode = document.createElement('span');
                    storeNode.className = 'bip-sum-store';
                    storeNode.dataset.sumStore = '';
                    chip.after(storeNode);
                }
                storeNode.textContent = store;
            } else if (storeNode) {
                storeNode.remove();
            }
        }
        row.classList.toggle('needs-review', needsReview(row));
        row.classList.toggle('is-unselected', !isSelected(row));
    }

    // ---------------------------------------------------------------------
    // Filtry, liczniki, suma zaznaczonych
    // ---------------------------------------------------------------------
    const FILTERS = {
        all: () => true,
        review: needsReview,
        expense: isExpense,
        income: (row) => !isExpense(row),
        duplicate: (row) => isDuplicate(row) || isPossibleDuplicate(row),
    };

    function syncCounts() {
        Object.entries(FILTERS).forEach(([name, test]) => {
            const node = form.querySelector(`[data-count="${name}"]`);
            if (node) {
                node.textContent = String(rows.filter(test).length);
            }
        });
    }

    function applyFilter() {
        let visible = 0;
        rows.forEach((row) => {
            row.hidden = !FILTERS[filter](row);
            if (!row.hidden) {
                visible += 1;
            }
        });
        empty.hidden = visible > 0;
        form.querySelectorAll('[data-bip-filter]').forEach((button) => {
            button.setAttribute('aria-pressed', String(button.dataset.bipFilter === filter));
        });
    }

    function syncTotal() {
        const chosen = rows.filter((row) => isSelected(row) && !isDuplicate(row));
        const expenses = chosen.filter(isExpense).reduce((sum, row) => sum + amountOf(row), 0);
        const incomes = chosen.filter((row) => !isExpense(row)).reduce((sum, row) => sum + amountOf(row), 0);
        const parts = [`${chosen.length} ${plural(chosen.length, 'pozycja', 'pozycje', 'pozycji')}`];
        if (expenses || !incomes) {
            parts.push(`−${money(expenses)} zł`);
        }
        if (incomes) {
            parts.push(`+${money(incomes)} zł`);
        }
        total.textContent = `Zaznaczono ${parts.join(' · ')}`;
        submitLabel.textContent = chosen.length ? `Importuj ${chosen.length}` : 'Nic nie zaznaczono';
        submit.disabled = chosen.length === 0;
    }

    function refresh() {
        syncCounts();
        applyFilter();
        syncTotal();
    }

    form.addEventListener('click', (event) => {
        const filterButton = event.target.closest('[data-bip-filter]');
        if (filterButton) {
            filter = filterButton.dataset.bipFilter;
            applyFilter();
            return;
        }
        const bulk = event.target.closest('[data-bip-select-visible]');
        if (bulk) {
            const value = bulk.dataset.bipSelectVisible === '1';
            rows.filter((row) => !row.hidden && !isDuplicate(row)).forEach((row) => {
                field(row, 'select').checked = value;
                syncSummary(row);
            });
            syncTotal();
            return;
        }
        const action = event.target.closest('[data-bip-propagate-apply], [data-bip-propagate-skip]');
        if (action) {
            const row = action.closest('[data-bip-row]');
            const box = row.querySelector('[data-bip-propagate]');
            if (action.matches('[data-bip-propagate-apply]')) {
                const value = labelOf(row);
                const others = sameStoreRows(row, value);
                others.forEach((other) => {
                    const input = field(other, 'category');
                    input.value = value;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    syncSummary(other);
                });
                box.textContent = `Zmieniono ${others.length} ${plural(others.length, 'pozycję', 'pozycje', 'pozycji')}.`;
                setTimeout(() => { box.hidden = true; }, 3500);
                syncCounts();
                syncTotal();
            } else {
                box.hidden = true;
            }
        }
    });

    // ---------------------------------------------------------------------
    // Ta sama kategoria dla pozostałych pozycji z tego sklepu
    // ---------------------------------------------------------------------
    function sameStoreRows(row, value) {
        const key = fold(storeOf(row));
        if (!key || !isExpense(row)) {
            return [];
        }
        return rows.filter((other) => other !== row && isExpense(other) && !isDuplicate(other)
            && fold(storeOf(other)) === key && labelOf(other) !== value);
    }

    function offerPropagation(row) {
        const value = labelOf(row);
        const box = row.querySelector('[data-bip-propagate]');
        const others = value ? sameStoreRows(row, value) : [];
        if (!others.length) {
            box.hidden = true;
            return;
        }
        const count = others.length;
        const text = document.createElement('span');
        text.textContent = `Zmienić też ${count} ${plural(count, 'pozostałą pozycję', 'pozostałe pozycje', 'pozostałych pozycji')} `
            + `„${storeOf(row)}” na „${value}”?`;
        const apply = document.createElement('button');
        apply.type = 'button';
        apply.className = 'btn btn-sm btn-primary';
        apply.dataset.bipPropagateApply = '';
        apply.textContent = 'Zmień';
        const skip = document.createElement('button');
        skip.type = 'button';
        skip.className = 'btn btn-sm btn-soft-neutral';
        skip.dataset.bipPropagateSkip = '';
        skip.textContent = 'Nie';
        box.replaceChildren(text, apply, skip);
        box.hidden = false;
    }

    // ---------------------------------------------------------------------
    // Zdarzenia pól
    // ---------------------------------------------------------------------
    form.addEventListener('input', (event) => {
        const row = event.target.closest('[data-bip-row]');
        if (!row) {
            return;
        }
        if (event.target.matches('[data-bip-category]') && isExpense(row)) {
            const target = row.querySelector('[data-bank-brokerage-target]');
            if (target) {
                const isInvestment = event.target.value === investment;
                target.classList.toggle('d-none', !isInvestment);
                if (!isInvestment) {
                    target.querySelector('select').value = '';
                }
            }
        }
        syncSummary(row);
        syncCounts();
        syncTotal();
    });

    form.addEventListener('change', (event) => {
        const row = event.target.closest('[data-bip-row]');
        if (!row) {
            return;
        }
        if (event.target.matches('[data-bip-category]')) {
            offerPropagation(row);
        }
        syncSummary(row);
        // Bez przeliczania filtra: poprawiona pozycja nie znika spod palca
        // (razem z propozycją dla tego sklepu). Filtr odświeża kliknięcie.
        syncCounts();
        syncTotal();
    });

    // Pole z błędem w zwiniętej albo ukrytej filtrem pozycji: pokaż ją.
    form.addEventListener('invalid', (event) => {
        const row = event.target.closest('[data-bip-row]');
        if (!row) {
            return;
        }
        if (row.hidden) {
            filter = 'all';
            applyFilter();
        }
        const details = row.querySelector('[data-bip-item]');
        if (details) {
            details.open = true;
        }
    }, true);

    form.addEventListener('submit', () => {
        submit.disabled = true;
        submitLabel.textContent = 'Importuję…';
    });
    window.addEventListener('pageshow', (event) => {
        if (event.persisted) {
            syncTotal();
        }
    });

    form.querySelector('[data-bip-bulk]').hidden = false;
    rows.forEach(syncSummary);
    refresh();
}());
