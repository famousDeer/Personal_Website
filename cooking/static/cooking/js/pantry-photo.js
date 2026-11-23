(() => {
    const dialog = document.querySelector('[data-pantry-photo-dialog]');
    if (!dialog || typeof dialog.showModal !== 'function') {
        return;
    }

    const home = dialog.parentElement;
    const image = dialog.querySelector('img');
    const heading = dialog.querySelector('h2');

    document.addEventListener('click', (event) => {
        const link = event.target.closest('[data-pantry-photo]');
        if (event.defaultPrevented || !link || event.button !== 0 || event.ctrlKey
            || event.metaKey || event.shiftKey || event.altKey || dialog.open) {
            return;
        }

        event.preventDefault();
        // Panel szczegółów utrzymuje fokus wewnątrz siebie.
        (link.closest('[data-pantry-detail]') || home).append(dialog);
        heading.textContent = link.dataset.pantryPhoto;
        image.alt = `Zdjęcie produktu: ${link.dataset.pantryPhoto}`;
        image.src = link.href;
        dialog.showModal();
    });

    dialog.addEventListener('click', (event) => {
        if (event.target === dialog) {
            dialog.close();
        }
    });

    dialog.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            // Escape zamyka zdjęcie, a panel produktu pozostaje otwarty.
            event.stopPropagation();
        }
    });

    dialog.addEventListener('close', () => {
        image.removeAttribute('src');
        home.append(dialog);
    });
})();
