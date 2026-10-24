/* Strona aktywności: dane w telefonie, kolejka zmian offline, rysowanie.
 *
 * Treningi z serwera (snapshot) i niewysłane zmiany (kolejka) leżą w IndexedDB,
 * więc strona pokazuje wykresy i przyjmuje nowe treningi bez połączenia.
 * Widok = snapshot + kolejka nałożona na wierzch. Po udanej synchronizacji
 * wysłane zmiany znikają z kolejki, a snapshot przychodzi świeży z serwera.
 * Każda zmiana ma `uid` treningu, więc ponowna wysyłka niczego nie dubluje.
 * Statystyki liczy activity-stats.js (window.ActivityStats).
 */
(function () {
    'use strict';

    var S = window.ActivityStats;
    var configNode = document.getElementById('activity-config');
    var root = document.querySelector('[data-act-app]');
    if (!S || !configNode || !root) { return; }
    var config = JSON.parse(configNode.textContent);

    var STORE_KEY = 'state';
    var TAB_KEY = 'aktywnosc-zakladka';
    var SYNC_INTERVAL_MS = 5 * 60 * 1000;
    var KIND_INDEX = { swim: 0, run: 1, gym: 2, other: 3 };

    function $(selector, scope) { return (scope || document).querySelector(selector); }
    function $$(selector, scope) { return Array.prototype.slice.call((scope || document).querySelectorAll(selector)); }

    function esc(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    /** UUID v4 - crypto.randomUUID działa tylko pod HTTPS, getRandomValues wszędzie. */
    function newUid() {
        if (window.crypto && crypto.randomUUID) {
            try { return crypto.randomUUID(); } catch (e) { /* zwykłe http */ }
        }
        var bytes = new Uint8Array(16);
        crypto.getRandomValues(bytes);
        bytes[6] = (bytes[6] & 0x0f) | 0x40;
        bytes[8] = (bytes[8] & 0x3f) | 0x80;
        var hex = Array.prototype.map.call(bytes, function (b) { return (b + 0x100).toString(16).slice(1); }).join('');
        return hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' + hex.slice(16, 20) + '-' + hex.slice(20);
    }

    function kindColor(kind) {
        var palette = window.AppCharts ? AppCharts.palette().categories : ['#2a78d6', '#eb6834', '#1baf7a', '#eda100'];
        return palette[KIND_INDEX[kind] || 0];
    }

    // --- Pamięć w telefonie (IndexedDB, w razie braku - tylko w pamięci) ---
    var store = (function () {
        var memory = {};
        var dbPromise = null;

        function open() {
            if (!dbPromise) {
                dbPromise = new Promise(function (resolve) {
                    try {
                        var request = indexedDB.open('aktywnosc', 1);
                        request.onupgradeneeded = function () { request.result.createObjectStore('kv'); };
                        request.onsuccess = function () { resolve(request.result); };
                        request.onerror = function () { resolve(null); };
                        request.onblocked = function () { resolve(null); };
                    } catch (e) {
                        resolve(null);   // tryb prywatny / brak IndexedDB
                    }
                });
            }
            return dbPromise;
        }

        return {
            get: function (key) {
                return open().then(function (db) {
                    if (!db) { return memory[key]; }
                    return new Promise(function (resolve) {
                        try {
                            var request = db.transaction('kv').objectStore('kv').get(key);
                            request.onsuccess = function () { resolve(request.result); };
                            request.onerror = function () { resolve(memory[key]); };
                        } catch (e) {
                            resolve(memory[key]);
                        }
                    });
                });
            },
            set: function (key, value) {
                memory[key] = value;
                return open().then(function (db) {
                    if (!db) { return null; }
                    return new Promise(function (resolve) {
                        try {
                            var tx = db.transaction('kv', 'readwrite');
                            tx.objectStore('kv').put(value, key);
                            tx.oncomplete = resolve;
                            tx.onerror = resolve;
                            tx.onabort = resolve;
                        } catch (e) {
                            resolve();
                        }
                    });
                });
            }
        };
    })();

    // --- Stan ---------------------------------------------------------------
    var state = {
        snapshot: null,
        queue: [],
        user: null,
        csrf: null,
        syncedAt: null,
        syncing: false,
        syncAgain: false,
        offline: false,
        authNeeded: false,
        errors: [],
        tab: null,
        exercise: null,
        listLimit: 15
    };

    function persist() {
        return store.set(STORE_KEY, {
            snapshot: state.snapshot,
            queue: state.queue,
            user: state.user,
            csrf: state.csrf,
            syncedAt: state.syncedAt
        });
    }

    function byNewest(a, b) {
        if (a.date !== b.date) { return a.date < b.date ? 1 : -1; }
        return (a.time || '') < (b.time || '') ? 1 : -1;
    }

    function serverWorkouts() {
        return (state.snapshot && state.snapshot.workouts) || [];
    }

    /** Treningi z serwera z nałożonymi niewysłanymi zmianami. */
    function workouts() {
        var map = {};
        serverWorkouts().forEach(function (w) { map[w.uid] = w; });
        state.queue.forEach(function (op) {
            if (op.type === 'save') {
                var previous = map[op.workout.uid];
                map[op.workout.uid] = Object.assign({}, op.workout, {
                    source: previous ? previous.source : 'manual',
                    pending: true
                });
            } else if (op.type === 'delete') {
                delete map[op.uid];
            }
        });
        return Object.keys(map).map(function (uid) { return map[uid]; }).sort(byNewest);
    }

    function weeklyGoal() {
        var goal = state.snapshot && state.snapshot.settings ? state.snapshot.settings.weeklyGoal : 3;
        state.queue.forEach(function (op) { if (op.type === 'goal') { goal = op.value; } });
        return goal || 3;
    }

    function garminStatus() {
        return (state.snapshot && state.snapshot.settings && state.snapshot.settings.garmin) || {};
    }

    /** Dopisuje zmianę do kolejki; starsze zmiany tego samego treningu są już nieaktualne. */
    function enqueue(op) {
        op.id = newUid();
        state.queue = state.queue.filter(function (queued) {
            if (op.type === 'goal') { return queued.type !== 'goal'; }
            var uid = op.type === 'save' ? op.workout.uid : op.uid;
            var queuedUid = queued.type === 'save' ? queued.workout.uid : queued.uid;
            return queued.type === 'goal' || queuedUid !== uid;
        });
        state.queue.push(op);
        persist();
        render();
        scheduleSync(300);
    }

    // --- Synchronizacja ------------------------------------------------------
    function cookie(name) {
        var match = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
        return match ? decodeURIComponent(match[1]) : '';
    }

    function csrfToken() {
        var input = $('input[name="csrfmiddlewaretoken"]');
        return state.csrf || cookie('csrftoken') || (input ? input.value : '');
    }

    function applyPayload(data) {
        if (state.user && data.user && state.user !== data.user) {
            // Inna osoba zalogowała się na tym telefonie - jej dane od zera.
            state.queue = [];
        }
        state.user = data.user;
        state.csrf = data.csrf_token || state.csrf;
        state.snapshot = data.snapshot;
        state.syncedAt = new Date().toISOString();
        state.offline = false;
        state.authNeeded = false;
    }

    var syncTimer = null;
    function scheduleSync(delay) {
        clearTimeout(syncTimer);
        syncTimer = setTimeout(sync, delay || 0);
    }

    function request(url, options) {
        return fetch(url, Object.assign({ credentials: 'same-origin', cache: 'no-store' }, options || {}))
            .then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (data) {
                    return { status: response.status, ok: response.ok && data.ok !== false, data: data };
                });
            });
    }

    function postJson(url, body) {
        return request(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
            body: JSON.stringify(body || {})
        });
    }

    function sync(retried) {
        if (state.syncing) {
            state.syncAgain = true;
            return Promise.resolve();
        }
        state.syncing = true;
        renderStatus();
        var ops = state.queue.slice();
        var call = ops.length ? postJson(config.syncUrl, { ops: ops }) : request(config.snapshotUrl);
        return call.then(function (result) {
            if (result.status === 401) {
                state.authNeeded = true;
                state.offline = false;
                return;
            }
            if (result.status === 403 && !retried) {
                // Nieaktualny token CSRF (np. po ponownym zalogowaniu): pobierz świeży i spróbuj raz jeszcze.
                state.csrf = null;
                return request(config.snapshotUrl).then(function (fresh) {
                    if (fresh.ok) { state.csrf = fresh.data.csrf_token; }
                    state.syncing = false;
                    return sync(true);
                });
            }
            if (!result.ok) {
                state.errors.push(result.data.message || 'Synchronizacja nie powiodła się (' + result.status + ').');
                return;
            }
            var sent = {};
            ops.forEach(function (op) { sent[op.id] = true; });
            (result.data.results || []).forEach(function (item) {
                if (!item.ok) { state.errors.push('Nie zapisano zmiany: ' + item.message); }
            });
            state.queue = state.queue.filter(function (op) { return !sent[op.id]; });
            applyPayload(result.data);
            return persist();
        }).catch(function () {
            state.offline = true;
        }).then(function () {
            state.syncing = false;
            render();
            if (state.syncAgain) {
                state.syncAgain = false;
                scheduleSync(0);
            }
        });
    }

    // --- Rysowanie -------------------------------------------------------------
    function render() {
        renderStatus();
        renderBanner();
        if (!state.snapshot && !state.queue.length) { return; }
        var list = workouts();
        var today = S.todayIso();
        renderWeek(list, today);
        renderCalendar(list, today);
        renderMonth(list, today);
        renderProgress(list, today);
        renderList(list);
        renderGarminButton();
    }

    function timeLabel(iso) {
        if (!iso) { return ''; }
        var date = new Date(iso);
        var now = new Date();
        var hm = date.toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit' });
        return date.toDateString() === now.toDateString() ? hm : S.shortDayLabel(S.todayIso(date)) + ' ' + hm;
    }

    function renderStatus() {
        var chip = $('[data-act-status]');
        if (!chip) { return; }
        var pending = state.queue.length;
        var text;
        var tone = '';
        if (state.syncing) {
            text = 'Synchronizuję…';
        } else if (state.authNeeded) {
            text = 'Zaloguj się';
            tone = 'is-expense';
        } else if (state.offline) {
            text = 'Offline' + (pending ? ' · do wysłania: ' + pending : '');
            tone = 'is-invest';
        } else if (pending) {
            text = 'Do wysłania: ' + pending;
        } else if (state.syncedAt) {
            text = 'Zsynchronizowano ' + timeLabel(state.syncedAt);
            tone = 'is-income';
        } else {
            text = 'Wczytuję…';
        }
        chip.className = 'u-chip u-chip-dot act-status ' + tone;
        chip.textContent = text;
    }

    function alertHtml(kind, html, dismissIndex) {
        var close = dismissIndex === undefined ? ''
            : '<button type="button" class="btn-close" aria-label="Zamknij" data-act-dismiss="' + dismissIndex + '"></button>';
        return '<div class="alert alert-' + kind + (close ? ' alert-dismissible' : '') + ' app-message" role="alert">' + html + close + '</div>';
    }

    function renderBanner() {
        var banner = $('[data-act-banner]');
        var parts = [];
        if (state.authNeeded) {
            parts.push(alertHtml('warning', 'Sesja wygasła – <a href="' + esc(config.loginUrl) +
                '">zaloguj się</a>, żeby wysłać treningi. Zmiany czekają w telefonie.'));
        }
        if (!state.snapshot && state.offline) {
            parts.push(alertHtml('info', 'Brak połączenia i brak zapisanych danych. Otwórz stronę raz w domu, ' +
                'a potem zadziała też bez połączenia. Nowy trening możesz dodać już teraz.'));
        }
        var garmin = garminStatus();
        if (garmin.error) {
            parts.push(alertHtml('warning', '<strong>Garmin:</strong> ' + esc(garmin.error)));
        }
        state.errors.forEach(function (message, index) {
            parts.push(alertHtml('danger', esc(message), index));
        });
        if (!('serviceWorker' in navigator) && location.protocol === 'http:' && !state.swNoticeHidden) {
            parts.push(alertHtml('secondary', 'Tryb offline działa tylko pod adresem https:// – tutaj dane są w telefonie, ' +
                'ale sama strona nie otworzy się bez połączenia.', 'sw'));
        }
        banner.innerHTML = parts.join('');
    }

    function renderGarminButton() {
        var button = $('[data-act-garmin]');
        var garmin = garminStatus();
        button.hidden = !garmin.enabled;
        button.title = garmin.connected
            ? 'Ostatnio pobrane: ' + (garmin.syncedAt ? timeLabel(garmin.syncedAt) : 'jeszcze nie')
            : 'Konto Garmin nie jest połączone';
    }

    function renderWeek(list, today) {
        var info = S.weeklyGoal(list, weeklyGoal(), today);
        $('[data-act-goal]').value = String(info.goal);
        $('[data-act-goal-value]').textContent = info.goal;
        $('[data-act-done]').textContent = info.done;
        $('[data-act-meter]').style.width = Math.min(100, Math.round(info.done / info.goal * 100)) + '%';
        $('[data-act-motivation]').textContent = S.motivation(info);
        $('[data-act-motivation]').classList.toggle('is-met', info.met);
        $('[data-act-streak]').textContent = info.streak + ' ' + S.plural(info.streak, 'tydzień', 'tygodnie', 'tygodni');
        $('[data-act-best]').textContent = info.bestStreak;
        if (info.daysSinceLast === null) {
            $('[data-act-last]').textContent = '–';
            $('[data-act-last-note]').textContent = 'jeszcze bez treningów';
        } else {
            $('[data-act-last]').textContent = info.daysSinceLast === 0 ? 'dziś'
                : info.daysSinceLast === 1 ? 'wczoraj'
                    : info.daysSinceLast + ' dni temu';
            $('[data-act-last-note]').textContent = S.dayLabel(info.lastIso, true);
        }
        $('[data-act-days]').innerHTML = info.weekDays.map(function (day, index) {
            var kinds = day.kinds.filter(function (k, i) { return day.kinds.indexOf(k) === i; });
            var label = S.dayLabel(day.iso, true) + ': ' + (kinds.length
                ? kinds.map(function (k) { return S.KIND_LABELS[k]; }).join(', ') : 'bez treningu');
            var dots = kinds.map(function (k) { return '<span class="act-day-dot act-k-' + k + '"></span>'; }).join('');
            return '<li class="act-day' + (kinds.length ? ' is-active' : '') + (day.isToday ? ' is-today' : '') +
                (day.isFuture ? ' is-future' : '') + '" title="' + esc(label) + '">' +
                '<span class="act-day-name">' + S.WEEKDAYS_SHORT[index] + '</span>' +
                '<span class="act-day-mark">' + (dots || '') + '</span>' +
                '<span class="visually-hidden">' + esc(label) + '</span></li>';
        }).join('');
    }

    // Kalendarz: kolumny = tygodnie, wiersze = dni (pn–nd).
    var calendarDays = {};

    function renderCalendar(list, today) {
        var data = S.heatmap(list, today, 53);
        var container = $('[data-act-calendar]');
        calendarDays = {};
        var months = data.weeks.map(function (week) {
            return '<span>' + esc(week.monthLabel) + '</span>';
        }).join('');
        var cells = [];
        data.weeks.forEach(function (week) {
            week.days.forEach(function (day) {
                calendarDays[day.iso] = day;
                if (day.future) {
                    cells.push('<span class="act-cell is-future"></span>');
                } else if (day.kind) {
                    cells.push('<button type="button" class="act-cell act-k-' + day.kind + '" data-iso="' + day.iso +
                        '" aria-label="' + esc(S.dayLabel(day.iso, true) + ': ' + day.workouts.map(S.summarize).join('; ')) + '"></button>');
                } else {
                    cells.push('<span class="act-cell" data-iso="' + day.iso + '"></span>');
                }
            });
        });
        var weekdays = S.WEEKDAYS_SHORT.map(function (name, index) {
            return '<span>' + (index % 2 === 0 ? name : '') + '</span>';
        }).join('');
        container.style.setProperty('--act-weeks', data.weeks.length);
        container.innerHTML =
            '<div class="act-cal-months" aria-hidden="true">' + months + '</div>' +
            '<div class="act-cal-weekdays" aria-hidden="true">' + weekdays + '</div>' +
            '<div class="act-cal-grid">' + cells.join('') + '</div>';
        sizeCalendar();
        container.scrollLeft = container.scrollWidth;
        $('[data-act-cal-summary]').textContent = data.activeDays + ' ' +
            S.plural(data.activeDays, 'dzień', 'dni', 'dni') + ' z treningiem w ostatnim roku';
        $('[data-act-legend]').innerHTML = ['swim', 'run', 'gym', 'other'].map(function (kind) {
            return '<span class="u-legend-item"><span class="u-legend-swatch act-k-' + kind + '"></span>' +
                S.KIND_LABELS[kind] + '</span>';
        }).join('');
    }

    /** Komórki rosną z szerokością karty (11–22 px); na telefonie kalendarz się przewija. */
    function sizeCalendar() {
        var container = $('[data-act-calendar]');
        var weekdays = $('.act-cal-weekdays', container);
        if (!weekdays) { return; }
        var weeks = Number(container.style.getPropertyValue('--act-weeks')) || 53;
        var styles = getComputedStyle(container);
        var gap = 3;
        var available = container.clientWidth - weekdays.offsetWidth - (parseFloat(styles.columnGap) || 0);
        var cell = Math.floor((available - gap * (weeks - 1)) / weeks);
        container.style.setProperty('--act-cell', Math.max(11, Math.min(22, cell)) + 'px');
    }

    function showTooltip(cell) {
        var tooltip = $('[data-act-tooltip]');
        var day = calendarDays[cell.getAttribute('data-iso')];
        if (!day) { return; }
        var body = day.workouts.length
            ? day.workouts.map(function (w) {
                return '<div class="act-tooltip-row"><span class="act-day-dot act-k-' + w.kind + '"></span>' +
                    '<span>' + esc(S.summarize(w)) + '</span></div>';
            }).join('')
            : '<div class="act-tooltip-row is-muted">Bez treningu</div>';
        tooltip.innerHTML = '<div class="act-tooltip-title">' + esc(S.dayLabel(day.iso, true)) + '</div>' + body;
        tooltip.hidden = false;
        var wrap = tooltip.parentElement.getBoundingClientRect();
        var rect = cell.getBoundingClientRect();
        var width = tooltip.offsetWidth;
        var left = rect.left - wrap.left + rect.width / 2 - width / 2;
        left = Math.max(0, Math.min(left, wrap.width - width));
        var top = rect.top - wrap.top - tooltip.offsetHeight - 8;
        if (top < 0) { top = rect.bottom - wrap.top + 8; }
        tooltip.style.left = left + 'px';
        tooltip.style.top = top + 'px';
    }

    function hideTooltip() {
        $('[data-act-tooltip]').hidden = true;
    }

    function delta(current, previous, format) {
        var diff = current - previous;
        var tone = diff > 0 ? ' is-good' : diff < 0 ? ' is-bad' : '';
        var arrow = diff > 0 ? '↑' : diff < 0 ? '↓' : '→';
        return '<div class="u-metric-delta' + tone + '"><span class="u-metric-delta-value">' + arrow + ' ' +
            esc(format(Math.abs(diff))) + '</span><span>vs ' + esc(format(previous)) + ' miesiąc wcześniej</span></div>';
    }

    function renderMonth(list, today) {
        var data = S.monthComparison(list, today);
        var cur = data.current;
        var prev = data.previous;
        var km = function (m) { return S.formatDistance('run', m) || '0 km'; };
        var meters = function (m) { return S.formatDistance('swim', m) || '0 m'; };
        var count = function (n) { return String(n); };
        var hours = function (s) { return S.formatHours(s); };
        $('[data-act-month-title]').textContent = 'Ten miesiąc na tle poprzedniego';
        var tiles = [
            ['bi-calendar-check', 'Dni z treningiem', count(cur.days), delta(cur.days, prev.days, count)],
            ['bi-stopwatch', 'Czas treningów', hours(cur.duration), delta(cur.duration, prev.duration, hours)],
            ['bi-water', 'Pływanie', meters(cur.swimDistance), delta(cur.swimDistance, prev.swimDistance, meters)],
            ['bi-person-walking', 'Bieganie', km(cur.runDistance), delta(cur.runDistance, prev.runDistance, km)],
            ['bi-heart-pulse', 'Siłownia', cur.gymSessions + ' ' + S.plural(cur.gymSessions, 'trening', 'treningi', 'treningów'),
                delta(cur.gymSessions, prev.gymSessions, count)]
        ];
        $('[data-act-month]').innerHTML = tiles.map(function (tile) {
            return '<div class="u-metric"><div class="u-metric-label"><i class="bi ' + tile[0] + '" aria-hidden="true"></i> ' +
                tile[1] + '</div><div class="u-metric-value">' + esc(tile[2]) + '</div>' + tile[3] + '</div>';
        }).join('');
    }

    // --- Wykresy postępów ----------------------------------------------------
    function chartOptions(yFormat, tooltipTitle, tooltipLabel) {
        var options = AppCharts.baseOptions();
        options.plugins.legend.display = false;   // jedna seria - tytuł ją nazywa
        options.plugins.tooltip.displayColors = false;
        options.plugins.tooltip.callbacks = { title: tooltipTitle, label: tooltipLabel };
        options.scales.y.ticks.callback = yFormat;
        options.scales.y.beginAtZero = true;
        return options;
    }

    var volumeSlot = null;
    var trendSlot = null;
    var chartData = { volume: null, trend: null };

    function volumeChart() {
        var data = chartData.volume;
        var canvas = $('[data-act-volume-chart]');
        if (!data || !window.Chart) { return null; }
        var color = kindColor(data.kind);
        var unit = data.kind === 'run' ? ' km' : data.kind === 'swim' ? ' m' : '';
        var format = function (value) {
            if (data.kind === 'gym') { return value + ' ' + S.plural(value, 'trening', 'treningi', 'treningów'); }
            return new Intl.NumberFormat('pl-PL', { maximumFractionDigits: 1 }).format(value) + unit;
        };
        var options = chartOptions(
            function (value) { return data.kind === 'gym' ? value : format(value); },
            function (items) { return items.length ? 'Tydzień od ' + S.dayLabel(data.points[items[0].dataIndex].week) : ''; },
            function (ctx) { return format(ctx.parsed.y); }
        );
        if (data.kind === 'gym') { options.scales.y.ticks.precision = 0; }
        return new Chart(canvas, {
            type: 'bar',
            data: {
                labels: data.points.map(function (p) { return S.shortDayLabel(p.week); }),
                datasets: [{
                    label: data.title,
                    data: data.points.map(function (p) {
                        return data.kind === 'run' ? Math.round(p.value / 100) / 10 : p.value;
                    }),
                    backgroundColor: color,
                    borderRadius: 4,
                    maxBarThickness: 18
                }]
            },
            options: options
        });
    }

    function trendChart() {
        var data = chartData.trend;
        var canvas = $('[data-act-trend-chart]');
        if (!data || data.points.length < 2 || !window.Chart) { return null; }
        var color = kindColor(data.kind);
        var p = AppCharts.palette();
        var isGym = data.kind === 'gym';
        var options = chartOptions(
            function (value) { return isGym ? value + ' kg' : S.formatPace(data.kind, value).replace(/\/.*/, ''); },
            function (items) { return items.length ? S.dayLabel(data.points[items[0].dataIndex].date, true) : ''; },
            function (ctx) {
                var point = data.points[ctx.dataIndex];
                if (isGym) { return ['Najcięższa seria: ' + S.formatWeight(point.maxWeight), point.summary]; }
                return [S.formatPace(data.kind, point.pace), S.formatDistance(data.kind, point.distance)];
            }
        );
        options.plugins.appCrosshair = { color: p.axis };
        if (!isGym) {
            // Szybciej = mniej sekund na km; odwrócona oś, żeby lepszy wynik był wyżej.
            options.scales.y.reverse = true;
            options.scales.y.beginAtZero = false;
        }
        return new Chart(canvas, {
            type: 'line',
            data: {
                labels: data.points.map(function (point) { return S.shortDayLabel(point.date); }),
                datasets: [{
                    label: data.title,
                    data: data.points.map(function (point) { return isGym ? point.maxWeight : Math.round(point.pace); }),
                    borderColor: color,
                    backgroundColor: color,
                    pointBackgroundColor: color,
                    pointBorderColor: p.surface,
                    borderWidth: 2,
                    pointRadius: 4,
                    pointHoverRadius: 6,
                    pointBorderWidth: 2,
                    tension: 0
                }]
            },
            options: options,
            plugins: [AppCharts.crosshair]
        });
    }

    function defaultTab(list) {
        try {
            var saved = localStorage.getItem(TAB_KEY);
            if (saved) { return saved; }
        } catch (e) { /* tryb prywatny */ }
        var recent = list.filter(function (w) { return w.kind !== 'other'; })[0];
        return recent ? recent.kind : 'run';
    }

    function renderProgress(list, today) {
        state.tab = state.tab || defaultTab(list);
        var kind = state.tab;
        $$('[data-act-tabs] [data-kind]').forEach(function (button) {
            var active = button.getAttribute('data-kind') === kind;
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-selected', active ? 'true' : 'false');
        });

        var volumeTitle = { run: 'Kilometry w tygodniu', swim: 'Metry w tygodniu', gym: 'Treningi siłowe w tygodniu' }[kind];
        $('[data-act-volume-title]').textContent = volumeTitle + ' (16 tygodni)';
        chartData.volume = { kind: kind, title: volumeTitle, points: S.weeklyVolume(list, kind, today, 16) };

        var select = $('[data-act-exercise]');
        var trend;
        if (kind === 'gym') {
            var names = S.exerciseNames(list);
            if (names.indexOf(state.exercise) === -1) { state.exercise = names[0] || null; }
            select.hidden = !names.length;
            select.innerHTML = names.map(function (name) {
                return '<option' + (name === state.exercise ? ' selected' : '') + '>' + esc(name) + '</option>';
            }).join('');
            trend = {
                kind: kind,
                title: 'Najcięższa seria',
                points: state.exercise ? S.exerciseProgress(list, state.exercise).slice(-30) : []
            };
            $('[data-act-trend-title]').textContent = 'Najcięższa seria';
        } else {
            select.hidden = true;
            trend = { kind: kind, title: 'Tempo', points: S.paceSeries(list, kind, 30) };
            $('[data-act-trend-title]').textContent = kind === 'run'
                ? 'Tempo biegów (wyżej = szybciej)' : 'Tempo na 100 m (wyżej = szybciej)';
        }
        chartData.trend = trend;
        var empty = $('[data-act-trend-empty]');
        empty.hidden = trend.points.length >= 2;
        empty.textContent = kind === 'gym'
            ? 'Dodaj co najmniej dwa treningi z seriami tego ćwiczenia, żeby zobaczyć postęp.'
            : 'Potrzeba co najmniej dwóch treningów z dystansem i czasem.';
        $('[data-act-trend-chart]').parentElement.hidden = trend.points.length < 2;

        if (window.AppCharts && window.Chart) {
            volumeSlot = volumeSlot || AppCharts.slot(volumeChart);
            trendSlot = trendSlot || AppCharts.slot(trendChart);
            volumeSlot.redraw();
            trendSlot.redraw();
        }
        renderRecords(list, kind);
    }

    function recordTile(label, value, detail) {
        return '<div class="act-record"><span class="act-record-label">' + esc(label) + '</span>' +
            '<span class="act-record-value">' + esc(value) + '</span>' +
            '<span class="act-record-detail">' + esc(detail) + '</span></div>';
    }

    function renderRecords(list, kind) {
        var rec = S.records(list);
        var tiles = [];
        if (kind === 'gym') {
            rec.gym.slice(0, 8).forEach(function (lift) {
                tiles.push(recordTile(lift.exercise, S.formatWeight(lift.weight) + (lift.reps ? ' × ' + lift.reps : ''),
                    S.dayLabel(lift.date)));
            });
        } else {
            var group = rec[kind];
            if (group.longest) {
                tiles.push(recordTile(kind === 'run' ? 'Najdłuższy bieg' : 'Najdłuższe pływanie',
                    S.formatDistance(kind, group.longest.distance),
                    S.dayLabel(group.longest.date) + (group.longest.duration ? ' · ' + S.formatDuration(group.longest.duration) : '')));
            }
            if (group.fastest) {
                tiles.push(recordTile('Najszybsze tempo (od ' + S.formatDistance(kind, group.fastestMinimum) + ')',
                    S.formatPace(kind, S.pace(group.fastest)),
                    S.dayLabel(group.fastest.date) + ' · ' + S.formatDistance(kind, group.fastest.distance)));
            }
        }
        $('[data-act-records]').innerHTML = tiles.length ? tiles.join('')
            : '<p class="act-empty">Rekordy pojawią się po pierwszych treningach.</p>';
    }

    // --- Lista treningów --------------------------------------------------------
    function recordUids(list) {
        var rec = S.records(list);
        var uids = {};
        ['run', 'swim'].forEach(function (kind) {
            if (rec[kind].longest) { uids[rec[kind].longest.uid] = true; }
            if (rec[kind].fastest) { uids[rec[kind].fastest.uid] = true; }
        });
        return uids;
    }

    function renderList(list) {
        var container = $('[data-act-list]');
        var records = recordUids(list);
        $('[data-act-list-count]').textContent = list.length + ' ' + S.plural(list.length, 'trening', 'treningi', 'treningów');
        if (!list.length) {
            container.innerHTML = '<li class="act-empty">Jeszcze nic tu nie ma. Dodaj pierwszy trening' +
                (garminStatus().enabled ? ' albo pobierz go z Garmina.' : '.') + '</li>';
            $('[data-act-more]').hidden = true;
            return;
        }
        container.innerHTML = list.slice(0, state.listLimit).map(function (w) {
            var icons = '';
            if (records[w.uid]) { icons += '<i class="bi bi-trophy-fill act-record-icon" title="Rekord" aria-label="Rekord"></i>'; }
            if (w.source === 'garmin') { icons += '<i class="bi bi-smartwatch" title="Z Garmina" aria-label="Z Garmina"></i>'; }
            if (w.pending) { icons += '<i class="bi bi-cloud-arrow-up" title="Czeka na wysłanie" aria-label="Czeka na wysłanie"></i>'; }
            var extra = w.kind === 'gym' && w.sets && w.sets.length ? S.exercisesOf(w).join(', ') : (w.notes || '');
            return '<li><button type="button" class="act-item act-k-' + w.kind + '" data-uid="' + esc(w.uid) + '">' +
                '<span class="act-item-bar"></span>' +
                '<span class="act-item-main"><span class="act-item-title">' + esc(S.summarize(w)) + '</span>' +
                '<span class="act-item-sub">' + esc(S.dayLabel(w.date, true)) + (w.time ? ' · ' + esc(w.time) : '') +
                (extra ? ' · ' + esc(extra) : '') + '</span></span>' +
                '<span class="act-item-icons">' + icons + '</span></button></li>';
        }).join('');
        $('[data-act-more]').hidden = list.length <= state.listLimit;
    }

    // --- Formularz ----------------------------------------------------------------
    var form = $('[data-act-form]');
    var modalNode = document.getElementById('act-form-modal');
    var editing = null;

    function modal() {
        return window.bootstrap ? bootstrap.Modal.getOrCreateInstance(modalNode) : null;
    }

    function formKind() {
        var checked = $('input[name="kind"]:checked', form);
        return checked ? checked.value : 'run';
    }

    function updateKindFields() {
        var kind = formKind();
        $('[data-act-field="distance"]', form).hidden = kind === 'gym';
        $('[data-act-field="sets"]', form).hidden = kind !== 'gym';
        $('[data-act-distance-label]', form).textContent = kind === 'swim' ? 'Dystans (m)' : 'Dystans (km)';
        if (kind === 'gym' && !$$('.act-set', form).length) { addSetRow(['', '', '']); }
    }

    function addSetRow(row, focus) {
        var container = $('[data-act-sets]', form);
        var node = document.createElement('div');
        node.className = 'act-set';
        node.innerHTML =
            '<input class="form-control form-control-sm" list="act-exercises" placeholder="Ćwiczenie" aria-label="Ćwiczenie" data-set="exercise">' +
            '<input class="form-control form-control-sm" type="number" min="0" inputmode="numeric" placeholder="powt." aria-label="Powtórzenia" data-set="reps">' +
            '<input class="form-control form-control-sm" type="text" inputmode="decimal" placeholder="kg" aria-label="Ciężar w kg" data-set="kg">' +
            '<button type="button" class="btn btn-sm btn-link text-secondary" data-set-remove aria-label="Usuń serię"><i class="bi bi-x-lg"></i></button>';
        $('[data-set="exercise"]', node).value = row[0] || '';
        $('[data-set="reps"]', node).value = row[1] === null || row[1] === undefined ? '' : row[1];
        $('[data-set="kg"]', node).value = row[2] === null || row[2] === undefined ? '' : String(row[2]).replace('.', ',');
        container.appendChild(node);
        if (focus) { $('[data-set="' + focus + '"]', node).focus(); }
    }

    function openForm(workout) {
        editing = workout || null;
        form.reset();
        $('[data-act-sets]', form).innerHTML = '';
        $('[data-act-form-error]', form).hidden = true;
        $('#act-form-title').textContent = workout ? 'Edytuj trening' : 'Nowy trening';
        $('[data-act-delete]', form).hidden = !workout;
        $('[data-act-exercises]', form).innerHTML = S.exerciseNames(workouts()).map(function (name) {
            return '<option value="' + esc(name) + '">';
        }).join('');
        var w = workout || { kind: state.tab && state.tab !== 'other' ? state.tab : 'run', date: S.todayIso() };
        $$('input[name="kind"]', form).forEach(function (input) { input.checked = input.value === w.kind; });
        form.elements.date.value = w.date;
        form.elements.time.value = w.time || '';
        form.elements.distance.value = !w.distance ? ''
            : w.kind === 'swim' ? w.distance : String(Math.round(w.distance / 10) / 100).replace('.', ',');
        form.elements.duration.value = S.formatDuration(w.duration);
        form.elements.avgHr.value = w.avgHr || '';
        form.elements.title.value = w.title || '';
        form.elements.notes.value = w.notes || '';
        (w.sets || []).forEach(function (row) { addSetRow(row); });
        updateKindFields();
        var instance = modal();
        if (instance) { instance.show(); }
    }

    function parseNumber(text) {
        var value = String(text || '').trim().replace(/\s/g, '').replace(',', '.');
        if (!value) { return null; }
        var number = Number(value);
        return isFinite(number) && number >= 0 ? number : NaN;
    }

    function formError(message) {
        var box = $('[data-act-form-error]', form);
        box.textContent = message;
        box.hidden = false;
    }

    function readForm() {
        var kind = formKind();
        var date = form.elements.date.value;
        if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) { throw new Error('Podaj datę treningu.'); }
        var duration = S.parseDuration(form.elements.duration.value);
        if (isNaN(duration)) { throw new Error('Nie rozumiem czasu – wpisz np. 45 (minuty) albo 28:30.'); }
        var distance = null;
        if (kind !== 'gym') {
            distance = parseNumber(form.elements.distance.value);
            if (isNaN(distance)) { throw new Error('Dystans musi być liczbą.'); }
            if (distance !== null) { distance = kind === 'swim' ? Math.round(distance) : Math.round(distance * 1000); }
        }
        var avgHr = parseNumber(form.elements.avgHr.value);
        if (isNaN(avgHr)) { throw new Error('Tętno musi być liczbą.'); }
        var sets = [];
        if (kind === 'gym') {
            $$('.act-set', form).forEach(function (node) {
                var exercise = $('[data-set="exercise"]', node).value.trim();
                var reps = parseNumber($('[data-set="reps"]', node).value);
                var kg = parseNumber($('[data-set="kg"]', node).value);
                if (!exercise) { return; }
                if (isNaN(reps) || isNaN(kg)) { throw new Error('Powtórzenia i ciężar w serii „' + exercise + '” muszą być liczbami.'); }
                sets.push([exercise, reps === null ? null : Math.round(reps), kg]);
            });
        }
        return {
            uid: editing ? editing.uid : newUid(),
            kind: kind,
            date: date,
            time: form.elements.time.value || '',
            title: form.elements.title.value.trim(),
            duration: duration,
            distance: distance,
            avgHr: avgHr === null ? null : Math.round(avgHr),
            calories: editing ? editing.calories || null : null,
            notes: form.elements.notes.value.trim(),
            sets: sets
        };
    }

    function stripLocal(workout) {
        var copy = Object.assign({}, workout);
        delete copy.pending;
        delete copy.source;
        return copy;
    }

    // --- Komunikat z „Cofnij” -----------------------------------------------------
    var toastTimer = null;
    function toast(text, undo) {
        var node = $('[data-act-toast]');
        $('[data-act-toast-text]', node).textContent = text;
        var button = $('[data-act-toast-undo]', node);
        button.hidden = !undo;
        button.onclick = function () {
            node.hidden = true;
            if (undo) { undo(); }
        };
        node.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(function () { node.hidden = true; }, undo ? 8000 : 4000);
    }

    function deleteWorkout(workout) {
        var original = stripLocal(workout);
        enqueue({ type: 'delete', uid: workout.uid });
        toast('Usunięto trening z ' + S.dayLabel(workout.date) + '.', function () {
            enqueue({ type: 'save', workout: original });
        });
    }

    // --- Zdarzenia ------------------------------------------------------------------
    function bindEvents() {
        $('[data-act-add]').addEventListener('click', function () { openForm(null); });

        $$('input[name="kind"]', form).forEach(function (input) {
            input.addEventListener('change', updateKindFields);
        });
        $('[data-act-add-set]', form).addEventListener('click', function () {
            // Kolejna seria tego samego ćwiczenia: kopia poprzedniej, kursor w powtórzeniach.
            var rows = $$('.act-set', form);
            var last = rows[rows.length - 1];
            var row = last ? [
                $('[data-set="exercise"]', last).value,
                $('[data-set="reps"]', last).value,
                $('[data-set="kg"]', last).value
            ] : ['', '', ''];
            addSetRow(row, last ? 'reps' : 'exercise');
        });
        $('[data-act-add-exercise]', form).addEventListener('click', function () {
            addSetRow(['', '', ''], 'exercise');
        });
        $('[data-act-sets]', form).addEventListener('click', function (event) {
            var remove = event.target.closest('[data-set-remove]');
            if (remove) { remove.closest('.act-set').remove(); }
        });
        form.addEventListener('submit', function (event) {
            event.preventDefault();
            var workout;
            try {
                workout = readForm();
            } catch (error) {
                formError(error.message);
                return;
            }
            enqueue({ type: 'save', workout: workout });
            var instance = modal();
            if (instance) { instance.hide(); }
            toast(editing ? 'Zapisano zmiany.' : 'Dodano trening. Tak trzymaj!');
        });
        $('[data-act-delete]', form).addEventListener('click', function () {
            var workout = editing;
            var instance = modal();
            if (instance) { instance.hide(); }
            if (workout) { deleteWorkout(workout); }
        });

        $('[data-act-list]').addEventListener('click', function (event) {
            var item = event.target.closest('[data-uid]');
            if (!item) { return; }
            var uid = item.getAttribute('data-uid');
            var workout = workouts().filter(function (w) { return w.uid === uid; })[0];
            if (workout) { openForm(workout); }
        });
        $('[data-act-more]').addEventListener('click', function () {
            state.listLimit += 30;
            renderList(workouts());
        });

        $('[data-act-goal]').addEventListener('change', function (event) {
            enqueue({ type: 'goal', value: Number(event.target.value) });
        });

        $('[data-act-tabs]').addEventListener('click', function (event) {
            var button = event.target.closest('[data-kind]');
            if (!button) { return; }
            state.tab = button.getAttribute('data-kind');
            try { localStorage.setItem(TAB_KEY, state.tab); } catch (e) { /* tryb prywatny */ }
            renderProgress(workouts(), S.todayIso());
        });
        $('[data-act-exercise]').addEventListener('change', function (event) {
            state.exercise = event.target.value;
            renderProgress(workouts(), S.todayIso());
        });

        var calendar = $('[data-act-calendar]');
        calendar.addEventListener('mouseover', function (event) {
            var cell = event.target.closest('[data-iso]');
            if (cell) { showTooltip(cell); }
        });
        calendar.addEventListener('mouseleave', hideTooltip);
        calendar.addEventListener('focusin', function (event) {
            var cell = event.target.closest('[data-iso]');
            if (cell) { showTooltip(cell); }
        });
        calendar.addEventListener('focusout', hideTooltip);
        calendar.addEventListener('click', function (event) {
            var cell = event.target.closest('[data-iso]');
            if (cell) { showTooltip(cell); }   // dotyk na telefonie
        });
        calendar.addEventListener('scroll', hideTooltip, { passive: true });
        document.addEventListener('click', function (event) {
            if (!event.target.closest('[data-act-calendar]')) { hideTooltip(); }
        });

        $('[data-act-banner]').addEventListener('click', function (event) {
            var button = event.target.closest('[data-act-dismiss]');
            if (!button) { return; }
            var index = button.getAttribute('data-act-dismiss');
            if (index === 'sw') {
                state.swNoticeHidden = true;
            } else {
                state.errors.splice(Number(index), 1);
            }
            renderBanner();
        });

        $('[data-act-garmin]').addEventListener('click', function () {
            var garmin = garminStatus();
            if (!garmin.connected) {
                state.errors.push('Konto Garmin nie jest połączone. Na Raspberry Pi uruchom: ' +
                    'docker compose exec -it web python manage.py garmin_login --user ' + (state.user || '<login>'));
                renderBanner();
                return;
            }
            var button = $('[data-act-garmin]');
            button.disabled = true;
            postJson(config.garminUrl).then(function (result) {
                if (result.ok) {
                    applyPayload(result.data);
                    persist();
                    toast(result.data.message || 'Pobrano treningi z Garmina.');
                } else if (result.status === 401) {
                    state.authNeeded = true;
                } else {
                    state.errors.push(result.data.message || 'Nie udało się pobrać treningów z Garmina.');
                }
            }).catch(function () {
                state.errors.push('Pobieranie z Garmina wymaga połączenia z domowym serwerem.');
            }).then(function () {
                button.disabled = false;
                render();
            });
        });

        var resizeTimer = null;
        window.addEventListener('resize', function () {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(sizeCalendar, 150);
        });
        window.addEventListener('online', function () { scheduleSync(0); });
        window.addEventListener('offline', function () { state.offline = true; renderStatus(); });
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') { scheduleSync(0); }
        });
        setInterval(function () {
            if (document.visibilityState === 'visible') { sync(); }
        }, SYNC_INTERVAL_MS);
    }

    function registerServiceWorker() {
        if (!('serviceWorker' in navigator)) { return; }
        navigator.serviceWorker.register(config.serviceWorkerUrl, { scope: config.scope }).catch(function () {
            // Bez service workera strona działa dalej, tylko nie otworzy się offline.
        });
    }

    bindEvents();
    registerServiceWorker();
    store.get(STORE_KEY).then(function (saved) {
        if (saved) {
            state.snapshot = saved.snapshot || null;
            state.queue = saved.queue || [];
            state.user = saved.user || null;
            state.csrf = saved.csrf || null;
            state.syncedAt = saved.syncedAt || null;
        }
        render();
        sync();
    });
})();
