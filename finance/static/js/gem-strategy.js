/* =========================================================================
   Panel strategii GEM (finance/partials/gem_panel.html)
   =========================================================================
   - Po wejściu na stronę, gdy serwer oznaczy panel data-gem-auto="1"
     (od zamknięcia ostatniej sesji nie było udanego pobrania), skrypt
     w tle prosi o nowe notowania i podmienia panel. Strona nie czeka.
   - „Odśwież” robi to samo na żądanie, bez przeładowania strony.
   - Wykres stóp zwrotu z 12 mies. (Chart.js + wspólne AppCharts z charts.js),
     z tabelą pod spodem; przerysowuje się po zmianie motywu.
   Bez JavaScriptu formularz „Odśwież” działa zwykłym POST-em.
   ========================================================================= */
(function () {
    'use strict';

    // ---------------------------------------------------------------------
    // Wykres: stopa zwrotu od początku okna 12 mies. (jak wykres w skrypcie
    // z yfinance). Kolor stały dla funduszu (kolejność GEM_ASSETS), IB01
    // linią przerywaną, etykieta z wynikiem na końcu każdej linii.
    // ---------------------------------------------------------------------
    const pct = (value, digits = 1) => {
        const text = Math.abs(value).toLocaleString('pl-PL', { minimumFractionDigits: digits, maximumFractionDigits: digits });
        if (Number(value.toFixed(digits)) === 0) {
            return `${(0).toLocaleString('pl-PL', { minimumFractionDigits: digits })}%`;
        }
        return `${value > 0 ? '+' : '−'}${text}%`;
    };
    const parseDay = (iso) => {
        const [year, month, day] = iso.split('-').map(Number);
        return new Date(year, month - 1, day);
    };
    const dayFormat = new Intl.DateTimeFormat('pl-PL', { day: 'numeric', month: 'short', year: 'numeric' });
    const monthFormat = new Intl.DateTimeFormat('pl-PL', { month: 'short', year: '2-digit' });

    const endLabels = {
        id: 'gemEndLabels',
        afterDatasetsDraw(chart, args, options) {
            if (!options || !options.enabled) {
                return;
            }
            const { ctx, chartArea } = chart;
            const items = chart.data.datasets.map((dataset, index) => {
                const meta = chart.getDatasetMeta(index);
                const point = meta.data[meta.data.length - 1];
                if (!point || meta.hidden) {
                    return null;
                }
                return {
                    y: point.y,
                    x: point.x,
                    color: dataset.borderColor,
                    text: `${dataset.label} ${pct(dataset.data[dataset.data.length - 1])}`,
                };
            }).filter(Boolean).sort((a, b) => a.y - b.y);
            // Etykiety nie nachodzą na siebie: najmniej 15 px odstępu.
            for (let i = 1; i < items.length; i += 1) {
                items[i].y = Math.max(items[i].y, items[i - 1].y + 15);
            }
            const overflow = items.length ? items[items.length - 1].y - chartArea.bottom : 0;
            if (overflow > 0) {
                items.forEach((item) => { item.y -= overflow; });
            }
            ctx.save();
            ctx.font = '600 12px system-ui, -apple-system, "Segoe UI", sans-serif';
            ctx.textBaseline = 'middle';
            items.forEach((item) => {
                ctx.fillStyle = item.color;
                ctx.beginPath();
                ctx.arc(item.x + 9, item.y, 3.5, 0, Math.PI * 2);
                ctx.fill();
                ctx.fillStyle = options.color;
                ctx.fillText(item.text, item.x + 17, item.y);
            });
            ctx.restore();
        },
    };

    function buildChart() {
        const canvas = document.querySelector('[data-gem-panel] [data-gem-chart]');
        const node = document.getElementById('gemChartData');
        if (!canvas || !node || !window.Chart || !window.AppCharts) {
            return null;
        }
        const data = JSON.parse(node.textContent);
        const charts = window.AppCharts;
        const p = charts.palette();
        const wide = canvas.parentElement.clientWidth >= 560;
        const options = charts.baseOptions();
        options.layout = { padding: { right: wide ? 112 : 4, top: 4 } };
        options.plugins.appCrosshair = { color: p.axis };
        options.plugins.gemEndLabels = { enabled: wide, color: p.ink1 };
        options.plugins.tooltip.itemSort = (a, b) => b.parsed.y - a.parsed.y;
        options.plugins.tooltip.callbacks = {
            title: (items) => (items.length ? dayFormat.format(parseDay(data.labels[items[0].dataIndex])) : ''),
            label: (ctx) => ` ${ctx.dataset.label}: ${pct(ctx.parsed.y, 2)}`,
        };
        options.scales.x.ticks.maxTicksLimit = wide ? 8 : 4;
        options.scales.x.ticks.callback = function (value) {
            return monthFormat.format(parseDay(this.getLabelForValue(value)));
        };
        options.scales.y.ticks.callback = (value) => pct(value, 0);
        options.scales.y.grid.color = (ctx) => (ctx.tick && ctx.tick.value === 0 ? p.axis : p.grid);
        if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
            options.animation = false;
        }

        const datasets = data.datasets.map((item, index) => {
            const color = p.categories[index % p.categories.length];
            return {
                label: item.ticker,
                data: item.values,
                borderColor: color,
                backgroundColor: color,
                pointBackgroundColor: color,
                pointBorderColor: color,
                borderWidth: 2,
                borderDash: item.safe ? [6, 4] : [],
                pointRadius: 0,
                pointHoverRadius: 4,
                pointHoverBorderWidth: 2,
                pointHoverBorderColor: p.surface,
                tension: 0,
                fill: false,
            };
        });
        const best = data.datasets
            .map((item) => ({ ticker: item.ticker, value: item.values[item.values.length - 1] }))
            .sort((a, b) => b.value - a.value);
        charts.describe(canvas, `Stopa zwrotu z 12 miesięcy dzień po dniu. Na koniec: ${best.map((item) => `${item.ticker} ${pct(item.value)}`).join(', ')}.`);

        const table = document.getElementById('gemChartTable');
        if (table && !table.dataset.gemBound) {
            table.dataset.gemBound = '1';
            charts.twinTable(table, () => ({
                caption: 'Stopa zwrotu od początku okna 12 miesięcy na koniec każdego miesiąca',
                columns: ['Sesja', ...data.datasets.map((item) => item.ticker)],
                rows: data.table_rows.map((index) => [
                    dayFormat.format(parseDay(data.labels[index])),
                    ...data.datasets.map((item) => pct(item.values[index], 2)),
                ]),
            }));
        }

        return new window.Chart(canvas, {
            type: 'line',
            data: { labels: data.labels, datasets },
            options,
            plugins: [charts.crosshair, endLabels],
        });
    }

    let chartSlot = null;
    function drawChart() {
        if (!window.AppCharts) {
            return;
        }
        if (!chartSlot) {
            chartSlot = window.AppCharts.slot(buildChart);
        }
        chartSlot.redraw();
    }

    function refresh(panel, mode) {
        const form = panel.querySelector('[data-gem-refresh]');
        if (!form || panel.dataset.gemBusy === '1') {
            return;
        }
        const button = form.querySelector('[data-gem-refresh-button]');
        const label = form.querySelector('[data-gem-refresh-label]');
        const data = new FormData(form);
        data.set('mode', mode);
        panel.dataset.gemBusy = '1';
        panel.setAttribute('aria-busy', 'true');
        if (button) {
            button.disabled = true;
        }
        if (label) {
            label.textContent = 'Pobieram…';
        }

        fetch(form.action, {
            method: 'POST',
            body: data,
            headers: { 'X-Requested-With': 'fetch' },
            credentials: 'same-origin',
        })
            .then((response) => (response.ok ? response.json() : Promise.reject(new Error(String(response.status)))))
            .then((payload) => {
                const template = document.createElement('template');
                template.innerHTML = payload.html.trim();
                const fresh = template.content.firstElementChild;
                if (!fresh) {
                    throw new Error('empty');
                }
                // Nowy panel nie odświeża się sam drugi raz w tej wizycie.
                fresh.removeAttribute('data-gem-auto');
                panel.replaceWith(fresh);
                bind(fresh);
                drawChart();
            })
            .catch(() => {
                delete panel.dataset.gemBusy;
                panel.removeAttribute('aria-busy');
                if (button) {
                    button.disabled = false;
                }
                if (label) {
                    label.textContent = mode === 'auto' ? 'Odśwież' : 'Spróbuj ponownie';
                }
            });
    }

    function bind(panel) {
        const form = panel.querySelector('[data-gem-refresh]');
        if (form) {
            form.addEventListener('submit', (event) => {
                event.preventDefault();
                refresh(panel, 'manual');
            });
        }
    }

    function init() {
        drawChart();
        document.querySelectorAll('[data-gem-panel]').forEach((panel) => {
            bind(panel);
            if (panel.dataset.gemAuto === '1') {
                refresh(panel, 'auto');
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
