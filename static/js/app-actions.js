/* =========================================================================
   Akcje bez przeładowania, dymki i wspólne potwierdzenie (audyt, etap 2)
   =========================================================================
   Trzy rzeczy, z których korzysta cała aplikacja:

   1. AppToast - dymek na dole ekranu ("Zapisano", "Usunięto · Cofnij").
      Komunikaty z przyciskiem "Cofnij" serwer renderuje w #app-toasts
      (templates/base.html), a ten skrypt je pokazuje i chowa.

   2. Wspólne potwierdzenie. Formularz z atrybutem data-confirm="Pytanie"
      przed wysłaniem otwiera jeden arkusz potwierdzenia zamiast confirm()
      przeglądarki. Tylko dla rzadkich, nieodwracalnych akcji (usunięcie
      listy, konta, przepisu). Częste usunięcia mają "Cofnij" zamiast pytania.
        data-confirm          tytuł pytania
        data-confirm-detail   zdanie wyjaśnienia (opcjonalnie)
        data-confirm-action   napis przycisku (domyślnie "Usuń")

   3. Formularze z atrybutem data-partial="#a, #b" wysyłają się w tle.
      Serwer odpowiada tak jak zwykle (przekierowanie pod adres z pola
      next), a skrypt podmienia na stronie tylko wskazane fragmenty
      i pokazuje komunikat w dymku. Strona nie mruga i nie wraca na górę.
      Otwarte sekcje (details, collapse) i fokus zostają na miejscu.
        data-optimistic="nazwa"  natychmiastowa zmiana w interfejsie przed
                                 odpowiedzią serwera (AppPartial.optimistic)
      Bez JavaScriptu te same formularze działają zwyczajnie.
   ========================================================================= */
