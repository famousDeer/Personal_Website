(() => {
    const dialog = document.querySelector('[data-pantry-edit-dialog]');
    if (!dialog?.showModal || !window.AppPartial) {
        return;
    }

    const home = dialog.parentElement;
    const body = dialog.querySelector('[data-pantry-edit-body]');
    const error = dialog.querySelector('[data-pantry-edit-error]');
    const closeButton = dialog.querySelector('[data-pantry-edit-close]');
    const parser = new DOMParser();
    let loading = null;
    let saving = false;
    let productId = '';

    function showError(message) {
        error.textContent = message;
        error.hidden = false;
        error.scrollIntoView({ block: 'nearest' });
    }

    document.addEventListener('click', async (event) => {
        const link = event.target.closest('[data-pantry-edit]');
        if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey
            || event.metaKey || event.altKey || event.shiftKey || dialog.open) {
            return;
        }
        event.preventDefault();

        productId = link.dataset.pantryEdit;
        const controller = new AbortController();
        loading = controller;
        error.hidden = true;
        body.textContent = 'Wczytuję dane produktu…';
        (link.closest('[data-pantry-detail]') || home).append(dialog);
        dialog.showModal();
        try {
            const response = await fetch(link.href, { signal: controller.signal });
            const doc = parser.parseFromString(await response.text(), 'text/html');
            const form = doc.querySelector('[data-pantry-edit-form]');
            const unitMeta = doc.getElementById('pantry-unit-meta');
            if (!response.ok || !form || !unitMeta) {
                throw new Error('Brak formularza edycji.');
            }
            if (controller.signal.aborted || !dialog.open) {
                return;
            }
            const deleteLink = document.createElement('a');
            deleteLink.href = `${link.href}#pantry-delete-product`;
            deleteLink.className = 'btn btn-link text-danger mt-3';
            deleteLink.textContent = 'Przejdź do usuwania produktu';
            body.replaceChildren(form, unitMeta, deleteLink);
            initPantryEditFields(body);
        } catch (failure) {
            if (!controller.signal.aborted && dialog.open) {
                body.replaceChildren();
                showError('Nie udało się wczytać edycji. Zamknij panel i spróbuj ponownie.');
            }
        }
    });

    dialog.addEventListener('submit', async (event) => {
        const form = event.target.closest('[data-pantry-edit-form]');
        if (!form) {
            return;
        }
        event.preventDefault();
        if (saving) {
            return;
        }
        saving = true;
        error.hidden = true;
        const payload = new FormData(form);
        payload.set('next', window.location.pathname + window.location.search);
        const saveButton = event.submitter || form.querySelector('button[type="submit"]');
        const fieldset = form.querySelector('fieldset');
        if (fieldset) {
            fieldset.disabled = true;
        }
        saveButton.disabled = true;
        closeButton.disabled = true;
        form.setAttribute('aria-busy', 'true');
        try {
            const response = await fetch(form.action, { method: 'POST', body: payload });
            const doc = parser.parseFromString(await response.text(), 'text/html');
            if (!response.ok) {
                const message = doc.querySelector('main .app-message.alert-danger, #app-toasts .is-error .app-toast-text');
                showError(message?.textContent.trim() || 'Nie udało się zapisać zmian. Sprawdź dane i spróbuj ponownie.');
                return;
            }
            if (!doc.getElementById('pantry-products')) {
                throw new Error('Brak zaktualizowanej listy.');
            }
            const row = document.getElementById(`pantry-row-${productId}`);
            dialog.close();
            window.AppPartial.apply(doc, '#pantry-products, #pantry-today, #pantry-category-filters', row);
            const editLink = document.querySelector(`[data-pantry-detail] [data-pantry-edit="${CSS.escape(productId)}"]`);
            const updatedRow = document.getElementById(`pantry-row-${productId}`);
            const updatedCard = document.getElementById(`product-${productId}`);
            const focusTarget = editLink || updatedRow?.querySelector('[data-open-detail]');
            if (focusTarget?.getClientRects().length) {
                focusTarget.focus({ preventScroll: true });
            } else {
                updatedCard?.querySelector('[data-pantry-edit]')?.focus({ preventScroll: true });
            }
        } catch (failure) {
            showError('Nie udało się uzyskać potwierdzenia zapisu. Sprawdź połączenie i spróbuj ponownie.');
        } finally {
            saving = false;
            if (fieldset) {
                fieldset.disabled = false;
            }
            saveButton.disabled = false;
            closeButton.disabled = false;
            form.removeAttribute('aria-busy');
        }
    });

    dialog.addEventListener('click', (event) => {
        const cancel = event.target.closest('[data-pantry-edit-cancel], [data-pantry-edit-close]');
        if (cancel) {
            event.preventDefault();
            if (!saving) {
                dialog.close();
            }
        }
    });
    dialog.addEventListener('cancel', (event) => {
        if (saving) {
            event.preventDefault();
        }
    });
    dialog.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            event.stopPropagation();
        }
    });
    dialog.addEventListener('close', () => {
        loading?.abort();
        body.replaceChildren();
        home.append(dialog);
    });
})();
