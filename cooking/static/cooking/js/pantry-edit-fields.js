function initPantryEditFields(scope = document) {
    const form = scope.querySelector('[data-pantry-edit-form]');
    const metaElement = scope.querySelector('#pantry-unit-meta');
    if (!form || !metaElement) {
        return;
    }
    const unitMeta = JSON.parse(metaElement.textContent);
    const unitSelect = form.querySelector('[data-pantry-unit-select]');
    const unitLabels = form.querySelectorAll('[data-pantry-unit-label]');
    const unitNote = form.querySelector('[data-pantry-unit-note]');
    const quantityInputs = form.querySelectorAll('[data-pantry-quantity-input]');
    const sizeInput = form.querySelector('[data-pantry-package-size]');
    const countInput = form.querySelector('[data-pantry-package-count]');
    const totalInput = form.querySelector('[data-pantry-total-quantity]');
    const barcodeInput = form.querySelector('[data-pantry-barcode]');
    const imageInput = form.querySelector('#pantry-product-image');
    const imagePicker = form.querySelector('label[for="pantry-product-image"]');
    const removeImage = form.querySelector('[data-pantry-remove-image]');
    const originalUnit = form.dataset.originalUnit;
    // Punkt odniesienia do przeliczeń: jednostka i liczby, które użytkownik
    // ostatnio widział w przeliczalnej mierze. Przejście na miarę nieprzeliczalną
    // (np. ml -> opak.) zostawia liczby, ale nie zmienia punktu odniesienia,
    // więc powrót do ml odtwarza 750 zamiast zostawić 0.75.
    let baseline = null;

    const round2 = (value) => Math.round(value * 100) / 100;
    const numberOf = (input) => Number(String(input.value).replace(',', '.'));

    function tracksPackages() {
        return Boolean(barcodeInput.value.trim())
            || ['szt', 'opak'].includes(unitSelect.value)
            || numberOf(countInput) > 0;
    }

    function syncStep() {
        const integerOnly = unitSelect.value === 'szt';
        quantityInputs.forEach((input) => {
            input.step = integerOnly ? '1' : '0.01';
        });
        sizeInput.min = integerOnly ? '1' : '0.01';
        unitLabels.forEach((label) => {
            label.textContent = unitMeta[unitSelect.value]?.label || unitSelect.value;
        });
    }

    function describeUnitChange() {
        const from = unitMeta[originalUnit];
        const to = unitMeta[unitSelect.value];
        if (!from || !to || originalUnit === unitSelect.value) {
            unitNote.hidden = true;
            return;
        }
        unitNote.hidden = false;
        if (from.group === to.group) {
            unitNote.className = 'alert alert-info small py-2 mt-3 mb-0';
            unitNote.textContent = `Ilości przeliczone z ${from.label} na ${to.label}. `
                + 'Po zapisaniu przeliczę też historię ruchów, więc prognoza zostanie poprawna.';
        } else {
            unitNote.className = 'alert alert-warning small py-2 mt-3 mb-0';
            unitNote.textContent = `${from.label} i ${to.label} to różne miary, więc liczby zostają bez zmian `
                + '– popraw je, jeśli trzeba. Historia ruchów zostanie w dotychczasowych liczbach, '
                + 'a prognoza może przez jakiś czas być mniej dokładna.';
        }
    }

    function rememberBaseline() {
        baseline = {
            unit: unitSelect.value,
            values: Array.from(quantityInputs, (input) => input.value),
        };
    }

    unitSelect.addEventListener('change', () => {
        const from = unitMeta[baseline.unit];
        const to = unitMeta[unitSelect.value];
        if (from && to && from.group === to.group) {
            const factor = Number(from.factor) / Number(to.factor);
            quantityInputs.forEach((input, index) => {
                const value = Number(String(baseline.values[index]).replace(',', '.'));
                if (baseline.values[index] !== '' && Number.isFinite(value)) {
                    input.value = String(round2(value * factor));
                }
            });
        }
        syncStep();
        describeUnitChange();
    });
    quantityInputs.forEach((input) => input.addEventListener('input', rememberBaseline));

    function totalFromCount() {
        const count = numberOf(countInput);
        const size = numberOf(sizeInput);
        if (Number.isFinite(count) && Number.isFinite(size) && size > 0 && tracksPackages()) {
            totalInput.value = String(round2(count * size));
        }
    }

    countInput.addEventListener('input', () => {
        totalFromCount();
        rememberBaseline();
    });
    sizeInput.addEventListener('input', () => {
        totalFromCount();
        rememberBaseline();
    });
    totalInput.addEventListener('input', () => {
        const total = numberOf(totalInput);
        const size = numberOf(sizeInput);
        if (Number.isFinite(total) && Number.isFinite(size) && size > 0 && tracksPackages()) {
            countInput.value = String(Math.max(0, Math.ceil(total / size - 1e-9)));
        }
    });

    imageInput?.addEventListener('change', () => {
        const [file] = imageInput.files;
        if (file && imagePicker) {
            imagePicker.querySelector('strong').textContent = 'Nowe zdjęcie gotowe';
            imagePicker.querySelector('small').textContent = file.name;
            imagePicker.classList.add('has-file');
            if (removeImage) {
                removeImage.checked = false;
            }
        }
    });

    rememberBaseline();
    syncStep();
    describeUnitChange();
}

document.addEventListener('DOMContentLoaded', () => initPantryEditFields());
