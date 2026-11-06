/* Pływający dymek stopera na każdej stronie (poza samym Czasem).
 *
 * Stoi: małe szare kółko. Biegnie: pigułka w kolorze kategorii z ikoną
 * i czasem. Kliknięcie otwiera panel: czas, Stop, start albo przełączenie
 * kategorii i link do Czasu. Chwilowa kategoria (Instagram w trakcie Pracy)
 * ma jeszcze „Wróć do: Praca” - stop z powrotem do przerwanej czynności. Stan przychodzi z /czas/stan/ (JSON), a nie
 * z szablonu - strona Sport otwiera się z pamięci telefonu. Bez połączenia
 * albo bez logowania dymek się nie pokazuje.
 *
 * Ustawienia tego urządzenia (localStorage `czas-dymek`): po lewej stronie,
 * chowanie, gdy stoper stoi.
 */
(function () {
    'use strict';

    var root = document.querySelector('[data-time-bubble]');
    if (!root || !window.fetch) { return; }

    var url = root.getAttribute('data-url');
    var czasUrl = root.getAttribute('data-czas-url');
    var PREFS_KEY = 'czas-dymek';
    var state = null;
    var open = false;
    var busy = false;
    var error = '';
    var prefs = loadPrefs();

    function loadPrefs() {
        var defaults = { left: false, hideIdle: false };
        try {
            var saved = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}');
            return Object.assign(defaults, saved && typeof saved === 'object' ? saved : {});
        } catch (e) {
            return defaults;
        }
    }

    function savePrefs() {
        try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (e) { /* tryb prywatny */ }
    }

    function esc(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function pad(n) { return (n < 10 ? '0' : '') + n; }

    function elapsed() {
        var since = Date.parse(state.running.since);
        return Number.isNaN(since) ? 0 : Math.max(0, Math.floor((Date.now() - since) / 1000));
    }

    /** Panel: 1:05:12. Pigułka: „12 min”, od godziny „1:05”. */
    function clock(seconds, withSeconds) {
        var h = Math.floor(seconds / 3600);
        var m = Math.floor(seconds % 3600 / 60);
        if (!withSeconds && !h) { return m + ' min'; }
        return h + ':' + pad(m) + (withSeconds ? ':' + pad(seconds % 60) : '');
    }

    function spoken(seconds) {
        var h = Math.floor(seconds / 3600);
        var m = Math.floor(seconds % 3600 / 60);
        return (h ? h + ' h ' : '') + m + ' min';
    }

    function panelHtml() {
        var running = state.running;
        var head = running
            ? '<div class="tb-head-label">Teraz, od ' + esc(new Date(running.since).toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit' })) + '</div>' +
              '<div class="tb-head-name">' + esc(running.name) + (running.note ? ' · ' + esc(running.note) : '') + '</div>'
            : '<div class="tb-head-label">Stoper stoi</div><div class="tb-head-name">Czym się zajmujesz?</div>';
        var cats = state.categories.map(function (c) {
            var active = running && running.category === c.id;
            return '<button type="button" class="tb-cat' + (active ? ' is-running' : '') + '" data-tb-start="' + c.id + '"' +
                ' style="--tl-color: var(--cat-' + c.color + ')"' + (active ? ' aria-pressed="true"' : '') + (busy ? ' disabled' : '') + '>' +
                '<i class="bi ' + esc(c.icon) + '" aria-hidden="true"></i>' + esc(c.name) + '</button>';
        }).join('');
        return '<div class="tb-panel" id="tb-panel" role="dialog" aria-label="Stoper">' +
            '<div class="tb-head"><span class="tb-head-icon"><i class="bi ' + (running ? esc(running.icon) : 'bi-pause-fill') + '" aria-hidden="true"></i></span>' +
            '<div class="tb-head-main">' + head + '</div>' +
            (running ? '<div class="tb-elapsed" data-tb-elapsed>' + clock(elapsed(), true) + '</div>' : '') + '</div>' +
            '<div class="tb-actions">' +
            (running && running.resumeTo ? '<button type="button" class="btn btn-sm btn-primary" data-tb-resume' + (busy ? ' disabled' : '') + '>' +
                '<i class="bi ' + esc(running.resumeTo.icon) + '" aria-hidden="true"></i> Wróć do: ' + esc(running.resumeTo.name) + '</button>' : '') +
            (running ? '<button type="button" class="btn btn-sm btn-outline-secondary" data-tb-stop' + (busy ? ' disabled' : '') + '>' +
                '<i class="bi bi-stop-fill" aria-hidden="true"></i> Stop</button>' : '') +
            '<a class="btn btn-sm btn-outline-secondary" href="' + esc(czasUrl) + '"><i class="bi bi-hourglass-split" aria-hidden="true"></i> Otwórz Czas</a></div>' +
            '<p class="tb-subtitle">' + (running ? 'Przełącz na' : 'Start') + '</p>' +
            '<div class="tb-cats">' + cats + '</div>' +
            (error ? '<p class="tb-error" role="alert">' + esc(error) + '</p>' : '') +
            '<div class="tb-prefs">' +
            '<label><input type="checkbox" data-tb-pref="left"' + (prefs.left ? ' checked' : '') + '> Dymek po lewej stronie</label>' +
            '<label><input type="checkbox" data-tb-pref="hideIdle"' + (prefs.hideIdle ? ' checked' : '') + '> Chowaj dymek, gdy stoper stoi</label>' +
            '</div></div>';
    }

    function render() {
        if (!state) {
            root.hidden = true;
            document.body.classList.remove('has-time-bubble');
            return;
        }
        var running = state.running;
        root.hidden = !running && prefs.hideIdle && !open;
        document.body.classList.toggle('has-time-bubble', !root.hidden);
        root.classList.toggle('is-running', Boolean(running));
        root.classList.toggle('is-left', Boolean(prefs.left));
        root.style.setProperty('--tl-color', running ? 'var(--cat-' + running.color + ')' : 'transparent');
        var label = running ? 'Stoper: ' + running.name + ', ' + spoken(elapsed()) : 'Stoper stoi';
        var toggle = '<button type="button" class="tb-toggle" data-tb-toggle aria-expanded="' + open + '" aria-controls="tb-panel"' +
            ' aria-label="' + esc(label) + '. ' + (open ? 'Zamknij' : 'Pokaż opcje') + '">' +
            (running
                ? '<span class="tb-pulse" aria-hidden="true"></span><i class="bi ' + esc(running.icon) + '" aria-hidden="true"></i>' +
                  '<span class="tb-time" data-tb-time>' + clock(elapsed(), false) + '</span>'
                : '<i class="bi bi-stopwatch" aria-hidden="true"></i>') +
            '</button>';
        root.innerHTML = (open ? panelHtml() : '') + toggle;
    }

    function tick() {
        if (!state || !state.running) { return; }
        var seconds = elapsed();
        var time = root.querySelector('[data-tb-time]');
        if (time) { time.textContent = clock(seconds, false); }
        var full = root.querySelector('[data-tb-elapsed]');
        if (full) { full.textContent = clock(seconds, true); }
    }

    function load() {
        return fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' }, cache: 'no-store' })
            .then(function (response) {
                if (response.status === 401) { state = null; return null; }
                return response.ok ? response.json() : null;
            })
            .then(function (data) {
                if (data && data.ok) { state = data; }
                render();
            })
            .catch(function () {
                // Bez połączenia (np. Sport z pamięci telefonu) stary stan by kłamał.
                state = null;
                render();
            });
    }

    function send(payload) {
        if (busy || !state) { return; }
        busy = true;
        error = '';
        render();
        fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-CSRFToken': state.csrfToken },
            body: JSON.stringify(payload)
        })
            .then(function (response) { return response.json().catch(function () { return {}; }).then(function (data) { return [response, data]; }); })
            .then(function (result) {
                busy = false;
                if (result[0].ok && result[1].ok) {
                    state = result[1];
                    open = false;
                } else {
                    error = result[1].error || 'Nie udało się. Odśwież stronę i spróbuj jeszcze raz.';
                }
                render();
            })
            .catch(function () {
                busy = false;
                error = 'Brak połączenia z serwerem.';
                render();
            });
    }

    function close(focusToggle) {
        if (!open) { return; }
        open = false;
        error = '';
        render();
        if (focusToggle) {
            var toggle = root.querySelector('[data-tb-toggle]');
            if (toggle) { toggle.focus(); }
        }
    }

    root.addEventListener('click', function (event) {
        if (event.target.closest('[data-tb-toggle]')) {
            open = !open;
            error = '';
            render();
            if (open) { load(); }
            return;
        }
        if (event.target.closest('[data-tb-stop]')) { send({ action: 'stop' }); return; }
        if (event.target.closest('[data-tb-resume]')) { send({ action: 'stop', resume: true }); return; }
        var start = event.target.closest('[data-tb-start]');
        if (start) { send({ action: 'start', category: start.getAttribute('data-tb-start') }); }
    });

    root.addEventListener('change', function (event) {
        var pref = event.target.getAttribute('data-tb-pref');
        if (!pref) { return; }
        prefs[pref] = event.target.checked;
        savePrefs();
        render();
    });

    // Kliknięcie obok zamyka panel. Droga zdarzenia, a nie root.contains:
    // dymek przerysowuje się w trakcie kliknięcia i kliknięty przycisk
    // znika już z dokumentu.
    document.addEventListener('click', function (event) {
        if (open && event.composedPath().indexOf(root) === -1) { close(false); }
    });
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') { close(true); }
    });

    // Przyklejony pasek przycisków formularza: dymek staje nad nim.
    function lift() {
        var bar = document.querySelector('.u-form-actions');
        var height = 0;
        if (bar) {
            var position = getComputedStyle(bar).position;
            if (position === 'sticky' || position === 'fixed') { height = bar.offsetHeight; }
        }
        root.style.setProperty('--tb-lift', height + 'px');
    }

    // Powrót do karty albo strona z pamięci przeglądarki („Wstecz”): świeży stan.
    document.addEventListener('visibilitychange', function () {
        if (!document.hidden) { load(); }
    });
    window.addEventListener('pageshow', function (event) {
        if (event.persisted) { load(); }
    });
    window.addEventListener('resize', lift);

    lift();
    load();
    setInterval(tick, 1000);
})();
