/* =========================================================================
   Formularz przepisu (cooking/templates/cooking/recipe_form.html)
   =========================================================================
   - Kroki i składniki: dodawanie z wzorów <template>, usuwanie z „Cofnij”,
     krok może nie mieć składników, numeracja przeliczana na bieżąco.
   - Wymagane pola ustawiane na bieżąco: nazwa składnika jest wymagana,
     gdy wpisano ilość (i odwrotnie), opis kroku - gdy krok ma treść.
   - Czas przepisu: puste pole = suma czasów kroków (liczy też serwer).
   - Szkic w przeglądarce (localStorage), zapisywany po każdej zmianie.
     Po powrocie na stronę pasek proponuje przywrócenie. Na komputerze
     dodatkowo ostrzeżenie przy wyjściu ze strony z niezapisanymi zmianami.
   - Zdjęcie: duży plik z telefonu zmniejszany w przeglądarce (do 2000 px,
     JPEG), żeby nie przekroczyć limitu serwera.
   - Składniki a spiżarnia (logika w recipe-ingredients.js): podpowiedzi
     nazw, stan „w spiżarni” / „nowy produkt” z „Chodziło o…?”, ostrzeżenie
     o jednostce, której „Gotuj” nie przeliczy, kategoria tylko przy nowym
     produkcie (dobierana z reguł słów kluczowych).
   - „Wklej listę”: skopiowana lista składników z podglądem przed dodaniem.
   - Kolejność: strzałki przy kroku, uchwyt przy składniku (przeciąganie,
     także do innego kroku, albo strzałki na klawiaturze).
   - Kategorie przepisu jako przyciski; ponowne kliknięcie odznacza.
   Bez JavaScriptu formularz dalej działa (serwer sprawdza to samo).
   ========================================================================= */
