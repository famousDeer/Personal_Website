/* Analiza domowa (finance/household_analysis.html):
   - wykres: wydatki niezbędne, pozostałe i oszczędności (słupki skumulowane)
     oraz przychody (linia) w jednej skali złotych, z tabelą pod spodem,
   - kliknięcie miesiąca otwiera jego szczegóły (finance:household_month),
   - zmiana rodzaju kategorii pokazuje przycisk „Zapisz i przelicz”. */
(function () {
    'use strict';

    const form = document.getElementById('kategorie');
    if (form) {
        const save = form.querySelector('[data-ha-save]');
        form.addEventListener('change', (event) => {
            if (event.target.matches('select') && save) {
                save.hidden = false;
            }
        });
    }

    const canvas = document.getElementById('haChart');
    const node = document.getElementById('haChartData');
    if (!canvas || !node || !window.Chart || !window.AppCharts) {
        return;
    }
    const data = JSON.parse(node.textContent);
    const charts = window.AppCharts;
    const monthLabel = (iso) => {
        const [year, month] = iso.split('-').map(Number);
        return new Intl.DateTimeFormat('pl-PL', { month: 'short', year: 'numeric' }).format(new Date(year, month - 1, 1));
    };
    const zl = (value) => `${Math.round(value).toLocaleString('pl-PL')} zł`;
    const labels = data.labels.map(monthLabel);

    function build() {
        const p = charts.palette();
        const options = charts.baseOptions();
        options.scales.x.stacked = true;
        options.plugins.legend.labels.sort = (a, b) => a.datasetIndex - b.datasetIndex;
        options.scales.y.stacked = true;
        options.plugins.tooltip.callbacks.label = (ctx) => ` ${ctx.dataset.label}: ${zl(ctx.parsed.y)}`;
        options.plugins.tooltip.callbacks.footer = (items) => {
            const spent = items.filter((item) => item.dataset.stack === 'out').reduce((sum, item) => sum + item.parsed.y, 0);
            return spent ? `Wydatki i oszczędności: ${zl(spent)}` : '';
        };
        // Kliknięcie miesiąca (słupka albo kolumny) otwiera jego szczegóły.
        options.onClick = (event, elements, chart) => {
            const hit = elements.length ? elements : chart.getElementsAtEventForMode(event, 'index', { intersect: false }, false);
            const url = hit.length ? (data.urls || [])[hit[0].index] : '';
            if (url) {
                window.location.href = url;
            }
        };
        options.onHover = (event, elements, chart) => {
            chart.canvas.style.cursor = elements.length || chart.getElementsAtEventForMode(event, 'index', { intersect: false }, false).length ? 'pointer' : '';
        };
        options.plugins.tooltip.callbacks.afterFooter = () => 'Kliknij, żeby zobaczyć szczegóły';
        if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
            options.animation = false;
        }
        const bar = (label, values, color) => ({
            type: 'bar', label, data: values, stack: 'out', backgroundColor: color, borderColor: p.surface,
            borderWidth: { top: 2 }, borderSkipped: 'bottom', borderRadius: 4, maxBarThickness: 44,
        });
        return new window.Chart(canvas, {
            data: {
                labels,
                datasets: [
                    bar('Niezbędne', data.essential, p.categories[0]),
                    bar('Pozostałe', data.lifestyle, p.categories[1]),
                    bar('Oszczędności', data.savings, p.categories[2]),
                    {
                        type: 'line', label: 'Przychody', data: data.income, stack: 'in',
                        borderColor: p.ink1, backgroundColor: p.ink1, pointBackgroundColor: p.ink1,
                        borderWidth: 2, borderDash: [6, 4], pointRadius: 4, pointHoverRadius: 6, tension: 0,
                        order: -1,
                    },
                ],
            },
            options,
            plugins: [charts.crosshair],
        });
    }

    charts.register(build);
    charts.twinTable(document.getElementById('haChartTable'), () => ({
        caption: 'Wydatki według rodzaju i przychody w kolejnych miesiącach',
        columns: ['Miesiąc', 'Niezbędne', 'Pozostałe', 'Oszczędności', 'Przychody'],
        rows: labels.map((label, index) => [
            label, zl(data.essential[index]), zl(data.lifestyle[index]), zl(data.savings[index]), zl(data.income[index]),
        ]),
    }));
}());
