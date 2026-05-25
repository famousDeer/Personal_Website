document.addEventListener('DOMContentLoaded', () => {
    const stepList = document.querySelector('[data-step-list]');
    const addStepButton = document.querySelector('[data-add-step]');
    if (!stepList || !addStepButton) {
        return;
    }

    function syncSteps() {
        const steps = stepList.querySelectorAll('[data-step-card]');
        steps.forEach((step, index) => {
            step.dataset.stepIndex = index;
            const label = step.querySelector('[data-step-label]');
            if (label) {
                label.textContent = `Krok ${index + 1}`;
            }
            const mix = step.querySelector('input[name="step_mix_after"]');
            if (mix) {
                mix.value = String(index);
            }
            step.querySelectorAll('input[name="ingredient_step"]').forEach((input) => {
                input.value = String(index);
            });
        });
        stepList.querySelectorAll('[data-remove-step]').forEach((button) => {
            button.disabled = steps.length === 1;
        });
    }

    function syncIngredientButtons(step) {
        const rows = step.querySelectorAll('[data-ingredient-row]');
        step.querySelectorAll('[data-remove-ingredient]').forEach((button) => {
            button.disabled = rows.length === 1;
        });
    }

    function resetControls(container) {
        container.querySelectorAll('input, textarea, select').forEach((field) => {
            if (field.type === 'checkbox') {
                field.checked = false;
            } else if (field.tagName === 'SELECT') {
                field.selectedIndex = 0;
            } else if (field.type !== 'hidden') {
                field.value = '';
            }
        });
        const unit = container.querySelector('select[name="ingredient_unit"]');
        if (unit) {
            unit.value = 'g';
        }
    }

    function bindIngredientRow(row) {
        const removeButton = row.querySelector('[data-remove-ingredient]');
        if (!removeButton || removeButton.dataset.bound === '1') {
            return;
        }
        removeButton.dataset.bound = '1';
        removeButton.addEventListener('click', () => {
            const step = row.closest('[data-step-card]');
            row.remove();
            syncIngredientButtons(step);
        });
    }

    function bindStep(step) {
        const addIngredientButton = step.querySelector('[data-add-ingredient]');
        const removeStepButton = step.querySelector('[data-remove-step]');
        const ingredientList = step.querySelector('[data-ingredient-list]');

        if (addIngredientButton && addIngredientButton.dataset.bound !== '1') {
            addIngredientButton.dataset.bound = '1';
            addIngredientButton.addEventListener('click', () => {
                const template = ingredientList.querySelector('[data-ingredient-row]');
                const clone = template.cloneNode(true);
                resetControls(clone);
                ingredientList.appendChild(clone);
                bindIngredientRow(clone);
                syncSteps();
                syncIngredientButtons(step);
                clone.querySelector('input[name="ingredient_name"]').focus();
            });
        }

        if (removeStepButton && removeStepButton.dataset.bound !== '1') {
            removeStepButton.dataset.bound = '1';
            removeStepButton.addEventListener('click', () => {
                step.remove();
                syncSteps();
            });
        }

        step.querySelectorAll('[data-ingredient-row]').forEach(bindIngredientRow);
        syncIngredientButtons(step);
    }

    stepList.querySelectorAll('[data-step-card]').forEach(bindStep);
    syncSteps();

    addStepButton.addEventListener('click', () => {
        const template = stepList.querySelector('[data-step-card]');
        const clone = template.cloneNode(true);
        resetControls(clone);
        const ingredientRows = clone.querySelectorAll('[data-ingredient-row]');
        ingredientRows.forEach((row, index) => {
            if (index > 0) {
                row.remove();
            }
        });
        clone.querySelectorAll('[data-bound]').forEach((element) => {
            delete element.dataset.bound;
        });
        stepList.appendChild(clone);
        bindStep(clone);
        syncSteps();
        clone.querySelector('input[name="step_title"]').focus();
    });
});
