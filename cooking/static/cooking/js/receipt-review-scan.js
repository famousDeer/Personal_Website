/* Skan kodów na etapie „Sprawdź” - przed „Dodaj do spiżarni”.
 *
 * Aparat (barcode-camera.js) działa bez przerwy, opakowania skanuje się
 * w dowolnej kolejności. Serwer sam dobiera pozycję paragonu do kodu i pyta
 * tylko wtedy, gdy nie jest pewny. Przycisk skanu przy pozycji kieruje
 * następny kod właśnie do niej (np. żeby poprawić wcześniejszy skan).
 */
(function () {
    'use strict';

    const root = document.getElementById('receipt-review-scan');
    const form = document.querySelector('[data-review-form]');
    if (!root || !form || !window.BarcodeCamera) {
        return;
    }
    const select = (selector) => root.querySelector(selector);
    const toggle = select('[data-scan-toggle]');
    const toggleLabel = select('[data-scan-toggle-label]');
    const panel = select('[data-scan-panel]');
    const statusLine = select('[data-status]');
    const chooseBox = select('[data-choose]');
    const chooseTitle = select('[data-choose-title]');
    const chooseList = select('[data-choose-list]');
    const manualInput = select('#rs-manual-code');
    const csrfToken = form.querySelector('input[name="csrfmiddlewaretoken"]').value;
    const REPEAT_MS = 4000;

    const state = { busy: false, paused: false, lastCode: '', lastAt: 0, activeLine: null };

    const lineElement = (id) => form.querySelector(`[data-line-id="${id}"]`);

    function setStatus(text, tone = '') {
        statusLine.textContent = text;
        statusLine.dataset.tone = tone;
    }

    function setActiveLine(id) {
        form.querySelectorAll('.rc-line.is-active').forEach((line) => line.classList.remove('is-active'));
        state.activeLine = id;
        if (id) {
            lineElement(id)?.classList.add('is-active');
        }
    }

    async function post(body) {
        const response = await fetch(root.dataset.scanUrl, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken, Accept: 'application/json' },
            body: JSON.stringify(body),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.ok === false) {
            throw new Error(payload.error || 'Nie udało się zapisać kodu.');
        }
        return payload;
    }

    // Wynik skanu → wiersz pozycji: produkt, kod i „Do spiżarni”.
    function applyScan(result) {
        const line = lineElement(result.line.id);
        if (!line) {
            return;
        }
        const target = line.querySelector('[data-line-target]');
        target.value = result.target;
        target.readOnly = Boolean(result.locked);
        line.querySelector('[data-line-action]').value = 'add';
        line.querySelector('[data-line-badge]').textContent = 'Skan';
        line.querySelector('[data-line-code-value]').textContent = result.barcode;
        line.querySelector('[data-line-code-note]').textContent = result.locked
            ? '– produkt ze spiżarni'
            : '– nowy kod, nazwę możesz poprawić';
        line.querySelector('[data-line-code]').hidden = false;
        line.classList.remove('is-suggested', 'is-new', 'is-ignored');
        line.classList.add('is-known');
        if (!result.target) {
            target.focus();
        }
    }

    function hideChoice() {
        chooseBox.hidden = true;
        state.paused = false;
    }

    function showChoice(barcode, result) {
        state.paused = true;
        chooseTitle.textContent = result.product
            ? `„${result.product}” – która to pozycja z paragonu?`
            : `Kod ${barcode} – która to pozycja z paragonu?`;
        chooseList.replaceChildren(...result.candidates.map((candidate) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'btn btn-outline-primary';
            const name = document.createElement('span');
            name.textContent = candidate.name;
            const price = document.createElement('small');
            price.textContent = `${candidate.price.replace('.', ',')} zł`;
            button.append(name, price);
            button.addEventListener('click', async () => {
                try {
                    const chosen = await post({ barcode, line_id: candidate.id });
                    hideChoice();
                    handleResult(barcode, chosen);
                } catch (error) {
                    setStatus(error.message, 'error');
                }
            });
            return button;
        }));
        chooseBox.hidden = false;
        setStatus('Wskaż pozycję – potem skanuj dalej.');
    }

    function handleResult(barcode, result) {
        if (result.status === 'known' || result.status === 'new') {
            navigator.vibrate?.(60);
            applyScan(result);
            setActiveLine(null);
            if (result.status === 'known') {
                setStatus(`✓ ${result.line.name} → ${result.target} – skanuj następny`, 'ok');
            } else if (result.target) {
                setStatus(`✓ ${result.line.name}: nowy kod, produkt „${result.target}” – popraw nazwę, jeśli trzeba`, 'ok');
            } else {
                setStatus(`✓ ${result.line.name}: nowy kod – wpisz nazwę produktu przy pozycji`, 'ok');
            }
        } else if (result.status === 'choose') {
            showChoice(barcode, result);
        } else if (result.status === 'already') {
            setStatus(`Ten kod ma już pozycja „${result.line.name}”.`);
        } else {
            setStatus('Wszystkie pozycje mają już kody. Żeby zmienić skan, dotknij skanu przy pozycji.');
        }
    }

    async function onBarcode(raw) {
        const barcode = String(raw || '').replace(/\s/g, '');
        const now = Date.now();
        if (!barcode || state.busy || state.paused) {
            return;
        }
        if (barcode === state.lastCode && now - state.lastAt < REPEAT_MS) {
            return;
        }
        state.lastCode = barcode;
        state.lastAt = now;
        state.busy = true;
        setStatus(`Sprawdzam ${barcode}…`);
        try {
            handleResult(barcode, await post({ barcode, line_id: state.activeLine }));
        } catch (error) {
            setStatus(error.message, 'error');
        } finally {
            state.busy = false;
        }
    }

    const camera = window.BarcodeCamera.create({
        video: select('[data-video]'), frame: select('.rb-frame'), zxingUrl: root.dataset.zxingUrl, onCode: onBarcode,
    });

    function readyText() {
        const active = state.activeLine && lineElement(state.activeLine);
        return active ? `Zeskanuj kod pozycji „${active.dataset.lineName}”.` : 'Skieruj aparat na kod dowolnego produktu z paragonu.';
    }

    async function openPanel() {
        panel.hidden = false;
        root.classList.add('is-open');
        toggle.setAttribute('aria-expanded', 'true');
        toggleLabel.textContent = 'Zakończ skanowanie';
        if (camera.active) {
            setStatus(readyText());
            return;
        }
        setStatus('Uruchamiam aparat…');
        try {
            await camera.start();
            setStatus(readyText());
        } catch (error) {
            camera.stop();
            setStatus(error?.name === 'InsecureContextError'
                ? 'Aparat działa tylko przez HTTPS – wpisz kod ręcznie.'
                : error?.name === 'NotAllowedError'
                    ? 'Brak dostępu do aparatu – zezwól i spróbuj ponownie albo wpisz kod ręcznie.'
                    : 'Nie udało się uruchomić aparatu – wpisz kod ręcznie.', 'error');
        }
    }

    function closePanel() {
        camera.stop();
        hideChoice();
        setActiveLine(null);
        panel.hidden = true;
        root.classList.remove('is-open');
        toggle.setAttribute('aria-expanded', 'false');
        toggleLabel.textContent = 'Skanuj produkty';
    }

    toggle.addEventListener('click', () => (panel.hidden ? openPanel() : closePanel()));
    select('[data-choose-skip]').addEventListener('click', () => {
        hideChoice();
        setStatus('Pominięto. Skanuj następny produkt.');
    });
    select('[data-manual]').addEventListener('submit', (event) => {
        event.preventDefault();
        state.lastCode = '';
        onBarcode(manualInput.value);
        manualInput.value = '';
    });
    form.querySelectorAll('[data-line-scan]').forEach((button) => {
        button.hidden = false;
        button.addEventListener('click', () => {
            hideChoice();
            state.lastCode = '';
            setActiveLine(Number(button.closest('[data-line-id]').dataset.lineId));
            openPanel();
            root.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
    });
    form.querySelectorAll('[data-line-clear]').forEach((button) => {
        button.addEventListener('click', async () => {
            const line = button.closest('[data-line-id]');
            try {
                await post({ action: 'clear', line_id: Number(line.dataset.lineId) });
            } catch (error) {
                setStatus(error.message, 'error');
                return;
            }
            line.querySelector('[data-line-target]').readOnly = false;
            line.querySelector('[data-line-code]').hidden = true;
            line.querySelector('[data-line-badge]').textContent = 'Propozycja';
            state.lastCode = '';
        });
    });
    window.addEventListener('pagehide', () => camera.stop());
})();
