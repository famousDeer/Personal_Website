(() => {
    const defaultVisibleSuggestions = 8;

    function normalize(value) {
        return value.trim().toLocaleLowerCase('pl-PL');
    }

    function readSuggestions(sourceId) {
        const dataElement = document.getElementById(sourceId);
        if (!dataElement) {
            return [];
        }
        const suggestions = JSON.parse(dataElement.textContent);
        return Array.isArray(suggestions) ? suggestions : [];
    }

    function getMatches(query, suggestions, visibleLimit) {
        const normalizedQuery = normalize(query);
        const filtered = normalizedQuery
            ? suggestions.filter((item) => normalize(item).includes(normalizedQuery))
            : suggestions;
        const maxVisibleSuggestions = visibleLimit > 0 ? visibleLimit : suggestions.length;

        return filtered
            .sort((first, second) => {
                const firstNormalized = normalize(first);
                const secondNormalized = normalize(second);
                const firstStarts = firstNormalized.startsWith(normalizedQuery);
                const secondStarts = secondNormalized.startsWith(normalizedQuery);
                if (firstStarts === secondStarts) {
                    return firstNormalized.localeCompare(secondNormalized, 'pl-PL');
                }
                return firstStarts ? -1 : 1;
            })
            .slice(0, maxVisibleSuggestions);
    }

    function setupAutocomplete(input) {
        const wrapper = input.closest('.finance-autocomplete');
        const menu = wrapper ? wrapper.querySelector('[data-autocomplete-menu]') : null;
        const suggestions = readSuggestions(input.dataset.autocompleteSource);
        const iconClass = input.dataset.autocompleteIcon || 'bi-list-ul';
        const visibleLimit = Number.parseInt(input.dataset.autocompleteLimit || defaultVisibleSuggestions, 10);
        if (!wrapper || !menu || !suggestions.length) {
            return;
        }

        let matches = [];
        let activeIndex = -1;

        function closeMenu() {
            menu.classList.add('d-none');
            input.setAttribute('aria-expanded', 'false');
            input.removeAttribute('aria-activedescendant');
            activeIndex = -1;
        }

        function setActive(index) {
            activeIndex = index;
            if (activeIndex < 0) {
                input.removeAttribute('aria-activedescendant');
            }
            menu.querySelectorAll('.finance-autocomplete-option').forEach((option, optionIndex) => {
                const isActive = optionIndex === activeIndex;
                option.classList.toggle('is-active', isActive);
                option.setAttribute('aria-selected', isActive ? 'true' : 'false');
                if (isActive) {
                    input.setAttribute('aria-activedescendant', option.id);
                    option.scrollIntoView({ block: 'nearest' });
                }
            });
        }

        function selectValue(value) {
            input.value = value;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            closeMenu();
            input.focus();
        }

        function renderMenu() {
            menu.innerHTML = '';
            matches.forEach((value, index) => {
                const option = document.createElement('button');
                option.type = 'button';
                option.id = `${menu.id}-option-${index}`;
                option.className = 'finance-autocomplete-option';
                option.setAttribute('role', 'option');
                option.setAttribute('aria-selected', 'false');
                option.tabIndex = -1;

                const icon = document.createElement('i');
                icon.className = `bi ${iconClass}`;
                icon.setAttribute('aria-hidden', 'true');

                const label = document.createElement('span');
                label.textContent = value;

                option.append(icon, label);
                option.addEventListener('mousedown', (event) => event.preventDefault());
                option.addEventListener('click', () => selectValue(value));
                menu.append(option);
            });
        }

        function openMenu() {
            matches = getMatches(input.value, suggestions, visibleLimit);
            if (!matches.length) {
                closeMenu();
                return;
            }
            renderMenu();
            menu.classList.remove('d-none');
            input.setAttribute('aria-expanded', 'true');
            setActive(-1);
        }

        input.addEventListener('focus', openMenu);
        input.addEventListener('input', openMenu);
        input.addEventListener('blur', () => {
            window.setTimeout(closeMenu, 120);
        });

        input.addEventListener('keydown', (event) => {
            const isOpen = !menu.classList.contains('d-none');
            if (event.key === 'ArrowDown') {
                event.preventDefault();
                if (!isOpen) {
                    openMenu();
                    return;
                }
                setActive((activeIndex + 1) % matches.length);
            } else if (event.key === 'ArrowUp' && isOpen) {
                event.preventDefault();
                setActive((activeIndex - 1 + matches.length) % matches.length);
            } else if (event.key === 'Enter' && isOpen && activeIndex >= 0) {
                event.preventDefault();
                selectValue(matches[activeIndex]);
            } else if (event.key === 'Escape' && isOpen) {
                event.preventDefault();
                closeMenu();
            }
        });

        document.addEventListener('pointerdown', (event) => {
            if (!wrapper.contains(event.target)) {
                closeMenu();
            }
        });
    }

    function init() {
        document.querySelectorAll('[data-autocomplete-input]').forEach(setupAutocomplete);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
