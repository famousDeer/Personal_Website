/* Sekcje „Forma” i „Regeneracja i zdrowie” z danych dziennych zegarka.
 *
 * window.ActivityHealth.render({ workouts, health, today }) - wołane przez
 * activity-app.js przy każdym rysowaniu. Sekcje są ukryte, dopóki nie ma
 * danych (osoba bez Garmina ich nie widzi).
 */
(function () {
    'use strict';

    var S = window.ActivityStats;
    var fitnessNode = document.querySelector('[data-act-fitness]');
    var healthNode = document.querySelector('[data-act-health]');
    if (!S || !fitnessNode || !healthNode) { return; }

    var METRIC_KEY = 'aktywnosc-zdrowie-wykres';
    var DAYS = 30;
    // Kolor = tożsamość miary (kolejne kolory kategorii po dyscyplinach).
    var METRICS = [
        { key: 'sleep', label: 'Sen', type: 'bar', color: 6, value: function (v) { return Math.round(v / 360) / 10; }, format: function (v) { return S.formatSleep(v * 3600); } },
        { key: 'restingHr', label: 'Tętno spoczynkowe', type: 'line', color: 7, format: function (v) { return v + ' bpm'; } },
        { key: 'hrv', label: 'HRV', type: 'line', color: 4, format: function (v) { return v + ' ms'; } },
        { key: 'steps', label: 'Kroki', type: 'bar', color: 5, format: function (v) { return new Intl.NumberFormat('pl-PL').format(v); } },
        { key: 'bbHigh', label: 'Body Battery', type: 'bar', color: 0, format: function (v) { return String(v); } },
        { key: 'stress', label: 'Stres', type: 'line', color: 3, format: function (v) { return String(v); } }
    ];
    var READINESS_ADVICE = {
        PRIME: 'Świetny dzień na mocny trening.',
        HIGH: 'Dobry dzień na mocniejszy trening.',
        MODERATE: 'Zwykły trening będzie w sam raz.',
        LOW: 'Lepiej lżej – spokojne pływanie albo rozruch.',
        POOR: 'Postaw na regenerację – sen i spacer.'
    };

    var state = { metric: null, data: null };
    var healthSlot = null;
    var vo2Slot = null;

    function esc(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function num(value, digits) {
        return new Intl.NumberFormat('pl-PL', { maximumFractionDigits: digits || 0 }).format(value);
    }

    function tile(icon, label, value, note) {
        if (value === null || value === undefined || value === '') { return ''; }
        return '<div class="u-metric"><div class="u-metric-label"><i class="bi ' + icon + '" aria-hidden="true"></i> ' + esc(label) +
            '</div><div class="u-metric-value">' + esc(value) + '</div>' +
            (note ? '<div class="u-metric-note">' + esc(note) + '</div>' : '') + '</div>';
    }

    function whenLabel(date, today) {
        if (!date) { return ''; }
        var diff = S.dayNumber(today) - S.dayNumber(date);
        return diff === 0 ? 'dziś' : diff === 1 ? 'wczoraj' : S.shortDayLabel(date);
    }

    function palette() {
        return window.AppCharts ? AppCharts.palette() : null;
    }

    // --- Forma -------------------------------------------------------------------
    function renderFitness(ctx) {
        var vo2 = S.vo2Series(ctx.workouts, ctx.health);
        var status = S.latestHealth(ctx.health, 'status');
        var readiness = S.latestHealth(ctx.health, 'readiness');
        var level = S.latestHealth(ctx.health, 'readinessLevel');
        var race = ['race5k', 'race10k', 'raceHalf', 'raceMarathon'].map(function (key) {
            return S.latestHealth(ctx.health, key);
        });
        var tiles = [];
        if (vo2.length) {
            var last = vo2[vo2.length - 1];
            var before = S.valueBefore(vo2, ctx.today, 90);
            var note = before && before.date !== last.date
                ? (last.value >= before.value ? '↑ ' : '↓ ') + num(Math.abs(last.value - before.value), 1) + ' w 90 dni'
                : whenLabel(last.date, ctx.today);
            tiles.push(tile('bi-lungs', 'VO2max', num(last.value, 1), note));
        }
        if (status) {
            tiles.push(tile('bi-graph-up-arrow', 'Status treningowy', S.label(S.TRAINING_STATUS, status.value), whenLabel(status.date, ctx.today)));
        }
        if (readiness && S.dayNumber(ctx.today) - S.dayNumber(readiness.date) <= 1) {
            var levelKey = level && level.date === readiness.date ? level.value : '';
            tiles.push(tile('bi-battery-charging', 'Gotowość do treningu', readiness.value + '/100',
                [S.label(S.READINESS_LEVELS, levelKey), READINESS_ADVICE[levelKey] || ''].filter(Boolean).join(' – ')));
        }
        var raceNames = ['5 km', '10 km', 'Półmaraton', 'Maraton'];
        var raceHtml = race.map(function (item, index) {
            return item ? '<div class="act-race-item"><span>' + raceNames[index] + '</span><strong>' +
                esc(S.formatRaceTime(item.value)) + '</strong></div>' : '';
        }).join('');

        fitnessNode.hidden = !tiles.length && !raceHtml;
        if (fitnessNode.hidden) { return; }
        fitnessNode.querySelector('[data-act-fitness-tiles]').innerHTML = tiles.join('');
        var raceNode = fitnessNode.querySelector('[data-act-race]');
        raceNode.hidden = !raceHtml;
        raceNode.innerHTML = raceHtml ? '<h3 class="act-chart-title">Prognozowane czasy biegu</h3><div class="act-race-grid">' + raceHtml + '</div>' : '';

        var block = fitnessNode.querySelector('[data-act-vo2-block]');
        block.hidden = vo2.length < 2;
        state.vo2 = vo2.slice(-60);
        if (!block.hidden && window.AppCharts && window.Chart) {
            vo2Slot = vo2Slot || AppCharts.slot(vo2Chart);
            vo2Slot.redraw();
        }
    }

    function vo2Chart() {
        var points = state.vo2 || [];
        var p = palette();
        if (points.length < 2 || !p) { return null; }
        var color = p.categories[1];
        var options = AppCharts.baseOptions();
        options.plugins.legend.display = false;
        options.plugins.tooltip.displayColors = false;
        options.plugins.tooltip.callbacks = {
            title: function (items) { return items.length ? S.dayLabel(points[items[0].dataIndex].date) : ''; },
            label: function (item) { return 'VO2max ' + num(item.parsed.y, 1); }
        };
        options.plugins.appCrosshair = { color: p.axis };
        options.scales.y.ticks.callback = function (value) { return num(value, 0); };
        return new Chart(fitnessNode.querySelector('[data-act-vo2-chart]'), {
            type: 'line',
            data: {
                labels: points.map(function (point) { return S.shortDayLabel(point.date); }),
                datasets: [{
                    label: 'VO2max', data: points.map(function (point) { return point.value; }),
                    borderColor: color, backgroundColor: color, pointBackgroundColor: color, pointBorderColor: p.surface,
                    borderWidth: 2, pointRadius: 4, pointHoverRadius: 6, pointBorderWidth: 2, tension: 0
                }]
            },
            options: options,
            plugins: [AppCharts.crosshair]
        });
    }

    // --- Regeneracja i zdrowie -----------------------------------------------------
    function renderHealth(ctx) {
        var recent = (ctx.health || []).filter(function (row) {
            return S.dayNumber(ctx.today) - S.dayNumber(row.date) < DAYS;
        });
        var available = METRICS.filter(function (metric) {
            return recent.some(function (row) { return row[metric.key] !== undefined; });
        });
        healthNode.hidden = !available.length;
        if (healthNode.hidden) { return; }

        var latest = function (key) { return S.latestHealth(recent, key); };
        var sleep = latest('sleep');
        var score = latest('sleepScore');
        var rhr = latest('restingHr');
        var hrv = latest('hrv');
        var hrvStatus = latest('hrvStatus');
        var bbHigh = latest('bbHigh');
        var bbLow = latest('bbLow');
        var steps = latest('steps');
        var goal = latest('stepGoal');
        var stress = latest('stress');
        var avg = function (key) {
            return S.average(recent.map(function (row) { return row[key] === undefined ? null : row[key]; }));
        };
        var tiles = [
            sleep ? tile('bi-moon-stars', 'Sen', S.formatSleep(sleep.value),
                [score && score.date === sleep.date ? 'wynik ' + score.value : '', sleep.date === ctx.today ? 'ostatnia noc' : 'noc na ' + whenLabel(sleep.date, ctx.today)].filter(Boolean).join(' · ')) : '',
            rhr ? tile('bi-heart', 'Tętno spoczynkowe', rhr.value + ' bpm', 'średnio ' + num(avg('restingHr')) + ' w 30 dni') : '',
            hrv ? tile('bi-activity', 'HRV (noc)', hrv.value + ' ms',
                hrvStatus && hrvStatus.date === hrv.date ? S.label(S.HRV_STATUS, hrvStatus.value) : whenLabel(hrv.date, ctx.today)) : '',
            bbHigh ? tile('bi-lightning-charge', 'Body Battery', 'maks. ' + bbHigh.value,
                bbLow && bbLow.date === bbHigh.date ? 'min. ' + bbLow.value + ' · ' + whenLabel(bbHigh.date, ctx.today) : whenLabel(bbHigh.date, ctx.today)) : '',
            steps ? tile('bi-signpost-2', 'Kroki', num(steps.value),
                (goal && goal.date === steps.date ? 'cel ' + num(goal.value) + ' · ' : '') + whenLabel(steps.date, ctx.today)) : '',
            stress ? tile('bi-wind', 'Stres (średnio)', String(stress.value), whenLabel(stress.date, ctx.today)) : ''
        ];
        healthNode.querySelector('[data-act-health-tiles]').innerHTML = tiles.join('');

        if (!state.metric) {
            try { state.metric = localStorage.getItem(METRIC_KEY); } catch (e) { /* tryb prywatny */ }
        }
        if (!available.some(function (m) { return m.key === state.metric; })) { state.metric = available[0].key; }
        healthNode.querySelector('[data-act-health-metrics]').innerHTML = available.map(function (metric) {
            var active = metric.key === state.metric;
            return '<button type="button" role="tab" data-metric="' + metric.key + '" class="' + (active ? 'is-active' : '') +
                '" aria-selected="' + active + '">' + esc(metric.label) + '</button>';
        }).join('');
        state.data = { health: ctx.health, today: ctx.today };
        if (window.AppCharts && window.Chart) {
            healthSlot = healthSlot || AppCharts.slot(healthChart);
            healthSlot.redraw();
        }
    }

    function healthChart() {
        var metric = METRICS.filter(function (m) { return m.key === state.metric; })[0];
        var p = palette();
        if (!metric || !state.data || !p) { return null; }
        var series = S.healthSeries(state.data.health, metric.key, state.data.today, DAYS);
        var transform = metric.value || function (v) { return v; };
        var color = p.categories[metric.color];
        var options = AppCharts.baseOptions();
        options.plugins.legend.display = false;
        options.plugins.tooltip.displayColors = false;
        options.plugins.tooltip.callbacks = {
            title: function (items) { return items.length ? S.dayLabel(series[items[0].dataIndex].date, true) : ''; },
            label: function (item) { return metric.label + ': ' + metric.format(item.parsed.y); }
        };
        options.scales.y.ticks.callback = function (value) { return metric.key === 'sleep' ? value + ' h' : num(value); };
        options.scales.y.beginAtZero = metric.type === 'bar';
        var dataset = {
            label: metric.label,
            data: series.map(function (point) { return point.value === null ? null : transform(point.value); }),
            borderColor: color,
            backgroundColor: color
        };
        if (metric.type === 'bar') {
            dataset.borderRadius = 4;
            dataset.maxBarThickness = 14;
        } else {
            options.plugins.appCrosshair = { color: p.axis };
            Object.assign(dataset, { borderWidth: 2, pointRadius: 3, pointHoverRadius: 6, pointBackgroundColor: color, pointBorderColor: p.surface, spanGaps: true, tension: 0 });
        }
        return new Chart(healthNode.querySelector('[data-act-health-chart]'), {
            type: metric.type,
            data: { labels: series.map(function (point) { return S.shortDayLabel(point.date); }), datasets: [dataset] },
            options: options,
            plugins: metric.type === 'line' ? [AppCharts.crosshair] : []
        });
    }

    healthNode.querySelector('[data-act-health-metrics]').addEventListener('click', function (event) {
        var button = event.target.closest('[data-metric]');
        if (!button) { return; }
        state.metric = button.getAttribute('data-metric');
        try { localStorage.setItem(METRIC_KEY, state.metric); } catch (e) { /* tryb prywatny */ }
        Array.prototype.forEach.call(healthNode.querySelectorAll('[data-metric]'), function (node) {
            var active = node === button;
            node.classList.toggle('is-active', active);
            node.setAttribute('aria-selected', String(active));
        });
        if (healthSlot) { healthSlot.redraw(); }
    });

    window.ActivityHealth = {
        render: function (ctx) {
            renderFitness(ctx);
            renderHealth(ctx);
        }
    };
})();
