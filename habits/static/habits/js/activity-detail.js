/* Szczegóły treningu (okno po kliknięciu na liście): liczby z zegarka,
 * strefy tętna, okrążenia, trasa na mapie i serie z siłowni.
 *
 * window.ActivityDetail.open(trening, ctx), gdzie ctx = { config, onEdit(trening) }.
 * Mapa (Leaflet + kafelki OpenStreetMap) wymaga internetu - trasa przychodzi
 * z /habits/api/route/<uid>/, Leaflet ładuje się dopiero przy pierwszej mapie.
 */
(function () {
    'use strict';

    var S = window.ActivityStats;
    var modalNode = document.getElementById('act-detail-modal');
    if (!S || !modalNode) { return; }

    var current = null;
    var context = null;
    var map = null;
    var leafletPromise = null;
    var SET_TYPE_LABELS = { warmup: 'rozgrzewka', failure: 'do upadku', dropset: 'drop set' };

    function esc(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function num(value, digits) {
        return new Intl.NumberFormat('pl-PL', { maximumFractionDigits: digits || 0 }).format(value);
    }

    function stat(label, value, note) {
        if (value === null || value === undefined || value === '') { return ''; }
        return '<div class="act-stat"><span class="act-stat-label">' + esc(label) + '</span>' +
            '<span class="act-stat-value">' + esc(value) + '</span>' +
            (note ? '<span class="act-stat-note">' + esc(note) + '</span>' : '') + '</div>';
    }

    function statsHtml(w) {
        var d = w.details || {};
        var pace = S.pace(w);
        var parts = [
            stat('Dystans', S.formatDistance(w.kind, w.distance)),
            stat('Czas', S.formatDuration(w.duration), d.movingDuration && Math.abs(d.movingDuration - w.duration) > 30
                ? 'w ruchu ' + S.formatDuration(d.movingDuration) : ''),
            stat(w.kind === 'swim' ? 'Tempo na 100 m' : 'Tempo', pace ? S.formatPace(w.kind, pace) : ''),
            stat('Śr. tętno', w.avgHr ? w.avgHr + ' bpm' : '', d.maxHr ? 'maks. ' + d.maxHr : ''),
            stat('Kalorie', w.calories ? num(w.calories) + ' kcal' : ''),
            stat('Przewyższenie', d.elevationGain ? '↑ ' + num(d.elevationGain) + ' m' : '',
                d.elevationLoss ? '↓ ' + num(d.elevationLoss) + ' m' : ''),
            stat(w.kind === 'swim' ? 'Ruchy na minutę' : 'Kadencja', d.cadence ? d.cadence + (w.kind === 'swim' ? '' : ' kroków/min') : ''),
            stat('Moc', d.avgPower ? d.avgPower + ' W' : ''),
            stat('Training Effect', d.aerobicTE ? num(d.aerobicTE, 1) + ' tlenowy' : '',
                [d.anaerobicTE ? num(d.anaerobicTE, 1) + ' beztlenowy' : '', S.label(S.TE_LABELS, d.teLabel)].filter(Boolean).join(' · ')),
            stat('Obciążenie treningowe', d.trainingLoad ? num(d.trainingLoad) : ''),
            stat('VO2max po treningu', d.vo2max ? num(d.vo2max, 1) : ''),
            stat('Basen', d.poolLength ? num(d.poolLength, 1) + ' m' : '', d.lengths ? d.lengths + ' ' + S.plural(d.lengths, 'długość', 'długości', 'długości') : ''),
            stat('SWOLF', d.swolf || ''),
            stat('Ruchy ramion', d.strokes ? num(d.strokes) : '', d.avgStrokes ? num(d.avgStrokes, 1) + ' na długość' : ''),
            stat('Kroki', d.steps && w.kind === 'run' ? num(d.steps) : '')
        ];
        var html = parts.join('');
        return html ? '<div class="act-stats">' + html + '</div>' : '';
    }

    function zonesHtml(w) {
        var zones = (w.details || {}).hrZones;
        var shares = S.zoneShares(zones);
        if (!shares.length) { return ''; }
        var names = ['Rozgrzewka', 'Łatwa', 'Tlenowa', 'Progowa', 'Maksymalna'];
        var bar = shares.map(function (share, index) {
            return share ? '<span class="act-zone act-zone-' + (index + 1) + '" style="width:' + share + '%"></span>' : '';
        }).join('');
        var rows = zones.map(function (seconds, index) {
            return '<li><span class="act-zone-dot act-zone-' + (index + 1) + '"></span>' +
                '<span>Strefa ' + (index + 1) + ' · ' + names[index] + '</span>' +
                '<span class="act-zone-time">' + (seconds ? S.formatDuration(seconds) : '–') + ' (' + shares[index] + '%)</span></li>';
        }).join('');
        return '<h3 class="act-detail-subtitle">Strefy tętna</h3>' +
            '<div class="act-zones" role="img" aria-label="Czas w strefach tętna">' + bar + '</div>' +
            '<ul class="act-zone-list">' + rows + '</ul>';
    }

    function lapsHtml(w) {
        var laps = (w.details || {}).laps;
        if (!laps || laps.length < 2) { return ''; }
        var showElev = laps.some(function (lap) { return lap.elev; });
        var showHr = laps.some(function (lap) { return lap.hr; });
        var rows = laps.map(function (lap, index) {
            return '<tr><td>' + (index + 1) + '</td><td>' + esc(S.formatDistance(w.kind, lap.d)) + '</td>' +
                '<td>' + esc(S.formatDuration(lap.t)) + '</td><td>' + esc(S.lapPace(w.kind, lap)) + '</td>' +
                (showHr ? '<td>' + (lap.hr || '–') + '</td>' : '') +
                (showElev ? '<td>' + (lap.elev ? '↑ ' + lap.elev + ' m' : '–') + '</td>' : '') + '</tr>';
        }).join('');
        return '<h3 class="act-detail-subtitle">Okrążenia</h3><div class="u-table-wrap"><table class="u-table act-laps">' +
            '<thead><tr><th>#</th><th>Dystans</th><th>Czas</th><th>Tempo</th>' +
            (showHr ? '<th>Tętno</th>' : '') + (showElev ? '<th>Przewyższenie</th>' : '') +
            '</tr></thead><tbody>' + rows + '</tbody></table></div>';
    }

    function setsHtml(w) {
        if (!w.sets || !w.sets.length) { return ''; }
        var groups = [];
        w.sets.forEach(function (row) {
            var last = groups[groups.length - 1];
            if (!last || last.name !== row[0]) {
                last = { name: row[0], rows: [] };
                groups.push(last);
            }
            last.rows.push(row);
        });
        return '<h3 class="act-detail-subtitle">Ćwiczenia</h3>' + groups.map(function (group) {
            var number = 0;
            var items = group.rows.map(function (row) {
                var warmup = S.isWarmup(row);
                if (!warmup) { number += 1; }
                var amount = row[1] ? row[1] + ' powt.' : row[4] ? S.formatDuration(row[4]) : '–';
                var type = SET_TYPE_LABELS[row[3]];
                return '<li class="' + (warmup ? 'is-warmup' : '') + '"><span class="act-set-no">' + (warmup ? 'R' : number) + '</span>' +
                    '<span>' + (row[2] ? esc(S.formatWeight(row[2])) + ' × ' : '') + esc(amount) + '</span>' +
                    (type ? '<span class="act-set-type">' + type + '</span>' : '') + '</li>';
            }).join('');
            return '<div class="act-exercise"><div class="act-exercise-name">' + esc(group.name) + '</div><ol class="act-set-list">' + items + '</ol></div>';
        }).join('');
    }

    function sourcesHtml(w) {
        var parts = [];
        if (w.garmin) { parts.push('<span class="u-chip"><i class="bi bi-smartwatch" aria-hidden="true"></i> Garmin</span>'); }
        if (w.hevy) { parts.push('<span class="u-chip"><i class="bi bi-phone" aria-hidden="true"></i> Hevy</span>'); }
        if (!parts.length) { parts.push('<span class="u-chip"><i class="bi bi-pencil" aria-hidden="true"></i> Wpisany ręcznie</span>'); }
        if (w.pending) { parts.push('<span class="u-chip is-invest"><i class="bi bi-cloud-arrow-up" aria-hidden="true"></i> Czeka na wysłanie</span>'); }
        return parts.join(' ');
    }

    // --- Mapa ------------------------------------------------------------------
    function loadLeaflet() {
        if (window.L) { return Promise.resolve(window.L); }
        if (!leafletPromise) {
            leafletPromise = new Promise(function (resolve, reject) {
                var css = document.createElement('link');
                css.rel = 'stylesheet';
                css.href = context.config.leafletCss;
                document.head.appendChild(css);
                var script = document.createElement('script');
                script.src = context.config.leafletJs;
                script.onload = function () { resolve(window.L); };
                script.onerror = function () { leafletPromise = null; reject(new Error('leaflet')); };
                document.head.appendChild(script);
            });
        }
        return leafletPromise;
    }

    function showMap(w) {
        var box = modalNode.querySelector('[data-act-map]');
        if (!box) { return; }
        var message = function (text) { box.innerHTML = '<p class="act-empty">' + esc(text) + '</p>'; };
        if (!navigator.onLine) {
            message('Mapa trasy wymaga połączenia z internetem.');
            return;
        }
        var url = context.config.routeUrl.replace('UID', encodeURIComponent(w.uid));
        Promise.all([
            loadLeaflet(),
            fetch(url, { credentials: 'same-origin' }).then(function (response) {
                if (!response.ok) { throw new Error(String(response.status)); }
                return response.json();
            })
        ]).then(function (results) {
            var L = results[0];
            var points = results[1].points || [];
            if (current !== w || points.length < 2) {
                if (points.length < 2) { message('Brak trasy dla tego treningu.'); }
                return;
            }
            box.innerHTML = '';
            map = L.map(box, { scrollWheelZoom: false, attributionControl: true });
            // Polityka kafelków OSM (jak mapa podróży, finance/travel.html): tylko
            // tile.openstreetmap.org, widoczna atrybucja i nagłówek Referer. Strona ma
            // SECURE_REFERRER_POLICY = same-origin, więc bez łagodniejszej polityki na
            // samych kafelkach OSM dostaje żądanie bez referrera i zwraca 403 „Access blocked”.
            L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
                maxZoom: 19,
                referrerPolicy: 'strict-origin-when-cross-origin',
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'
            }).addTo(map);
            var color = context.kindColor(w.kind);
            var line = L.polyline(points, { color: color, weight: 4, opacity: 0.9 }).addTo(map);
            L.circleMarker(points[0], { radius: 6, color: '#fff', weight: 2, fillColor: color, fillOpacity: 1 }).addTo(map).bindTooltip('Start');
            L.circleMarker(points[points.length - 1], { radius: 6, color: color, weight: 2, fillColor: '#fff', fillOpacity: 1 }).addTo(map).bindTooltip('Meta');
            map.fitBounds(line.getBounds(), { padding: [16, 16] });
        }).catch(function () {
            message('Nie udało się wczytać trasy – sprawdź połączenie.');
        });
    }

    function render(w) {
        var title = w.title || S.KIND_LABELS[w.kind];
        modalNode.querySelector('#act-detail-title').innerHTML =
            '<span class="act-day-dot act-k-' + w.kind + '"></span> ' + esc(title);
        var location = (w.details || {}).location;
        var body = '<p class="act-detail-sub">' + esc(S.dayLabel(w.date, true)) + (w.time ? ', ' + esc(w.time) : '') +
            (location ? ' · ' + esc(location) : '') + '</p>' +
            '<div class="act-detail-sources">' + sourcesHtml(w) + '</div>' +
            statsHtml(w) +
            ((w.details || {}).hasRoute ? '<h3 class="act-detail-subtitle">Trasa</h3><div class="act-map" data-act-map><p class="act-empty">Wczytuję mapę…</p></div>' : '') +
            zonesHtml(w) + lapsHtml(w) + setsHtml(w) +
            (w.notes ? '<h3 class="act-detail-subtitle">Notatka</h3><p class="act-notes">' + esc(w.notes) + '</p>' : '');
        modalNode.querySelector('[data-act-detail-body]').innerHTML = body;
    }

    function open(workout, ctx) {
        current = workout;
        context = ctx;
        if (map) { map.remove(); map = null; }
        render(workout);
        var modal = window.bootstrap ? bootstrap.Modal.getOrCreateInstance(modalNode) : null;
        if (modal) { modal.show(); }
    }

    modalNode.addEventListener('shown.bs.modal', function () {
        // Mapa dopiero po pokazaniu okna - Leaflet musi znać jego rozmiar.
        if (current && (current.details || {}).hasRoute) { showMap(current); }
    });
    modalNode.addEventListener('hidden.bs.modal', function () {
        if (map) { map.remove(); map = null; }
    });
    modalNode.querySelector('[data-act-detail-edit]').addEventListener('click', function () {
        var workout = current;
        var modal = window.bootstrap ? bootstrap.Modal.getOrCreateInstance(modalNode) : null;
        if (modal) {
            modalNode.addEventListener('hidden.bs.modal', function once() {
                modalNode.removeEventListener('hidden.bs.modal', once);
                if (context && workout) { context.onEdit(workout); }
            });
            modal.hide();
        } else if (context && workout) {
            context.onEdit(workout);
        }
    });

    window.ActivityDetail = { open: open };
})();