(function () {
    'use strict';

    const form = document.querySelector('[data-recipe-form]');
    if (!form) {
        return;
    }

    const stepList = form.querySelector('[data-step-list]');
    const stepTemplate = document.querySelector('[data-step-template]');
    const ingredientTemplate = document.querySelector('[data-ingredient-template]');
    const totalTime = form.querySelector('[data-total-time]');
    const timeHint = form.querySelector('[data-time-hint]');
    const statusNode = form.querySelector('[data-draft-status]');
    const banner = form.querySelector('[data-draft-banner]');
    const submitButton = form.querySelector('[data-submit]');
    const draftKey = form.dataset.draftKey;
    const failedSubmit = form.dataset.failed === '1';
    const maxImageBytes = Number(form.dataset.maxImageBytes) || 5 * 1024 * 1024;
    const DRAFT_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
    const R = window.RecipeIngredients || null;
    const pantryIndex = (() => {
        const node = document.getElementById('recipe-pantry-index');
        try {
            return node ? JSON.parse(node.textContent) : [];
        } catch (error) {
            return [];
        }
    })();
    const keywordRules = (() => {
        const node = document.getElementById('recipe-keyword-rules');
        try {
            return node ? JSON.parse(node.textContent) : [];
        } catch (error) {
            return [];
        }
    })();

    const ingredientList = form.querySelector('[data-ingredient-list]');
    const steps = () => Array.from(stepList.querySelectorAll('[data-step-card]'));
    const rows = () => Array.from(ingredientList.querySelectorAll('[data-ingredient-row]'));
    const field = (root, name) => root.querySelector(`[name="${name}"]`);
    const text = (root, name) => {
        const node = field(root, name);
        return node ? node.value.trim() : '';
    };
    const fromTemplate = (template) => template.content.firstElementChild.cloneNode(true);
    // Jednostki, przy których ilość nie jest potrzebna (szczypta = 1).
    const AMOUNT_OPTIONAL = new Set(['do_smaku', 'szczypta']);
    let stepKeys = 0;

    function autosize(textarea) {
        textarea.style.height = 'auto';
        textarea.style.height = `${textarea.scrollHeight + 2}px`;
    }

    // ---------------------------------------------------------------------
    // Kroki i składniki
    // ---------------------------------------------------------------------
    // Składnik pamięta krok po kluczu karty (data-step-key), nie po numerze:
    // przesunięcie kroku nie gubi przypisania, a usunięcie kroku i „Cofnij”
    // je przywraca. Pole ingredient_step dostaje numer przy każdej zmianie.
    function stepKey(step) {
        if (!step.dataset.stepKey) {
            stepKeys += 1;
            step.dataset.stepKey = `s${stepKeys}`;
        }
        return step.dataset.stepKey;
    }

    function rowIsFilled(row) {
        return Boolean(text(row, 'ingredient_name') || text(row, 'ingredient_quantity'));
    }

    function rowsIn(step) {
        const key = stepKey(step);
        return rows().filter((row) => row.dataset.stepKey === key);
    }

    function stepHasContent(step) {
        return Boolean(
            text(step, 'step_title') || text(step, 'step_instruction') || text(step, 'step_duration_minutes')
            || rowsIn(step).some(rowIsFilled)
        );
    }

    function syncRequirements() {
        rows().forEach((row) => {
            const optional = AMOUNT_OPTIONAL.has(field(row, 'ingredient_unit').value);
            const quantity = field(row, 'ingredient_quantity');
            field(row, 'ingredient_name').required = Boolean(text(row, 'ingredient_quantity'));
            quantity.required = Boolean(text(row, 'ingredient_name')) && !optional;
            quantity.placeholder = optional ? '–' : 'Ilość';
        });
        const all = steps();
        const anyContent = all.some(stepHasContent);
        all.forEach((step, index) => {
            // Krok musi mówić, co zrobić - wystarczy tytuł albo opis.
            // Gdy wszystkie kroki są puste, wymagany jest opis pierwszego.
            const needsText = stepHasContent(step) || (!anyContent && index === 0);
            field(step, 'step_instruction').required = needsText && !text(step, 'step_title');
        });
    }

    function syncTotalTime() {
        let sum = 0;
        form.querySelectorAll('[data-step-duration]').forEach((input) => {
            const minutes = parseInt(input.value, 10);
            if (minutes > 0) {
                sum += minutes;
            }
        });
        totalTime.placeholder = sum ? `= ${sum}` : 'auto';
        totalTime.required = sum === 0;
        timeHint.textContent = sum
            ? `Pusty czas = suma czasów kroków (${sum} min). Kcal można pominąć.`
            : 'Wpisz czas albo podaj czasy przy krokach. Kcal można pominąć.';
    }

    function stepOptionLabel(step, index) {
        const title = text(step, 'step_title');
        return title ? `Krok ${index + 1} · ${title}` : `Krok ${index + 1}`;
    }

    function syncStepSelects() {
        const all = steps();
        const keys = all.map(stepKey);
        rows().forEach((row) => {
            const select = field(row, 'ingredient_step');
            const options = [new Option('Bez kroku', '')];
            all.forEach((step, index) => options.push(new Option(stepOptionLabel(step, index), String(index))));
            select.replaceChildren(...options);
            const position = keys.indexOf(row.dataset.stepKey);
            select.value = position >= 0 ? String(position) : '';
        });
    }

    function syncSteps() {
        const all = steps();
        syncStepSelects();
        all.forEach((step, index) => {
            step.querySelector('[data-step-label]').textContent = `Krok ${index + 1}`;
            field(step, 'step_mix_after').value = String(index);
            const assigned = rowsIn(step).filter((row) => text(row, 'ingredient_name'));
            step.querySelector('[data-step-chips]').replaceChildren(
                ...assigned.map((row) => element('span', 'rf-step-chip', text(row, 'ingredient_name'))),
            );
            step.querySelector('[data-no-ingredients]').hidden = assigned.length > 0;
            step.querySelector('[data-mix-option]').hidden = assigned.length === 0;
            step.querySelector('[data-remove-step]').disabled = all.length === 1;
            step.querySelector('[data-move-step="-1"]').disabled = index === 0;
            step.querySelector('[data-move-step="1"]').disabled = index === all.length - 1;
            step.querySelector('[data-step-picker-open]').hidden = !step.querySelector('[data-step-picker]').hidden;
            if (!step.querySelector('[data-step-picker]').hidden) {
                renderPicker(step);
            }
        });
        form.querySelector('[data-paste-open]').hidden = !R || !form.querySelector('[data-paste-panel]').hidden;
        // Wybór kroku przy składniku na telefonie dopiero przy 2+ krokach.
        form.classList.toggle('has-many-steps', all.length > 1);
        syncRequirements();
        syncTotalTime();
    }

    function setSelect(select, value) {
        select.value = value;
        if (select.value !== value) {
            select.selectedIndex = 0;
        }
    }

    function addIngredient(data, focus) {
        const row = fromTemplate(ingredientTemplate);
        if (data) {
            field(row, 'ingredient_name').value = data.name || '';
            field(row, 'ingredient_quantity').value = data.quantity || '';
            setSelect(field(row, 'ingredient_unit'), data.unit || 'g');
            setSelect(field(row, 'ingredient_category'), data.category || '');
            if (data.category) {
                field(row, 'ingredient_category').dataset.touched = '1';
            }
            if (data.stepKey) {
                row.dataset.stepKey = data.stepKey;
            }
        }
        ingredientList.appendChild(row);
        syncRowStatus(row);
        syncSteps();
        if (focus) {
            field(row, 'ingredient_name').focus();
        }
        return row;
    }

    function addStep(data, focus) {
        const step = fromTemplate(stepTemplate);
        stepList.appendChild(step);
        stepKey(step);
        if (data) {
            field(step, 'step_title').value = data.title || '';
            field(step, 'step_duration_minutes').value = data.duration || '';
            field(step, 'step_instruction').value = data.instruction || '';
            field(step, 'step_mix_after').checked = Boolean(data.mix);
        }
        syncSteps();
        step.querySelectorAll('textarea').forEach(autosize);
        if (focus) {
            field(step, 'step_instruction').focus();
            step.scrollIntoView({ block: 'nearest' });
        }
        return step;
    }

    function removeWithUndo(node, message, worthUndo) {
        const parent = node.parentNode;
        const next = node.nextSibling;
        node.remove();
        syncSteps();
        changed();
        if (!worthUndo || !window.AppToast) {
            return;
        }
        window.AppToast.show(message, {
            tone: 'info',
            timeout: 8000,
            action: {
                label: 'Cofnij',
                onClick: () => {
                    parent.insertBefore(node, next && next.parentNode === parent ? next : null);
                    syncSteps();
                    changed();
                },
            },
        });
    }

    // Wybór składników z poziomu kroku: lista pól wyboru.
    function renderPicker(step) {
        const key = stepKey(step);
        const all = steps();
        const list = step.querySelector('[data-step-picker-list]');
        const named = rows().filter((row) => text(row, 'ingredient_name'));
        if (!named.length) {
            list.replaceChildren(element('span', 'small text-muted', 'Najpierw dodaj składniki na liście powyżej.'));
            return;
        }
        list.replaceChildren(...named.map((row) => {
            const label = element('label', 'form-check');
            const input = element('input', 'form-check-input');
            input.type = 'checkbox';
            input.checked = row.dataset.stepKey === key;
            input.dataset.pickRow = String(rows().indexOf(row));
            const other = all.find((candidate) => candidate !== step && stepKey(candidate) === row.dataset.stepKey);
            label.append(input, element('span', 'form-check-label', text(row, 'ingredient_name')));
            if (other) {
                label.append(' ', element('span', 'rf-picker-other', `(teraz krok ${all.indexOf(other) + 1})`));
            }
            return label;
        }));
    }

    form.addEventListener('click', (event) => {
        const button = event.target.closest('button');
        if (!button) {
            return;
        }
        if (button.matches('[data-add-step]')) {
            addStep(null, true);
            changed();
        } else if (button.matches('[data-add-ingredient]')) {
            addIngredient(null, true);
            changed();
        } else if (button.matches('[data-remove-ingredient]')) {
            const row = button.closest('[data-ingredient-row]');
            const name = text(row, 'ingredient_name');
            removeWithUndo(row, name ? `Usunięto składnik „${name}”.` : 'Usunięto składnik.', rowIsFilled(row));
        } else if (button.matches('[data-remove-step]')) {
            const step = button.closest('[data-step-card]');
            const label = step.querySelector('[data-step-label]').textContent;
            removeWithUndo(step, `Usunięto ${label.toLowerCase()}.`, stepHasContent(step));
        } else if (button.matches('[data-move-step]')) {
            moveStep(button.closest('[data-step-card]'), Number(button.dataset.moveStep), button);
        } else if (button.matches('[data-step-picker-open]')) {
            const step = button.closest('[data-step-card]');
            step.querySelector('[data-step-picker]').hidden = false;
            syncSteps();
            const first = step.querySelector('[data-step-picker-list] input');
            if (first) {
                first.focus();
            }
        } else if (button.matches('[data-step-picker-close]')) {
            const step = button.closest('[data-step-card]');
            step.querySelector('[data-step-picker]').hidden = true;
            syncSteps();
            step.querySelector('[data-step-picker-open]').focus();
        } else if (button.matches('[data-use-name]')) {
            const row = button.closest('[data-ingredient-row]');
            const entry = R.exact(button.dataset.useName, pantryIndex);
            if (entry) {
                pickSuggestion(field(row, 'ingredient_name'), entry, false);
            }
        } else if (button.matches('[data-show-category]')) {
            const row = button.closest('[data-ingredient-row]');
            row.classList.add('show-category');
            field(row, 'ingredient_category').focus();
        } else if (button.matches('[data-set-unit]')) {
            const row = button.closest('[data-ingredient-row]');
            field(row, 'ingredient_unit').value = button.dataset.setUnit;
            syncRowStatus(row);
            syncRequirements();
            changed();
        } else if (button.matches('[data-paste-open]')) {
            openPaste();
        } else if (button.matches('[data-paste-cancel]')) {
            closePaste();
        } else if (button.matches('[data-paste-apply]')) {
            applyPaste();
        }
    });

    form.addEventListener('change', (event) => {
        const target = event.target;
        if (target.matches('[data-pick-row]')) {
            const row = rows()[Number(target.dataset.pickRow)];
            const step = target.closest('[data-step-card]');
            const position = target.dataset.pickRow;
            if (row) {
                if (target.checked) {
                    row.dataset.stepKey = stepKey(step);
                } else {
                    delete row.dataset.stepKey;
                }
            }
            syncSteps();
            const again = step.querySelector(`[data-pick-row="${position}"]`);
            if (again) {
                again.focus();
            }
        } else if (target.matches('[data-step-select]')) {
            const row = target.closest('[data-ingredient-row]');
            const step = steps()[Number(target.value)];
            if (step) {
                row.dataset.stepKey = stepKey(step);
            } else {
                delete row.dataset.stepKey;
            }
            syncSteps();
        }
    });

    function moveStep(step, offset, button) {
        const all = steps();
        const target = all[all.indexOf(step) + offset];
        if (!target) {
            return;
        }
        stepList.insertBefore(step, offset < 0 ? target : target.nextSibling);
        syncSteps();
        changed();
        const again = step.querySelector(`[data-move-step="${offset}"]`);
        (again && !again.disabled ? again : button).focus({ preventScroll: true });
        step.scrollIntoView({ block: 'nearest' });
    }

    // ---------------------------------------------------------------------
    // Składniki a spiżarnia: podpowiedzi i stan wiersza
    // ---------------------------------------------------------------------
    const UNIT_LABEL = {
        g: 'g', kg: 'kg', ml: 'ml', l: 'l', szt: 'szt.', opak: 'opak.',
        lyzka: 'łyżka', lyzeczka: 'łyżeczka', szklanka: 'szklanka', szczypta: 'szczypta', do_smaku: 'do smaku',
    };
    const KITCHEN_VOLUME = new Set(['lyzka', 'lyzeczka', 'szklanka']);
    const FAMILY_HINT = { g: 'g lub kg', kg: 'g lub kg', ml: 'ml lub l', l: 'ml lub l', szt: 'szt.', opak: 'opak.' };

    function element(tag, className, content) {
        const node = document.createElement(tag);
        if (className) {
            node.className = className;
        }
        if (content !== undefined) {
            node.textContent = content;
        }
        return node;
    }

    function actionButton(label, data) {
        const button = element('button', 'rf-ing-action', label);
        button.type = 'button';
        Object.assign(button.dataset, data);
        return button;
    }

    function syncRowStatus(row, typing) {
        const name = text(row, 'ingredient_name');
        const status = row.querySelector('[data-ingredient-status]');
        const category = field(row, 'ingredient_category');
        row.classList.remove('is-known', 'is-new');
        status.className = 'rf-ing-status';
        status.replaceChildren();
        status.hidden = true;
        if (!R || !name) {
            return;
        }
        const known = R.exact(name, pantryIndex);
        if (known) {
            row.classList.add('is-known');
            category.value = '';  // kategorię weźmie serwer ze spiżarni
            const unit = field(row, 'ingredient_unit').value;
            const group = known.kind === 'group' ? ' · grupa marek' : '';
            if (R.sameFamily(unit, known.unit) || AMOUNT_OPTIONAL.has(unit)
                || (KITCHEN_VOLUME.has(unit) && ['ml', 'l'].includes(known.unit))) {
                status.append(element('i', 'bi bi-check2'), element('span', 'rf-ing-text', `W spiżarni${group}`));
            } else if (KITCHEN_VOLUME.has(unit)) {
                // Łyżka mąki to nie łyżka oleju - „Gotuj” poprosi o zważenie.
                status.append(element('i', 'bi bi-check2'),
                    element('span', 'rf-ing-text', `W spiżarni w ${FAMILY_HINT[known.unit] || known.unit} · przy gotowaniu zważysz`));
            } else {
                status.classList.add('is-warning');
                status.append(
                    element('i', 'bi bi-exclamation-triangle'),
                    element('span', 'rf-ing-text', `W spiżarni liczone w ${FAMILY_HINT[known.unit] || known.unit} – „Gotuj” nie przeliczy ${UNIT_LABEL[unit] || unit}.`),
                    actionButton(`Ustaw ${UNIT_LABEL[known.unit] || known.unit}`, { setUnit: known.unit }),
                );
            }
            status.hidden = false;
            return;
        }
        if (typing) {
            return;  // nowy produkt pokazujemy dopiero po wpisaniu nazwy
        }
        row.classList.add('is-new');
        status.classList.add('is-new');
        status.append(element('i', 'bi bi-plus-circle'), element('span', 'rf-ing-text', 'Nowy produkt – „Gotuj” doda go do spiżarni.'));
        const near = R.closest(name, pantryIndex);
        if (near) {
            status.append(actionButton(`Chodziło o „${near.entry.name}”?`, { useName: near.entry.name }));
        }
        if (!category.dataset.touched) {
            setSelect(category, R.categoryFor(name, keywordRules));
        }
        // Na telefonie kategoria jest schowana za tym przyciskiem.
        const chosen = category.value || 'auto';
        const toggle = actionButton(`Kategoria: ${chosen} · zmień`, { showCategory: '1' });
        toggle.classList.add('rf-only-mobile');
        status.append(toggle);
        status.hidden = false;
    }

    let combo = null;  // otwarta lista podpowiedzi: {input, list, items, index}
    let comboId = 0;

    function closeSuggestions() {
        if (!combo) {
            return;
        }
        combo.list.hidden = true;
        combo.list.replaceChildren();
        combo.input.setAttribute('aria-expanded', 'false');
        combo.input.removeAttribute('aria-activedescendant');
        combo = null;
    }

    function openSuggestions(input) {
        const row = input.closest('[data-ingredient-row]');
        const list = row.querySelector('[data-suggest]');
        const items = R ? R.search(input.value, pantryIndex, 6) : [];
        const onlySame = items.length === 1 && R.sameName(items[0].name) === R.sameName(input.value);
        if (!items.length || onlySame) {
            closeSuggestions();
            return;
        }
        if (combo && combo.input !== input) {
            closeSuggestions();
        }
        if (!list.id) {
            comboId += 1;
            list.id = `rf-suggest-${comboId}`;
        }
        list.replaceChildren(...items.map((entry, position) => {
            const option = element('div', 'rf-suggest-item');
            option.id = `${list.id}-${position}`;
            option.setAttribute('role', 'option');
            option.dataset.position = String(position);
            const meta = [entry.kind === 'group' ? `grupa ${entry.members || ''} marek`.replace('  ', ' ') : '', UNIT_LABEL[entry.unit] || entry.unit, entry.category]
                .filter(Boolean).join(' · ');
            option.append(element('span', 'rf-suggest-name', entry.name), element('span', 'rf-suggest-meta', meta));
            return option;
        }));
        list.hidden = false;
        input.setAttribute('aria-expanded', 'true');
        input.setAttribute('aria-controls', list.id);
        combo = { input, list, items, index: -1 };
    }

    function highlight(index) {
        if (!combo) {
            return;
        }
        combo.index = (index + combo.items.length) % combo.items.length;
        combo.list.querySelectorAll('[role="option"]').forEach((option, position) => {
            option.setAttribute('aria-selected', position === combo.index ? 'true' : 'false');
        });
        combo.input.setAttribute('aria-activedescendant', `${combo.list.id}-${combo.index}`);
    }

    function pickSuggestion(input, entry, moveFocus) {
        const row = input.closest('[data-ingredient-row]');
        input.value = entry.name;
        const unit = field(row, 'ingredient_unit');
        // Jednostkę kuchenną (łyżka, do smaku) zostawiamy - „Gotuj” ją obsłuży.
        if (!R.sameFamily(unit.value, entry.unit) && !AMOUNT_OPTIONAL.has(unit.value) && !KITCHEN_VOLUME.has(unit.value)) {
            unit.value = entry.unit;
        }
        closeSuggestions();
        syncRowStatus(row);
        syncRequirements();
        changed();
        if (moveFocus) {
            field(row, 'ingredient_quantity').focus();
        }
    }

    // Dotknięcie podpowiedzi nie zabiera fokusu z pola nazwy (pointerdown),
    // a wybór następuje dopiero po puszczeniu (click) - przewijanie strony
    // palcem po liście niczego nie wybiera.
    form.addEventListener('pointerdown', (event) => {
        if (event.target.closest('.rf-suggest-item')) {
            event.preventDefault();
        }
    });
    form.addEventListener('click', (event) => {
        const option = event.target.closest('.rf-suggest-item');
        if (option && combo) {
            pickSuggestion(combo.input, combo.items[Number(option.dataset.position)], true);
        }
    });

    form.addEventListener('keydown', (event) => {
        const input = event.target;
        if (input.name === 'ingredient_name' && combo && combo.input === input) {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                highlight(combo.index + (event.key === 'ArrowDown' ? 1 : -1));
            } else if (event.key === 'Enter' && combo.index >= 0) {
                event.preventDefault();
                pickSuggestion(input, combo.items[combo.index], true);
            } else if (event.key === 'Escape') {
                event.preventDefault();
                closeSuggestions();
            }
        } else if (input.name === 'ingredient_name' && event.key === 'Enter') {
            event.preventDefault();  // Enter w nazwie nie wysyła formularza
            field(input.closest('[data-ingredient-row]'), 'ingredient_quantity').focus();
        } else if (input.matches('[data-drag-ingredient]') && (event.key === 'ArrowUp' || event.key === 'ArrowDown')) {
            event.preventDefault();
            nudgeRow(input.closest('[data-ingredient-row]'), event.key === 'ArrowUp' ? -1 : 1);
            input.focus();
        }
    });

    form.addEventListener('focusout', (event) => {
        if (event.target.name === 'ingredient_name') {
            const row = event.target.closest('[data-ingredient-row]');
            if (combo && combo.input === event.target) {
                closeSuggestions();
            }
            syncRowStatus(row);
        }
    });

    form.addEventListener('focusin', (event) => {
        if (event.target.name === 'ingredient_name' && event.target.value.trim()) {
            openSuggestions(event.target);
        }
    });

    // ---------------------------------------------------------------------
    // Wklejanie listy składników
    // ---------------------------------------------------------------------
    function plural(count, one, few, many) {
        if (count === 1) {
            return one;
        }
        const tens = count % 100;
        return count % 10 >= 2 && count % 10 <= 4 && (tens < 12 || tens > 14) ? few : many;
    }

    const pastePanel = form.querySelector('[data-paste-panel]');
    const pasteInput = pastePanel.querySelector('[data-paste-input]');

    function openPaste() {
        pastePanel.hidden = false;
        form.querySelector('[data-paste-open]').hidden = true;
        pasteInput.focus();
    }

    function closePaste() {
        pasteInput.value = '';
        autosize(pasteInput);
        renderPaste();
        pastePanel.hidden = true;
        form.querySelector('[data-paste-open]').hidden = false;
    }

    function renderPaste() {
        const items = R.parseList(pasteInput.value, pantryIndex);
        const preview = pastePanel.querySelector('[data-paste-preview]');
        preview.replaceChildren(...items.map((item) => {
            const line = element('li', `rf-paste-item${item.match ? ' is-known' : ' is-new'}`);
            line.append(
                element('span', 'rf-paste-amount', R.amountText(item.quantity, item.unit)),
                element('span', 'rf-paste-name', item.name),
                element('span', 'rf-paste-badge', item.match ? 'w spiżarni' : 'nowy produkt'),
            );
            const notes = [];
            if (item.renamed) {
                notes.push(`z „${item.renamed}”`);
            }
            if (item.note) {
                notes.push(item.note);
            }
            if (notes.length) {
                line.append(element('span', 'rf-paste-note', notes.join(' · ')));
            }
            return line;
        }));
        if (items.some((item) => !item.match)) {
            preview.append(element('li', 'rf-paste-hint',
                'Nowe produkty trafią do spiżarni pod tą nazwą – popraw ją na podstawową formę (cebula, nie cebuli).'));
        }
        const apply = pastePanel.querySelector('[data-paste-apply]');
        apply.disabled = !items.length;
        apply.textContent = items.length
            ? `Dodaj ${items.length} ${plural(items.length, 'składnik', 'składniki', 'składników')}`
            : 'Dodaj składniki';
        return items;
    }

    function applyPaste() {
        const items = renderPaste();
        if (!items.length) {
            return;
        }
        rows().filter((row) => !rowIsFilled(row)).forEach((row) => row.remove());
        const added = items.map((item) => addIngredient({ name: item.name, quantity: item.quantity, unit: item.unit }));
        closePaste();
        syncSteps();
        changed();
        const missing = items.filter((item) => !item.quantity && !AMOUNT_OPTIONAL.has(item.unit)).length;
        if (window.AppToast) {
            window.AppToast.show(
                `Dodano ${items.length} ${plural(items.length, 'składnik', 'składniki', 'składników')}.`
                + (missing ? ` Uzupełnij ilość przy ${missing}.` : '')
                + (steps().length > 1 ? ' Krok możesz przypisać przy składniku.' : ''),
                { tone: 'success' },
            );
        }
        const firstEmpty = added.find((row) => !text(row, 'ingredient_quantity') && !AMOUNT_OPTIONAL.has(field(row, 'ingredient_unit').value));
        if (firstEmpty) {
            field(firstEmpty, 'ingredient_quantity').focus();
        }
    }

    // ---------------------------------------------------------------------
    // Kolejność składników: przeciąganie uchwytu (też między krokami)
    // ---------------------------------------------------------------------
    let drag = null;

    function nudgeRow(row, offset) {
        const siblings = rows();
        const target = siblings[siblings.indexOf(row) + offset];
        if (!target) {
            return;
        }
        ingredientList.insertBefore(row, offset < 0 ? target : target.nextSibling);
        syncSteps();
        changed();
        row.scrollIntoView({ block: 'nearest' });
    }

    function placeDraggedRow(y) {
        const others = rows().filter((row) => row !== drag.row);
        const before = others.find((row) => {
            const box = row.getBoundingClientRect();
            return y < box.top + box.height / 2;
        }) || null;
        if (drag.row.nextElementSibling !== before) {
            ingredientList.insertBefore(drag.row, before);
            drag.moved = true;
            try {
                // Przeniesienie w drzewie zwalnia przechwycenie wskaźnika.
                drag.handle.setPointerCapture(drag.id);
            } catch (error) { /* wskaźnik już puszczony */ }
        }
    }

    function dragFrame() {
        if (!drag) {
            return;
        }
        const edge = 90;
        const bottomEdge = window.innerHeight - 170;  // przypięty pasek i nawigacja
        if (drag.y < edge) {
            window.scrollBy({ top: -12, behavior: 'instant' });
            placeDraggedRow(drag.y);
        } else if (drag.y > bottomEdge) {
            window.scrollBy({ top: 12, behavior: 'instant' });
            placeDraggedRow(drag.y);
        }
        drag.frame = requestAnimationFrame(dragFrame);
    }

    form.addEventListener('pointerdown', (event) => {
        const handle = event.target.closest('[data-drag-ingredient]');
        if (!handle || event.button > 0) {
            return;
        }
        event.preventDefault();
        closeSuggestions();
        handle.setPointerCapture(event.pointerId);
        drag = { row: handle.closest('[data-ingredient-row]'), handle, id: event.pointerId, y: event.clientY, moved: false };
        drag.row.classList.add('is-dragging');
        drag.frame = requestAnimationFrame(dragFrame);
    });

    document.addEventListener('pointermove', (event) => {
        if (!drag || event.pointerId !== drag.id) {
            return;
        }
        drag.y = event.clientY;
        placeDraggedRow(event.clientY);
    });

    function endDrag(event) {
        if (!drag || event.pointerId !== drag.id) {
            return;
        }
        cancelAnimationFrame(drag.frame);
        drag.row.classList.remove('is-dragging');
        const moved = drag.moved;
        drag = null;
        syncSteps();
        if (moved) {
            changed();
        }
    }
    document.addEventListener('pointerup', endDrag);
    document.addEventListener('pointercancel', endDrag);

    // ---------------------------------------------------------------------
    // Kategorie przepisu (przyciski)
    // ---------------------------------------------------------------------
    const tags = form.querySelector('[data-tags]');
    const chosenValue = {};

    function chipText(input) {
        return input.closest('.rf-chip').textContent.trim();
    }

    function syncTagsSummary() {
        const chosen = Array.from(form.querySelectorAll('.rf-chip input:checked')).map(chipText);
        const node = form.querySelector('[data-tags-chosen]');
        if (node) {
            node.textContent = chosen.join(' · ');
        }
        form.querySelectorAll('.rf-chip input').forEach((input) => {
            chosenValue[input.name] = chosenValue[input.name] || '';
            if (input.checked) {
                chosenValue[input.name] = input.value;
            }
        });
    }

    form.addEventListener('click', (event) => {
        const input = event.target.closest('.rf-chip input');
        if (!input) {
            return;
        }
        if (chosenValue[input.name] === input.value) {
            input.checked = false;  // ponowne kliknięcie odznacza
            chosenValue[input.name] = '';
        } else {
            chosenValue[input.name] = input.value;
        }
        syncTagsSummary();
        changed();
    });

    // ---------------------------------------------------------------------
    // Stan formularza, szkic i ostrzeżenie przy wyjściu
    // ---------------------------------------------------------------------
    const SIMPLE_FIELDS = [
        'title', 'description', 'portions', 'kcal', 'preparation_time',
        'kitchen_region', 'meal_type', 'type_of_dish',
    ];

    function serialize() {
        const data = {};
        SIMPLE_FIELDS.forEach((name) => {
            const control = form.elements[name];
            data[name] = control ? String(control.value || '').trim() : '';
        });
        const all = steps();
        data.steps = all.map((step) => ({
            title: text(step, 'step_title'),
            duration: text(step, 'step_duration_minutes'),
            instruction: text(step, 'step_instruction'),
            mix: field(step, 'step_mix_after').checked,
        }));
        const keys = all.map(stepKey);
        data.ingredients = rows().filter(rowIsFilled).map((row) => ({
            name: text(row, 'ingredient_name'),
            quantity: text(row, 'ingredient_quantity'),
            unit: field(row, 'ingredient_unit').value,
            category: field(row, 'ingredient_category').value,
            step: keys.indexOf(row.dataset.stepKey),
        }));
        return data;
    }

    function applyState(data) {
        SIMPLE_FIELDS.forEach((name) => {
            const control = form.elements[name];
            if (!control) {
                return;
            }
            if (control instanceof RadioNodeList) {
                Array.from(control).forEach((radio) => {
                    radio.checked = radio.value === (data[name] || '');
                });
            } else if (control.tagName === 'SELECT') {
                setSelect(control, data[name] || '');
            } else {
                control.value = data[name] || '';
            }
        });
        syncTagsSummary();
        steps().forEach((step) => step.remove());
        rows().forEach((row) => row.remove());
        const savedSteps = data.steps && data.steps.length ? data.steps : [null];
        const created = savedSteps.map((step) => addStep(step));
        const savedRows = data.ingredients && data.ingredients.length ? data.ingredients : [null];
        savedRows.forEach((item) => addIngredient(item && {
            ...item,
            stepKey: created[item.step] ? stepKey(created[item.step]) : '',
        }));
        form.querySelectorAll('textarea').forEach(autosize);
        syncSteps();
    }

    const storage = (() => {
        try {
            const probe = '__recipe_form__';
            window.localStorage.setItem(probe, '1');
            window.localStorage.removeItem(probe);
            return window.localStorage;
        } catch (error) {
            return null;
        }
    })();

    function readDraft() {
        if (!storage) {
            return null;
        }
        try {
            const draft = JSON.parse(storage.getItem(draftKey));
            if (!draft || !draft.data || Date.now() - draft.savedAt > DRAFT_MAX_AGE_MS) {
                return null;
            }
            return draft;
        } catch (error) {
            return null;
        }
    }

    function clearDraft() {
        if (storage) {
            try {
                storage.removeItem(draftKey);
            } catch (error) { /* brak miejsca albo tryb prywatny */ }
        }
    }

    const clock = (timestamp) => new Date(timestamp).toLocaleString('pl-PL', {
        day: 'numeric', month: 'numeric', hour: '2-digit', minute: '2-digit',
    });

    let initialJson = '';
    let photoChanged = false;
    let submitting = false;
    let pendingDraft = null;  // szkic czeka na decyzję - nie nadpisujemy go
    let saveTimer = null;

    function isDirty() {
        return failedSubmit || photoChanged || JSON.stringify(serialize()) !== initialJson;
    }

    function setStatus(message) {
        if (statusNode) {
            statusNode.textContent = message;
        }
    }

    function saveDraft() {
        clearTimeout(saveTimer);
        saveTimer = null;
        if (!storage || submitting || pendingDraft) {
            return;
        }
        if (!isDirty()) {
            clearDraft();
            setStatus('');
            return;
        }
        const now = Date.now();
        try {
            storage.setItem(draftKey, JSON.stringify({
                savedAt: now,
                base: initialJson,
                hasImage: photoChanged,
                data: serialize(),
            }));
            setStatus(`Szkic zapisany na tym urządzeniu · ${new Date(now).toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit' })}`);
        } catch (error) {
            setStatus('Nie udało się zapisać szkicu na tym urządzeniu.');
        }
    }

    function changed() {
        if (pendingDraft) {
            setStatus('Najpierw przywróć albo odrzuć szkic u góry.');
            return;
        }
        clearTimeout(saveTimer);
        saveTimer = setTimeout(saveDraft, 600);
    }

    form.addEventListener('input', (event) => {
        if (event.target.matches('textarea')) {
            autosize(event.target);
        }
        if (event.target.matches('[data-step-duration]')) {
            syncTotalTime();
        }
        if (event.target.closest('[data-ingredient-row]') || event.target.closest('[data-step-card]')) {
            syncRequirements();
        }
        if (event.target.name === 'ingredient_name') {
            openSuggestions(event.target);
            syncRowStatus(event.target.closest('[data-ingredient-row]'), true);
        }
        if (event.target.matches('[data-paste-input]')) {
            renderPaste();
            return;  // sam tekst do wklejenia nie jest częścią przepisu
        }
        if (event.target.name === 'step_title' || event.target.name === 'ingredient_name') {
            syncStepSelects();  // nazwy kroków w wyborze i składniki przy krokach
            steps().forEach((step) => {
                step.querySelector('[data-step-chips]').replaceChildren(
                    ...rowsIn(step).filter((row) => text(row, 'ingredient_name'))
                        .map((row) => element('span', 'rf-step-chip', text(row, 'ingredient_name'))),
                );
            });
        }
        changed();
    });
    form.addEventListener('change', (event) => {
        if (event.target.name === 'ingredient_category') {
            event.target.dataset.touched = '1';
        }
        if (event.target.name === 'ingredient_unit') {
            syncRowStatus(event.target.closest('[data-ingredient-row]'));
            syncRequirements();
        }
        if (event.target.matches('[data-paste-input]') || event.target.matches('[data-pick-row]')) {
            if (event.target.matches('[data-pick-row]')) {
                changed();
            }
            return;
        }
        changed();
    });

    function showDraftBanner(draft) {
        let message = `Na tym urządzeniu jest niezapisany szkic z ${clock(draft.savedAt)}.`;
        if (draft.base && draft.base !== initialJson && !failedSubmit) {
            message += ' Przepis zmienił się od tego czasu.';
        }
        if (draft.hasImage) {
            message += ' Zdjęcie trzeba będzie wybrać jeszcze raz.';
        }
        banner.querySelector('[data-draft-text]').textContent = message;
        banner.hidden = false;
        pendingDraft = draft;
    }

    banner.querySelector('[data-draft-restore]').addEventListener('click', () => {
        const draft = pendingDraft;
        pendingDraft = null;
        banner.hidden = true;
        applyState(draft.data);
        saveDraft();
        field(form, 'title').focus();
    });

    banner.querySelector('[data-draft-discard]').addEventListener('click', () => {
        pendingDraft = null;
        banner.hidden = true;
        clearDraft();
        saveDraft();
    });

    // Telefon może zamknąć kartę bez ostrzeżenia - szkic zapisujemy od razu.
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden' && saveTimer) {
            saveDraft();
        }
    });
    window.addEventListener('pagehide', () => {
        if (saveTimer) {
            saveDraft();
        }
    });

    window.addEventListener('beforeunload', (event) => {
        if (submitting || !isDirty()) {
            return;
        }
        if (saveTimer) {
            saveDraft();
        }
        event.preventDefault();
        event.returnValue = '';
    });

    // Powrót przyciskiem „wstecz” z pamięci przeglądarki: przycisk znów działa.
    window.addEventListener('pageshow', (event) => {
        if (event.persisted) {
            submitting = false;
            submitButton.disabled = false;
            submitButton.classList.remove('is-busy');
        }
    });

    // ---------------------------------------------------------------------
    // Zdjęcie: podgląd i zmniejszanie przed wysłaniem
    // ---------------------------------------------------------------------
    const photo = form.querySelector('[data-photo]');
    const photoInput = form.querySelector('[data-photo-input]');
    const photoPreview = form.querySelector('[data-photo-preview]');
    const photoLabel = form.querySelector('[data-photo-label]');
    const photoHint = form.querySelector('[data-photo-hint]');
    const photoError = form.querySelector('[data-photo-error]');
    const photoRemove = form.querySelector('[data-photo-remove]');
    const ACCEPTED_TYPES = ['image/jpeg', 'image/png', 'image/webp'];
    const MAX_SIDE = 2000;
    const SHRINK_ABOVE = 1.5 * 1024 * 1024;
    let photoJob = null;
    let previewUrl = '';

    const megabytes = (bytes) => `${(bytes / 1024 / 1024).toLocaleString('pl-PL', { maximumFractionDigits: 1 })} MB`;

    function showPhotoError(message) {
        photoError.textContent = message;
        photoError.hidden = !message;
    }

    function loadImage(file) {
        return new Promise((resolve, reject) => {
            const url = URL.createObjectURL(file);
            const image = new Image();
            image.onload = () => {
                URL.revokeObjectURL(url);
                resolve(image);
            };
            image.onerror = () => {
                URL.revokeObjectURL(url);
                reject(new Error('unreadable'));
            };
            image.src = url;
        });
    }

    async function shrink(file) {
        const image = await loadImage(file);
        const width = image.naturalWidth;
        const height = image.naturalHeight;
        const needsWork = file.size > SHRINK_ABOVE || Math.max(width, height) > MAX_SIDE
            || !ACCEPTED_TYPES.includes(file.type);
        if (!needsWork) {
            return file;
        }
        const scale = Math.min(1, MAX_SIDE / Math.max(width, height));
        const canvas = document.createElement('canvas');
        canvas.width = Math.round(width * scale);
        canvas.height = Math.round(height * scale);
        const context = canvas.getContext('2d');
        context.fillStyle = '#ffffff';  // przezroczysty PNG nie będzie czarny
        context.fillRect(0, 0, canvas.width, canvas.height);
        context.drawImage(image, 0, 0, canvas.width, canvas.height);
        const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.85));
        if (!blob) {
            throw new Error('encode');
        }
        const name = `${(file.name || 'zdjecie').replace(/\.[^.]+$/, '')}.jpg`;
        return new File([blob], name, { type: 'image/jpeg', lastModified: Date.now() });
    }

    function replaceInputFile(file) {
        try {
            const transfer = new DataTransfer();
            transfer.items.add(file);
            photoInput.files = transfer.files;
            return true;
        } catch (error) {
            return false;
        }
    }

    function showPreview(file) {
        if (previewUrl) {
            URL.revokeObjectURL(previewUrl);
        }
        previewUrl = URL.createObjectURL(file);
        photoPreview.src = previewUrl;
        photoPreview.hidden = false;
        photo.classList.add('has-image');
        photo.classList.remove('is-removed');
        photoLabel.textContent = 'Zmień zdjęcie';
    }

    async function preparePhoto() {
        const original = photoInput.files && photoInput.files[0];
        showPhotoError('');
        if (!original) {
            return;
        }
        photo.classList.add('is-busy');
        photoLabel.textContent = 'Przygotowuję zdjęcie…';
        try {
            const ready = await shrink(original);
            if (ready !== original && !replaceInputFile(ready) && original.size > maxImageBytes) {
                throw new Error('too-big');
            }
            if (ready === original && original.size > maxImageBytes) {
                throw new Error('too-big');
            }
            showPreview(ready);
            photoChanged = true;
            if (photoRemove) {
                photoRemove.checked = false;
            }
            photoHint.textContent = ready !== original
                ? `Zdjęcie zmniejszone przed wysłaniem: ${megabytes(original.size)} → ${megabytes(ready.size)}.`
                : 'Zdjęcie gotowe do wysłania.';
        } catch (error) {
            photoInput.value = '';
            photoChanged = false;
            photoLabel.textContent = photoPreview.hidden ? 'Dodaj zdjęcie dania' : 'Zmień zdjęcie';
            showPhotoError(error.message === 'too-big'
                ? `To zdjęcie jest za duże (${megabytes(original.size)}, limit ${megabytes(maxImageBytes)}). Wybierz mniejsze.`
                : 'Nie udało się odczytać zdjęcia. Wybierz plik JPG, PNG albo WEBP.');
        } finally {
            photo.classList.remove('is-busy');
            changed();
        }
    }

    photoInput.addEventListener('change', () => {
        photoJob = preparePhoto().finally(() => {
            photoJob = null;
        });
    });

    if (photoRemove) {
        photoRemove.addEventListener('change', () => {
            photo.classList.toggle('is-removed', photoRemove.checked);
        });
    }

    // ---------------------------------------------------------------------
    // Wysyłanie
    // ---------------------------------------------------------------------
    form.addEventListener('submit', (event) => {
        if (submitting) {
            event.preventDefault();
            return;
        }
        if (photoJob) {
            // Zdjęcie jeszcze się zmniejsza - wyślij, gdy będzie gotowe.
            event.preventDefault();
            submitButton.disabled = true;
            photoJob.then(() => {
                submitButton.disabled = false;
                form.requestSubmit(submitButton);
            });
            return;
        }
        if (saveTimer) {
            saveDraft();
        }
        submitting = true;
        submitButton.disabled = true;
        submitButton.classList.add('is-busy');
        // Szkic zostaje do potwierdzenia zapisu: kasuje go lista przepisów
        // po udanym zapisie. Gdy serwer nie odpowie, nic nie przepada.
    });

    // ---------------------------------------------------------------------
    // Start
    // ---------------------------------------------------------------------
    if (tags && window.matchMedia('(min-width: 992px)').matches) {
        tags.open = true;
    }
    form.classList.add('js-ready');
    form.querySelectorAll('[data-ingredient-category]').forEach((select) => {
        if (select.value) {
            select.dataset.touched = '1';  // wybrana wcześniej - nie zmieniamy sama
        }
    });
    // Przypisania z serwera (numer kroku) zamieniamy na klucze kart.
    const initialSteps = steps();
    initialSteps.forEach(stepKey);
    rows().forEach((row) => {
        const step = initialSteps[Number(field(row, 'ingredient_step').value)];
        if (field(row, 'ingredient_step').value !== '' && step) {
            row.dataset.stepKey = stepKey(step);
        }
    });
    rows().forEach((row) => syncRowStatus(row));
    syncTagsSummary();
    form.querySelectorAll('textarea').forEach(autosize);
    syncSteps();
    initialJson = JSON.stringify(serialize());

    const draft = readDraft();
    if (failedSubmit) {
        // Serwer odesłał formularz z błędami: to, co jest na ekranie, jest
        // najnowsze. Zapisujemy to jako szkic.
        saveDraft();
        const summary = form.querySelector('[data-error-summary]');
        if (summary) {
            summary.focus({ preventScroll: true });
            summary.scrollIntoView({ block: 'start' });
        }
    } else if (draft && JSON.stringify(draft.data) !== initialJson) {
        showDraftBanner(draft);
    }
}());