(function (global) {
    'use strict';

    const reduceMotion = () => global.matchMedia('(prefers-reduced-motion: reduce)').matches;

    // ---------------------------------------------------------------------
    // 1. Dymki
    // ---------------------------------------------------------------------
    const TONES = { success: 'success', info: 'info', warning: 'warning', danger: 'danger', error: 'danger' };

    function toastRegion() {
        let region = document.getElementById('app-toasts');
        if (!region) {
            region = document.createElement('div');
            region.id = 'app-toasts';
            region.className = 'app-toasts';
            region.setAttribute('aria-live', 'polite');
            document.body.appendChild(region);
        }
        return region;
    }

    function dismissToast(node) {
        if (!node || node.dataset.leaving) {
            return;
        }
        node.dataset.leaving = '1';
        clearTimeout(node._timer);
        node.classList.add('is-leaving');
        const remove = () => node.remove();
        if (reduceMotion()) {
            setTimeout(remove, 160);
        } else {
            node.addEventListener('transitionend', remove, { once: true });
            setTimeout(remove, 400);
        }
    }

    function armToast(node) {
        if (node.dataset.armed) {
            return;
        }
        node.dataset.armed = '1';
        const timeout = Number(node.dataset.toastTimeout || 4500);
        const start = () => {
            clearTimeout(node._timer);
            node._timer = setTimeout(() => dismissToast(node), timeout);
        };
        // Czas stoi, gdy ktoś celuje w "Cofnij" albo ma na nim fokus.
        node.addEventListener('pointerenter', () => clearTimeout(node._timer));
        node.addEventListener('pointerleave', start);
        node.addEventListener('focusin', () => clearTimeout(node._timer));
        node.addEventListener('focusout', start);
        const close = node.querySelector('[data-toast-close]');
        if (close) {
            close.addEventListener('click', () => dismissToast(node));
        }
        requestAnimationFrame(() => node.classList.add('is-visible'));
        start();
    }

    function showToast(text, options = {}) {
        const tone = TONES[options.tone] || 'info';
        const node = document.createElement('div');
        node.className = `app-toast is-${tone}`;
        node.setAttribute('role', tone === 'danger' || tone === 'warning' ? 'alert' : 'status');
        node.dataset.toastTimeout = String(options.timeout || (tone === 'danger' ? 7000 : 4500));
        const span = document.createElement('span');
        span.className = 'app-toast-text';
        span.textContent = text;
        node.appendChild(span);
        if (options.action) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'app-toast-action';
            button.textContent = options.action.label;
            button.addEventListener('click', () => {
                dismissToast(node);
                options.action.onClick();
            });
            node.appendChild(button);
        }
        toastRegion().appendChild(node);
        armToast(node);
        return node;
    }

    function adoptToast(node) {
        const region = toastRegion();
        const imported = document.importNode(node, true);
        region.appendChild(imported);
        armToast(imported);
    }

    global.AppToast = { show: showToast, dismiss: dismissToast, adopt: adoptToast };

    // ---------------------------------------------------------------------
    // 2. Wspólne potwierdzenie
    // ---------------------------------------------------------------------
    let confirmState = null;

    function confirmDialog() {
        return document.getElementById('app-confirm');
    }

    function closeConfirm(accepted) {
        const dialog = confirmDialog();
        if (!dialog || !confirmState) {
            return;
        }
        const { form, submitter } = confirmState;
        confirmState = null;
        const finish = () => {
            dialog.classList.remove('is-closing');
            if (dialog.open) {
                dialog.close();
            }
            if (accepted) {
                form.dataset.confirmed = '1';
                // Pole potwierdzenia wymagane przez serwer (np. usunięcie produktu).
                form.querySelectorAll('input[data-confirm-flag]').forEach((input) => { input.value = '1'; });
                if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit(submitter && submitter.form === form ? submitter : undefined);
                } else {
                    form.submit();
                }
            } else if (submitter && typeof submitter.focus === 'function') {
                submitter.focus();
            }
        };
        if (reduceMotion() || !dialog.open) {
            finish();
            return;
        }
        dialog.classList.add('is-closing');
        let done = false;
        const once = () => {
            if (!done) {
                done = true;
                finish();
            }
        };
        dialog.addEventListener('transitionend', once, { once: true });
        setTimeout(once, 320);
    }

    function openConfirm(form, submitter) {
        const dialog = confirmDialog();
        if (!dialog || typeof dialog.showModal !== 'function') {
            // Stara przeglądarka: zwykłe pytanie zamiast arkusza.
            return global.confirm(form.dataset.confirm);
        }
        confirmState = { form, submitter };
        dialog.querySelector('[data-confirm-title]').textContent = form.dataset.confirm;
        const detail = dialog.querySelector('[data-confirm-detail]');
        detail.textContent = form.dataset.confirmDetail || '';
        detail.hidden = !form.dataset.confirmDetail;
        dialog.querySelector('[data-confirm-accept]').textContent = form.dataset.confirmAction || 'Usuń';

        // Na komputerze okno "wyrasta" z przycisku, który je otworzył.
        // Okno stoi na środku ekranu, więc przesunięcie przycisku względem środka
        // ekranu jest tym samym przesunięciem względem środka okna.
        const source = submitter || form;
        const rect = source.getBoundingClientRect();
        const visible = rect.width > 0 || rect.height > 0;
        const dx = visible ? Math.round(rect.left + rect.width / 2 - global.innerWidth / 2) : 0;
        const dy = visible ? Math.round(rect.top + rect.height / 2 - global.innerHeight / 2) : 0;
        dialog.style.setProperty('--confirm-dx', `${dx}px`);
        dialog.style.setProperty('--confirm-dy', `${dy}px`);
        dialog.classList.remove('is-closing');
        dialog.showModal();
        // Bezpieczny wybór domyślnie: fokus na "Anuluj".
        dialog.querySelector('[data-confirm-cancel]').focus();
        return null;
    }

    document.addEventListener('DOMContentLoaded', () => {
        const dialog = confirmDialog();
        if (!dialog) {
            return;
        }
        dialog.querySelector('[data-confirm-accept]').addEventListener('click', () => closeConfirm(true));
        dialog.querySelector('[data-confirm-cancel]').addEventListener('click', () => closeConfirm(false));
        dialog.addEventListener('cancel', (event) => {
            event.preventDefault();
            closeConfirm(false);
        });
        // Dotknięcie przyciemnionego tła zamyka arkusz.
        dialog.addEventListener('click', (event) => {
            if (event.target === dialog) {
                closeConfirm(false);
            }
        });
    });

    // ---------------------------------------------------------------------
    // 3. Formularze w tle
    // ---------------------------------------------------------------------
    const parser = new DOMParser();
    const optimistic = {};
    const FLASH_CLASSES = ['is-stock-changed'];

    function currentUrl() {
        return global.location.pathname + global.location.search;
    }

    function captureState(regions) {
        const state = { open: new Map(), shown: new Map(), focusId: null };
        regions.forEach((region) => {
            region.querySelectorAll('details[id]').forEach((node) => state.open.set(node.id, node.open));
            if (region.matches('details[id]')) {
                state.open.set(region.id, region.open);
            }
            region.querySelectorAll('.collapse[id]').forEach((node) => state.shown.set(node.id, node.classList.contains('show')));
        });
        const active = document.activeElement;
        if (active && active.id && regions.some((region) => region.contains(active))) {
            state.focusId = active.id;
        }
        return state;
    }

    function applyState(node, state) {
        const details = node.matches('details[id]') ? [node, ...node.querySelectorAll('details[id]')] : node.querySelectorAll('details[id]');
        details.forEach((element) => {
            if (state.open.has(element.id)) {
                element.open = state.open.get(element.id);
            }
        });
        node.querySelectorAll('.collapse[id]').forEach((element) => {
            if (!state.shown.has(element.id)) {
                return;
            }
            const shown = state.shown.get(element.id);
            element.classList.toggle('show', shown);
            document.querySelectorAll(`[data-bs-target="#${CSS.escape(element.id)}"]`).forEach((toggle) => {
                toggle.setAttribute('aria-expanded', String(shown));
                toggle.classList.toggle('collapsed', !shown);
            });
        });
    }

    function swapRegions(doc, selector) {
        const live = [...document.querySelectorAll(selector)];
        const state = captureState(live);
        const swapped = [];
        live.forEach((element) => {
            const key = element.id || element.dataset.partialKey;
            if (!key) {
                return;
            }
            const replacement = element.id
                ? doc.getElementById(element.id)
                : doc.querySelector(`[data-partial-key="${CSS.escape(key)}"]`);
            if (!replacement) {
                element.remove();
                return;
            }
            const fresh = document.importNode(replacement, true);
            applyState(fresh, state);
            // Podświetlenie zmiany (np. nowy stan w spiżarni) wygasa płynnie
            // na nowym elemencie zamiast zniknąć razem ze starym.
            const flashing = FLASH_CLASSES.filter((name) => element.classList.contains(name));
            fresh.classList.add(...flashing);
            element.replaceWith(fresh);
            if (flashing.length) {
                requestAnimationFrame(() => requestAnimationFrame(() => fresh.classList.remove(...flashing)));
            }
            swapped.push(fresh);
        });
        if (state.focusId) {
            const focusTarget = document.getElementById(state.focusId);
            if (focusTarget && document.activeElement !== focusTarget) {
                focusTarget.focus({ preventScroll: true });
            }
        }
        return swapped;
    }

    function showMessagesFrom(doc) {
        doc.querySelectorAll('#app-toasts .app-toast').forEach(adoptToast);
        doc.querySelectorAll('main .alert.app-message').forEach((alert) => {
            const tone = ['success', 'info', 'warning', 'danger'].find((name) => alert.classList.contains(`alert-${name}`)) || 'info';
            const text = alert.textContent.replace(/\s+/g, ' ').trim();
            if (text) {
                showToast(text, { tone });
            }
        });
    }

    // --- Spójność przestrzenna przy podmianie --------------------------------
    // Serwer może ułożyć fragment inaczej (odhaczona pozycja listy schodzi na
    // koniec). Strona nie może wtedy "skoczyć": jeden element zostaje
    // przypięty w tym samym miejscu ekranu, a elementy oznaczone data-flip,
    // które zmieniły miejsce, dojeżdżają do nowego zamiast teleportować się.

    function headerBottom() {
        const bar = document.querySelector('.app-nav');
        return bar ? bar.getBoundingClientRect().bottom : 0;
    }

    function pickAnchor(live, doc, acted) {
        const top = headerBottom();
        const visible = (element) => {
            const rect = element.getBoundingClientRect();
            return rect.height > 0 && rect.bottom > top && rect.top < global.innerHeight;
        };
        const survives = (element) => Boolean(doc.getElementById(element.id));
        // 1. Element, na którym wykonano akcję, jeśli zostaje i nie zmienia miejsca.
        const own = acted ? acted.closest('[id]') : null;
        if (own && live.some((region) => region.contains(own)) && !own.hasAttribute('data-flip')
            && survives(own) && visible(own)) {
            return own;
        }
        // 2. Najbliższy widoczny element nad nim (luka po nim domyka się od dołu),
        //    a gdy go nie ma - pod nim.
        const candidates = live.flatMap((region) => [region, ...region.querySelectorAll('[id]')])
            .filter((element) => element.id && survives(element) && visible(element)
                && !(own && (own.contains(element) || element.contains(own))));
        if (!candidates.length) {
            return null;
        }
        if (!own) {
            return candidates[0];
        }
        const ownTop = own.getBoundingClientRect().top;
        const above = candidates.filter((element) => element.getBoundingClientRect().bottom <= ownTop + 1);
        if (above.length) {
            return above.reduce((best, element) => (
                element.getBoundingClientRect().bottom > best.getBoundingClientRect().bottom ? element : best
            ));
        }
        return candidates[0];
    }

    function applyDocument(doc, selector, acted = null) {
        const live = [...document.querySelectorAll(selector)];
        const anchor = pickAnchor(live, doc, acted);
        const anchorId = anchor ? anchor.id : null;
        const anchorTop = anchor ? anchor.getBoundingClientRect().top : 0;
        const before = new Map();
        live.forEach((region) => region.querySelectorAll('[data-flip][id]').forEach((element) => {
            before.set(element.id, element.getBoundingClientRect().top);
        }));

        const swapped = swapRegions(doc, selector);

        if (anchorId) {
            const moved = document.getElementById(anchorId);
            if (moved) {
                const delta = moved.getBoundingClientRect().top - anchorTop;
                if (Math.abs(delta) > 0.5) {
                    global.scrollBy({ top: delta, behavior: 'instant' });
                }
            }
        }
        if (!reduceMotion()) {
            swapped.forEach((region) => region.querySelectorAll('[data-flip][id]').forEach((element) => {
                if (!before.has(element.id)) {
                    element.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 200, easing: 'ease-out' });
                    return;
                }
                const dy = before.get(element.id) - element.getBoundingClientRect().top;
                if (Math.abs(dy) > 1) {
                    element.animate(
                        [{ transform: `translateY(${dy}px)` }, { transform: 'translateY(0)' }],
                        { duration: 380, easing: 'cubic-bezier(0.32, 0.72, 0, 1)' },
                    );
                }
            }));
        }

        showMessagesFrom(doc);
        document.dispatchEvent(new CustomEvent('partial:swapped', { detail: { regions: swapped } }));
        return swapped;
    }

    function partialSelector(form) {
        const value = form.dataset.partial;
        if (value !== 'auto') {
            return value;
        }
        const page = document.querySelector('[data-partial-default]');
        return page ? page.dataset.partialDefault : '';
    }

    async function submitPartial(form, submitter, selector) {
        let body;
        try {
            body = submitter ? new FormData(form, submitter) : new FormData(form);
        } catch (error) {
            body = new FormData(form);
            if (submitter && submitter.name) {
                body.append(submitter.name, submitter.value);
            }
        }
        if (!body.has('next')) {
            body.append('next', currentUrl());
        }
        const revert = form.dataset.optimistic && optimistic[form.dataset.optimistic]
            ? optimistic[form.dataset.optimistic](form, submitter)
            : null;

        form.dataset.submitting = '1';
        form.setAttribute('aria-busy', 'true');
        const busyTimer = submitter ? setTimeout(() => submitter.classList.add('is-busy'), 150) : null;
        try {
            const response = await fetch(form.action, {
                method: 'POST',
                body,
                credentials: 'same-origin',
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
            });
            const landed = new URL(response.url);
            if (landed.pathname !== global.location.pathname) {
                // Serwer odesłał gdzie indziej (np. do logowania albo lista
                // została zakończona) - wtedy zwykłe przejście.
                global.location.assign(response.url);
                return;
            }
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            const doc = parser.parseFromString(await response.text(), 'text/html');
            applyDocument(doc, selector, submitter || form);
            const failed = Boolean(doc.querySelector('main .alert-danger.app-message'));
            if (!failed) {
                afterSuccess(form);
            }
        } catch (error) {
            if (typeof revert === 'function') {
                revert();
            }
            showToast('Nie udało się zapisać - sprawdź połączenie z domowym serwerem i spróbuj ponownie.', { tone: 'danger' });
        } finally {
            clearTimeout(busyTimer);
            if (submitter) {
                submitter.classList.remove('is-busy');
            }
            delete form.dataset.submitting;
            form.removeAttribute('aria-busy');
        }
    }

    function afterSuccess(form) {
        // Formularz dodawania czyści się i wraca do pierwszego pola -
        // następny produkt można wpisywać od razu.
        if (form.hasAttribute('data-partial-reset') && form.isConnected) {
            form.reset();
            const first = form.querySelector('input:not([type="hidden"]), select, textarea');
            if (first) {
                first.focus({ preventScroll: true });
            }
        }
        // Zapisana edycja zwija swój panel.
        if (form.dataset.partialClose) {
            document.querySelectorAll(form.dataset.partialClose).forEach((element) => {
                element.classList.remove('show');
                if (element.id) {
                    document.querySelectorAll(`[data-bs-target="#${CSS.escape(element.id)}"]`).forEach((toggle) => {
                        toggle.setAttribute('aria-expanded', 'false');
                        toggle.classList.add('collapsed');
                    });
                }
            });
        }
    }

    /** Pobiera bieżącą stronę jeszcze raz i podmienia wskazane fragmenty. */
    async function refresh(selector) {
        const response = await fetch(currentUrl(), { credentials: 'same-origin' });
        if (!response.ok || new URL(response.url).pathname !== global.location.pathname) {
            global.location.reload();
            return;
        }
        const doc = parser.parseFromString(await response.text(), 'text/html');
        applyDocument(doc, selector);
    }

    global.AppPartial = { optimistic, refresh, apply: applyDocument };

    // ---------------------------------------------------------------------
    // 4. To samo miejsce na stronie po zwykłym wysłaniu formularza
    // ---------------------------------------------------------------------
    // Usunięcie wydatku albo "Cofnij" przeładowuje listę. Formularz z polem
    // next wskazującym bieżący adres zapamiętuje, gdzie była strona, a po
    // powrocie przewija w to samo miejsce zamiast na górę.
    const SCROLL_KEY = 'app:scroll';

    function rememberScroll(form) {
        const next = form.querySelector('input[name="next"]');
        if (!next || next.value !== currentUrl()) {
            return;
        }
        try {
            global.sessionStorage.setItem(SCROLL_KEY, JSON.stringify({ url: currentUrl(), y: global.scrollY, at: Date.now() }));
        } catch (error) { /* tryb prywatny */ }
    }

    function restoreScroll() {
        let saved = null;
        try {
            saved = JSON.parse(global.sessionStorage.getItem(SCROLL_KEY) || 'null');
            global.sessionStorage.removeItem(SCROLL_KEY);
        } catch (error) {
            return;
        }
        if (!saved || saved.url !== currentUrl() || Date.now() - saved.at > 60000) {
            return;
        }
        // Kotwica elementu (#product-12) ma pierwszeństwo; #tab=... nie jest elementem.
        if (global.location.hash && !global.location.hash.startsWith('#tab=')) {
            return;
        }
        global.scrollTo({ top: saved.y, behavior: 'instant' });
    }

    document.addEventListener('DOMContentLoaded', restoreScroll);

    // Jedno miejsce obsługi wysyłania: najpierw potwierdzenie, potem wysyłka w tle.
    document.addEventListener('submit', (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement) || event.defaultPrevented) {
            return;
        }
        const submitter = event.submitter || null;

        if (form.dataset.confirm && form.dataset.confirmed !== '1') {
            event.preventDefault();
            const answer = openConfirm(form, submitter);
            if (answer === true) {
                // Tryb awaryjny (bez <dialog>): zwykłe potwierdzenie przeglądarki.
                form.dataset.confirmed = '1';
                form.querySelectorAll('input[data-confirm-flag]').forEach((input) => { input.value = '1'; });
                form.requestSubmit ? form.requestSubmit(submitter || undefined) : form.submit();
            }
            return;
        }
        delete form.dataset.confirmed;

        if (form.dataset.partial && global.fetch && global.DOMParser) {
            const selector = partialSelector(form);
            if (selector) {
                event.preventDefault();
                if (!form.dataset.submitting) {
                    submitPartial(form, submitter, selector);
                }
                return;
            }
        }
        rememberScroll(form);
    });

    // Dymki wyrenderowane przez serwer (np. "Usunięto · Cofnij" po przejściu na stronę).
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('#app-toasts .app-toast').forEach(armToast);
    });
})(window);
