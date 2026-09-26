/* =========================================================================
   Wspólna konfiguracja Chart.js
   =========================================================================
   Wykresy czytają kolory z tokenów CSS, a nie z domyślnej palety Chart.js.
   Dzięki temu słupek "wydatki" ma ten sam kolor co kwota wydatku w tabeli,
   a zmiana motywu przemalowuje wszystko jednym zdarzeniem.

   Zasady, których trzymają się wykresy w tej aplikacji:
   - jedna oś wartości; nigdy dwie skale na jednym wykresie,
   - kolor niesie tożsamość serii, nigdy jej pozycję w rankingu,
   - tekst (etykiety, osie, legenda) nosi kolory tekstu, nie kolory serii,
   - siatka i osie są wycofane, dane są na pierwszym planie.
   ========================================================================= */
(function (global) {
    'use strict';

    function token(name, fallback) {
        var value = getComputedStyle(document.documentElement).getPropertyValue(name);
        return (value && value.trim()) || fallback;
    }

    /**
     * Celownik: pionowa linia 1 px w kolorze osi pod wskazanym punktem.
     * Rysowana pod seriami, żeby nie przecinała punktów, i tylko wtedy,
     * gdy widać podpowiedź - po zjechaniu kursorem znika razem z nią.
     */
    var crosshair = {
        id: 'appCrosshair',
        beforeDatasetsDraw: function (chart, args, options) {
            var tooltip = chart.tooltip;
            var active = tooltip && tooltip.getActiveElements ? tooltip.getActiveElements() : [];
            if (!active.length) { return; }
            var area = chart.chartArea;
            var x = Math.round(active[0].element.x) + 0.5;
            var ctx = chart.ctx;
            ctx.save();
            ctx.beginPath();
            ctx.moveTo(x, area.top);
            ctx.lineTo(x, area.bottom);
            ctx.lineWidth = 1;
            ctx.strokeStyle = (options && options.color) || '#94a3b8';
            ctx.stroke();
            ctx.restore();
        }
    };

    var AppCharts = {
        registry: [],

        /** Bieżące kolory z tokenów - czytane przy każdym rysowaniu. */
        palette: function () {
            return {
                income: token('--income', '#14a374'),
                expense: token('--expense', '#d93a2b'),
                invest: token('--invest', '#2a78d6'),
                grid: token('--chart-grid', 'rgba(15,23,42,.08)'),
                axis: token('--chart-axis', '#94a3b8'),
                ink1: token('--ink-1', '#0f172a'),
                ink3: token('--ink-3', '#64748b'),
                surface: token('--chart-surface', '#ffffff'),
                tooltipBg: token('--chart-tooltip-bg', '#0f172a'),
                tooltipInk: token('--chart-tooltip-ink', '#f8fafc'),
                tooltipLine: token('--chart-tooltip-line', 'rgba(255,255,255,.12)'),
                categories: [
                    token('--cat-1', '#2a78d6'), token('--cat-2', '#eb6834'),
                    token('--cat-3', '#1baf7a'), token('--cat-4', '#eda100'),
                    token('--cat-5', '#e87ba4'), token('--cat-6', '#008300'),
                    token('--cat-7', '#4a3aa7'), token('--cat-8', '#e34948')
                ]
            };
        },

        /** Skrót dużych liczb na osi: "12,5 tys.", "1,2 mln" (z polskim przecinkiem). */
        compact: function (value) {
            return new Intl.NumberFormat('pl-PL', {
                notation: 'compact', maximumFractionDigits: 1
            }).format(value || 0);
        },

        money: function (value) {
            return new Intl.NumberFormat('pl-PL', {
                style: 'currency', currency: 'PLN', maximumFractionDigits: 0
            }).format(value || 0);
        },

        /** Wspólne ustawienia osi, siatki, legendy i podpowiedzi. */
        baseOptions: function () {
            var p = AppCharts.palette();
            return {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: {
                        display: true,
                        position: 'top',
                        align: 'start',
                        labels: {
                            boxWidth: 10,
                            boxHeight: 10,
                            usePointStyle: true,
                            pointStyle: 'rectRounded',
                            color: p.ink3,
                            font: { size: 12 },
                            padding: 16
                        }
                    },
                    tooltip: {
                        // Kolory z tokenów dymka, nie z --ink-1 i nie na sztywno.
                        // Tekst zakodowany jako #fff czytał się tylko dopóki tło
                        // było ciemne - po zmianie motywu zostawał biały na białym.
                        backgroundColor: p.tooltipBg,
                        titleColor: p.tooltipInk,
                        bodyColor: p.tooltipInk,
                        borderColor: p.tooltipLine,
                        borderWidth: 1,
                        padding: 10,
                        cornerRadius: 8,
                        displayColors: true,
                        boxWidth: 8,
                        boxHeight: 8,
                        usePointStyle: true,
                        callbacks: {
                            label: function (ctx) {
                                return ' ' + ctx.dataset.label + ': ' + AppCharts.money(ctx.parsed.y ?? ctx.parsed);
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        grid: { display: false },
                        border: { color: p.grid },
                        ticks: { color: p.axis, font: { size: 11 }, maxRotation: 0, autoSkipPadding: 12 }
                    },
                    y: {
                        grid: { color: p.grid, drawTicks: false },
                        border: { display: false },
                        ticks: {
                            color: p.axis,
                            font: { size: 11 },
                            padding: 8,
                            callback: function (value) {
                                if (Math.abs(value) >= 1000) { return AppCharts.compact(value); }
                                return value + ' zł';
                            }
                        }
                    }
                }
            };
        },

        crosshair: crosshair,

        /** "16 wrz" - dzień z miesiącem do tytułu podpowiedzi i tabeli. */
        dayLabel: function (year, month, day) {
            return new Intl.DateTimeFormat('pl-PL', { day: 'numeric', month: 'short' })
                .format(new Date(year, month - 1, day));
        },

        /**
         * Opis wykresu dla czytnika ekranu. Canvas sam w sobie jest dla
         * niego pustym obrazkiem, więc dostaje rolę "img" i jedno zdanie
         * z tym, co z wykresu wynika.
         */
        describe: function (canvas, text) {
            canvas.setAttribute('role', 'img');
            canvas.setAttribute('aria-label', text);
        },

        /**
         * Bliźniacza tabela pod wykresem: <details> z pustym ciałem, które
         * wypełnia się przy pierwszym otwarciu tymi samymi danymi co wykres.
         * build() zwraca { caption, columns: [...], rows: [[...]], foot: [...] };
         * pierwsza kolumna to etykieta wiersza, pozostałe to kwoty.
         */
        twinTable: function (details, build) {
            if (!details) { return; }
            var body = details.querySelector('.u-chart-data-body');
            var render = function () {
                var spec = build();
                var table = document.createElement('table');
                table.className = 'u-table';
                if (spec.caption) {
                    var caption = document.createElement('caption');
                    caption.className = 'visually-hidden';
                    caption.textContent = spec.caption;
                    table.appendChild(caption);
                }
                var addRow = function (section, cells, header) {
                    var tr = document.createElement('tr');
                    cells.forEach(function (cell, index) {
                        var el = document.createElement(header || index === 0 ? 'th' : 'td');
                        if (header) { el.scope = 'col'; } else if (index === 0) { el.scope = 'row'; }
                        if (index > 0) { el.className = 'u-amount'; }
                        el.textContent = cell;
                        tr.appendChild(el);
                    });
                    section.appendChild(tr);
                };
                var head = table.createTHead();
                addRow(head, spec.columns, true);
                var tbody = table.createTBody();
                spec.rows.forEach(function (row) { addRow(tbody, row); });
                if (spec.foot) { addRow(table.createTFoot(), spec.foot); }
                body.replaceChildren(table);
            };
            details.addEventListener('toggle', function () {
                if (details.open && !body.firstChild) { render(); }
            });
            if (details.open) { render(); }
        },

        /**
         * Do którego dnia rysować przebieg miesiąca: do dziś, bo dalej linia
         * byłaby płaska i wyglądała jak "reszta miesiąca bez wydatków".
         * Wpis z datą w przyszłości (np. zaplanowany rachunek) przesuwa
         * koniec do swojego dnia, żeby wykres zgadzał się z kafelkami.
         */
        monthCutoff: function (data) {
            var cutoff = data.daysPassed == null ? data.days.length : data.daysPassed;
            [data.incomes, data.expenses, data.investments].forEach(function (arr) {
                (arr || []).forEach(function (v, i) { if (v && i + 1 > cutoff) { cutoff = i + 1; } });
            });
            return Math.min(cutoff, data.days.length);
        },

        /**
         * Skumulowany przebieg miesiąca.
         *
         * Zastępuje wykres słupkowy dzień po dniu, na którym jeden słupek
         * wypłaty (8400 zł) rozciągał oś do 9000 zł i spłaszczał wszystkie
         * wydatki do niewidocznych kresek. Ujęcie skumulowane trzyma obie
         * serie w tym samym zakresie i odpowiada na pytanie, które dashboard
         * naprawdę zadaje: jak miesiąc wypada na tle wpływów.
         */
        cumulativeMonth: function (canvas, data) {
            var p = AppCharts.palette();
            var running = function (arr) {
                var total = 0;
                return arr.map(function (v) { total += (v || 0); return total; });
            };

            var passed = AppCharts.monthCutoff(data);
            var untilToday = function (arr) {
                return arr.map(function (v, i) { return i < passed ? v : null; });
            };

            var series = [
                { label: 'Przychody', values: untilToday(running(data.incomes)), color: p.income },
                { label: 'Wydatki', values: untilToday(running(data.expenses)), color: p.expense },
                { label: 'Inwestycje', values: untilToday(running(data.investments)), color: p.invest }
            ].filter(function (s) { return s.values.some(function (v) { return v > 0; }); });

            var options = AppCharts.baseOptions();
            options.plugins.appCrosshair = { color: p.axis };
            options.plugins.tooltip.callbacks.title = function (items) {
                if (!items.length || !data.year) { return items.length ? items[0].label : ''; }
                return AppCharts.dayLabel(data.year, data.month, Number(items[0].label));
            };

            return new global.Chart(canvas, {
                type: 'line',
                data: {
                    labels: data.days,
                    datasets: series.map(function (s) {
                        // Wypełnienie tylko pod wydatkami: dwa półprzezroczyste
                        // pola (wydatki i inwestycje) nakładały się w trzeci,
                        // fioletowy kolor, którego nie ma w legendzie.
                        var filled = s.label === 'Wydatki';
                        return {
                            label: s.label,
                            data: s.values,
                            borderColor: s.color,
                            backgroundColor: filled ? s.color + '1f' : s.color,
                            // Legenda i podpowiedź biorą kolor z punktu, nie
                            // z wypełnienia - bez tego kwadracik "Wydatki"
                            // byłby blady, a pozostałe pełne.
                            pointBackgroundColor: s.color,
                            pointBorderColor: s.color,
                            borderWidth: 2,
                            pointRadius: 0,
                            pointHoverRadius: 5,
                            pointHoverBorderWidth: 2,
                            pointHoverBorderColor: p.surface,
                            pointHoverBackgroundColor: s.color,
                            // Suma narastająca nigdy nie maleje; zwykłe wygładzenie
                            // (tension) potrafiło narysować przestrzał nad 8400 zł
                            // i dołek, którego w danych nie ma.
                            cubicInterpolationMode: 'monotone',
                            fill: filled ? 'origin' : false
                        };
                    })
                },
                options: options,
                plugins: [crosshair]
            });
        },

        /** Rejestruje wykres, żeby przemalować go po zmianie motywu. */
        register: function (factory) {
            var entry = { factory: factory, instance: null };
            entry.instance = factory();
            AppCharts.registry.push(entry);
            return entry.instance;
        },

        /**
         * Miejsce na wykres, którego dane się zmieniają (np. po wyborze
         * zakresu dat). redraw() rysuje go od nowa z bieżącymi danymi,
         * a zmiana motywu przemalowuje go razem z pozostałymi. Fabryka może
         * zwrócić null, gdy nie ma czego rysować.
         */
        slot: function (factory) {
            var entry = { factory: factory, instance: null };
            AppCharts.registry.push(entry);
            return {
                redraw: function () {
                    if (entry.instance) { entry.instance.destroy(); }
                    entry.instance = entry.factory();
                    return entry.instance;
                },
                clear: function () {
                    if (entry.instance) { entry.instance.destroy(); }
                    entry.instance = null;
                },
                instance: function () { return entry.instance; }
            };
        },

        repaintAll: function () {
            // Po zmianie motywu wykres ma tylko zmienić kolory. Bez tego
            // słupki i linie rosłyby od zera jak przy pierwszym wejściu.
            var Chart = global.Chart;
            var saved = Chart && Chart.defaults.animation;
            if (Chart) { Chart.defaults.animation = false; }
            try {
                AppCharts.registry.forEach(function (entry) {
                    if (entry.instance) { entry.instance.destroy(); }
                    entry.instance = entry.factory();
                });
            } finally {
                if (Chart) { Chart.defaults.animation = saved; }
            }
        }
    };

    document.addEventListener('themechange', function () {
        // Krótka zwłoka, żeby przeglądarka zdążyła policzyć nowe wartości tokenów.
        setTimeout(AppCharts.repaintAll, 30);
    });

    global.AppCharts = AppCharts;
})(window);
