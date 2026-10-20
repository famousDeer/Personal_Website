/* „Rozpakuj zakupy” po zatwierdzeniu paragonu.
 *
 * Aparat działa bez przerwy. Każdy kod idzie na serwer, który sam wybiera
 * produkt (jedyny pozostały albo pasujący do nazwy z Open Food Facts).
 * Użytkownik dotyka ekranu tylko wtedy, gdy serwer nie jest pewny, i przy
 * nabiale - raz, wybierając datę z opakowania.
 */
(function () {
    'use strict';

    const root = document.getElementById('receipt-barcodes');
    if (!root) {
        return;
    }
    const select = (selector) => root.querySelector(selector);
    const video = select('[data-video]');
    const statusLine = select('[data-status]');
    const startButton = select('[data-start]');
    const chooseBox = select('[data-choose]');
    const chooseTitle = select('[data-choose-title]');
    const chooseHint = select('[data-choose-hint]');
    const chooseSkip = select('[data-choose-skip]');
    const chooseTexts = { title: chooseTitle.textContent, skip: chooseSkip.textContent };
    const chooseList = select('[data-choose-list]');
    const remainingList = select('[data-remaining]');
    const doneList = select('[data-done]');
    const remainingCount = select('[data-remaining-count]');
    const allDone = select('[data-all-done]');
    const dateBox = select('[data-date]');
    const dateProduct = select('[data-date-product]');
    const dateChips = select('[data-date-chips]');
    const dateInput = select('[data-date-input]');
    const quickDates = (root.dataset.quickDates || '').split('|').filter(Boolean);
    const csrfToken = select('[data-csrf] input[name="csrfmiddlewaretoken"]').value;
    const formats = ['ean_13', 'ean_8', 'upc_a', 'upc_e', 'code_128'];
    const REPEAT_MS = 4000;

    const state = { busy: false, paused: false, lastCode: '', lastAt: 0, stream: null, controls: null, timer: null, merge: null };

    function setStatus(text, tone = '') {
        statusLine.textContent = text;
        statusLine.dataset.tone = tone;
    }

    function feedback() {
        if (navigator.vibrate) {
            navigator.vibrate(60);
        }
    }

    function itemLabel(item) {
        return `${item.name}${item.needs_code ? ' 🔢' : ''}${item.needs_date ? ' 📅' : ''}`;
    }

    function renderRemaining(items, linkedName) {
        remainingList.replaceChildren(...items.map((item) => {
            const li = document.createElement('li');
            li.dataset.productId = item.id;
            li.textContent = itemLabel(item);
            if (item.needs_date) {
                li.tabIndex = 0;
                li.setAttribute('role', 'button');
                li.classList.add('is-clickable');
                li.addEventListener('click', () => askDate(item.id, item.name));
            }
            return li;
        }));
        if (linkedName) {
            const li = document.createElement('li');
            li.textContent = linkedName;
            doneList.prepend(li);
        }
        remainingCount.textContent = String(items.length);
        allDone.hidden = items.length > 0;
        if (!items.length) {
            stopCamera();
            setStatus('Gotowe – wszystko rozpakowane.', 'ok');
        }
    }

    function dateLabel(isoDate) {
        const day = new Date(`${isoDate}T12:00:00`);
        return day.toLocaleDateString('pl-PL', { weekday: 'short', day: 'numeric', month: 'numeric' });
    }

    function askDate(productId, name) {
        state.paused = true;
        state.dateProductId = productId;
        dateProduct.textContent = name;
        dateInput.value = '';
        dateChips.replaceChildren(...quickDates.map((isoDate) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'btn btn-outline-success';
            button.textContent = dateLabel(isoDate);
            button.addEventListener('click', () => saveDate(isoDate));
            return button;
        }));
        chooseBox.hidden = true;
        dateBox.hidden = false;
        setStatus(`${name}: dotknij daty z opakowania.`);
        dateBox.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    async function saveDate(isoDate) {
        if (!isoDate) {
            dateInput.reportValidity();
            return;
        }
        try {
            const result = await post({ action: 'date', product_id: state.dateProductId, expires_on: isoDate });
            dateBox.hidden = true;
            state.paused = false;
            feedback();
            setStatus(`✓ ${result.product}: ważne do ${dateLabel(isoDate)} – skanuj następny`, 'ok');
            renderRemaining(result.remaining);
        } catch (error) {
            setStatus(error.message, 'error');
        }
    }

    function send(barcode, productId = null, merge = false) {
        return post({ barcode, product_id: productId, merge });
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

    function showChoice(barcode, result, merge = false) {
        state.paused = true;
        state.merge = merge ? barcode : null;
        chooseTitle.textContent = merge ? `Ten kod ma już „${result.product}”` : chooseTexts.title;
        chooseSkip.textContent = merge ? 'Żaden – to inny produkt' : chooseTexts.skip;
        if (merge) {
            chooseHint.textContent = 'Który produkt z paragonu to ten sam? Połączę je w jeden (stan, ceny i historia).';
        } else {
            chooseHint.textContent = result.catalog_name
                ? `Kod ${barcode} – w katalogu: „${result.catalog_name}”.`
                : `Kod ${barcode}.`;
        }
        chooseList.replaceChildren(...(result.candidates || []).map((candidate) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'btn btn-outline-primary';
            button.textContent = candidate.name;
            button.addEventListener('click', () => choose(barcode, candidate.id, merge));
            return button;
        }));
        chooseBox.hidden = false;
        setStatus(merge ? 'Wskaż ten sam produkt z paragonu – albo „Żaden”.' : 'Wskaż produkt – potem skanuj dalej.');
    }

    function hideChoice() {
        chooseBox.hidden = true;
        state.paused = false;
        state.merge = null;
    }

    async function choose(barcode, productId, merge = false) {
        try {
            const result = await send(barcode, productId, merge);
            hideChoice();
            handleResult(barcode, result);
        } catch (error) {
            setStatus(error.message, 'error');
        }
    }

    function handleResult(barcode, result) {
        if (result.status === 'linked') {
            feedback();
            renderRemaining(result.remaining, result.product);
            if (result.needs_date) {
                askDate(result.product_id, result.product);
            } else {
                setStatus(`✓ ${result.product} – skanuj następny`, 'ok');
            }
        } else if (result.status === 'merged') {
            feedback();
            renderRemaining(result.remaining, `${result.merged} → ${result.product}`);
            if (result.needs_date) {
                askDate(result.product_id, result.product);
            } else {
                setStatus(`✓ Połączono „${result.merged}” z „${result.product}” – skanuj następny`, 'ok');
            }
        } else if (result.status === 'merge') {
            showChoice(barcode, result, true);
        } else if (result.status === 'date') {
            feedback();
            askDate(result.product_id, result.product);
        } else if (result.status === 'known') {
            setStatus(`Ten kod już jest w spiżarni: ${result.product}.`);
        } else if (result.candidates && result.candidates.length) {
            showChoice(barcode, result);
        } else {
            setStatus('Wszystkie produkty z paragonu mają już kody.');
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
            handleResult(barcode, await send(barcode));
        } catch (error) {
            setStatus(error.message, 'error');
        } finally {
            state.busy = false;
        }
    }

    function stopCamera() {
        window.clearTimeout(state.timer);
        state.controls?.stop();
        state.controls = null;
        state.stream?.getTracks().forEach((track) => track.stop());
        state.stream = null;
    }

    async function nativeDetector() {
        if (!('BarcodeDetector' in window)) {
            return null;
        }
        try {
            const supported = await window.BarcodeDetector.getSupportedFormats();
            const usable = formats.filter((format) => supported.includes(format));
            return usable.length ? new window.BarcodeDetector({ formats: usable }) : null;
        } catch (error) {
            return null;
        }
    }

    function loadZxing() {
        if (window.ZXingBrowser) {
            return Promise.resolve(window.ZXingBrowser);
        }
        return new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = root.dataset.zxingUrl;
            script.onload = () => (window.ZXingBrowser ? resolve(window.ZXingBrowser) : reject(new Error('Brak czytnika kodów.')));
            script.onerror = () => reject(new Error('Nie udało się wczytać czytnika kodów.'));
            document.head.appendChild(script);
        });
    }

    const constraints = {
        audio: false,
        video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } },
    };

    async function startCamera() {
        startButton.hidden = true;
        if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
            setStatus('Aparat działa tylko przez HTTPS – wpisz kody ręcznie.', 'error');
            return;
        }
        setStatus('Uruchamiam aparat…');
        try {
            const detector = await nativeDetector();
            if (detector) {
                state.stream = await navigator.mediaDevices.getUserMedia(constraints);
                video.srcObject = state.stream;
                await video.play();
                const tick = async () => {
                    try {
                        if (!state.busy && !state.paused && video.readyState >= 2) {
                            const found = (await detector.detect(video)).find((item) => item.rawValue);
                            if (found) {
                                await onBarcode(found.rawValue);
                            }
                        }
                    } catch (error) {
                        // Pojedyncza nieczytelna klatka to norma, gdy opakowanie się rusza.
                    }
                    state.timer = window.setTimeout(tick, 150);
                };
                tick();
            } else {
                const ZXingBrowser = await loadZxing();
                const reader = new ZXingBrowser.BrowserMultiFormatReader();
                state.controls = await reader.decodeFromConstraints(constraints, video, (result) => {
                    if (result) {
                        onBarcode(typeof result.getText === 'function' ? result.getText() : result.text);
                    }
                });
            }
            setStatus('Skieruj aparat na kod pierwszego produktu.');
        } catch (error) {
            stopCamera();
            startButton.hidden = false;
            setStatus(error?.name === 'NotAllowedError'
                ? 'Brak dostępu do aparatu – zezwól i spróbuj ponownie albo wpisz kod ręcznie.'
                : 'Nie udało się uruchomić aparatu. Spróbuj ponownie albo wpisz kod ręcznie.', 'error');
        }
    }

    startButton.addEventListener('click', startCamera);
    select('[data-date-ok]').addEventListener('click', () => saveDate(dateInput.value));
    remainingList.querySelectorAll('[data-needs-date="1"]').forEach((li) => {
        li.tabIndex = 0;
        li.setAttribute('role', 'button');
        li.classList.add('is-clickable');
        li.addEventListener('click', () => askDate(Number(li.dataset.productId), li.dataset.name || li.textContent.trim()));
    });
    chooseSkip.addEventListener('click', async () => {
        const barcode = state.merge;
        hideChoice();
        if (barcode) {
            // „Żaden”: produkt ze spiżarni zostaje osobno; serwer najwyżej poprosi o termin.
            try {
                handleResult(barcode, await send(barcode, null, true));
            } catch (error) {
                setStatus(error.message, 'error');
            }
            return;
        }
        setStatus('Pominięto. Skanuj następny produkt.');
    });
    select('[data-manual]').addEventListener('submit', (event) => {
        event.preventDefault();
        const input = select('#rb-manual-code');
        state.lastCode = '';
        onBarcode(input.value);
        input.value = '';
    });
    window.addEventListener('pagehide', stopCamera);

    remainingList.querySelectorAll('li').forEach((li) => {
        li.dataset.name = li.textContent.trim();
        li.textContent = `${li.textContent.trim()}${li.dataset.needsCode ? ' 🔢' : ''}${li.dataset.needsDate ? ' 📅' : ''}`;
    });
    if (remainingList.querySelector('[data-needs-code="1"]')) {
        startCamera();
    } else if (remainingList.children.length) {
        startButton.hidden = false;
        setStatus('Dotknij produktu na liście, żeby wpisać termin – albo zeskanuj opakowanie.');
    } else {
        setStatus('Wszystkie produkty z tego paragonu mają już kody.', 'ok');
    }
})();
