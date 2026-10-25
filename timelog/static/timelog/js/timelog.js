// Harmonogram: licznik biegnącego stopera ([data-tl-since] = początek w ISO).
(function () {
    const counters = document.querySelectorAll('[data-tl-since]');
    if (!counters.length) return;

    function format(seconds) {
        const h = Math.floor(seconds / 3600);
        const m = Math.floor(seconds % 3600 / 60);
        const s = seconds % 60;
        return h + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
    }

    function tick() {
        const now = Date.now();
        counters.forEach((el) => {
            const since = Date.parse(el.dataset.tlSince);
            if (!Number.isNaN(since)) el.textContent = format(Math.max(0, Math.floor((now - since) / 1000)));
        });
    }

    tick();
    setInterval(tick, 1000);
    // Telefon wybudzony po godzinie: odśwież, żeby lista i pasek nie były sprzed godziny.
    let hiddenAt = null;
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) hiddenAt = Date.now();
        else if (hiddenAt && Date.now() - hiddenAt > 10 * 60 * 1000) location.reload();
    });
})();
