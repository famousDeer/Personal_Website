(() => {
    'use strict';

    const root = document.querySelector('#pantry-scanner');
    if (!root) {
        return;
    }

    const select = (selector, context = root) => context.querySelector(selector);
    const selectAll = (selector, context = root) => Array.from(context.querySelectorAll(selector));
    const modalElement = select('#pantryScannerModal');
    const video = select('[data-scanner-video]');
    if (!modalElement || !video) {
        return;
    }

    const elements = {
        cameraStage: select('[data-camera-stage]'),
        cameraPlaceholder: select('[data-camera-placeholder]'),
        cameraStatus: select('[data-camera-status]'),
        cameraStatusText: select('[data-camera-status-text]'),
        cameraAlert: select('[data-camera-alert]'),
        manualDetails: select('[data-manual-details]'),
        manualForm: select('[data-manual-code-form]'),
        manualCode: select('[data-manual-code]'),
        codePhoto: select('[data-code-photo]'),
        live: select('[data-scanner-live]'),
        knownIcon: select('[data-known-icon]'),
        knownImageWrap: select('[data-known-image-wrap]'),
        knownImage: select('[data-known-image]'),
        knownName: select('[data-known-name]'),
        knownPackageStock: select('[data-known-package-stock]'),
        knownStock: select('[data-known-stock]'),
        knownScanQuantity: select('[data-known-scan-quantity]'),
        knownConsumeLabel: select('[data-known-consume-label]'),
        knownPurchaseLabel: select('[data-known-purchase-label]'),
        knownCount: select('[data-known-count]'),
        knownCatalogMeta: select('[data-known-catalog-meta]'),
        knownDescription: select('[data-known-description]'),
        knownSource: select('[data-known-source]'),
        knownPhotoWrap: select('[data-known-photo-wrap]'),
        unknownIcon: select('[data-unknown-icon]'),
        unknownImageWrap: select('[data-unknown-image-wrap]'),
        unknownImage: select('[data-unknown-image]'),
        unknownEyebrow: select('[data-unknown-eyebrow]'),
        unknownTitle: select('[data-unknown-title]'),
        unknownMeta: select('[data-unknown-meta]'),
        unknownDescription: select('[data-unknown-description]'),
        unknownSource: select('[data-unknown-source]'),
        unknownCountdownLabel: select('[data-unknown-countdown-label]'),
        unknownHelp: select('[data-unknown-help]'),
        unknownCode: select('[data-unknown-code]'),
        unknownPhotoWrap: select('[data-unknown-photo-wrap]'),
        registerForm: select('[data-register-form]'),
        registerTitle: select('[data-register-title]'),
        registerName: select('[data-register-name]'),
        registerQuantityPerScan: select('[data-register-quantity-per-scan]'),
        registerUnit: select('[data-register-unit]'),
        registerCountWrap: select('[data-register-count-wrap]'),
        registerCount: select('[data-register-count]'),
        registerCategory: select('[data-register-category]'),
        registerCode: select('[data-register-code]'),
        registerSubmit: select('[data-register-submit]'),
        registerSubmitLabel: select('[data-register-submit-label]'),
        registerOneDetail: select('[data-register-one-detail]'),
        editCatalog: select('[data-edit-catalog]'),
        registerCatalogNote: select('[data-register-catalog-note]'),
        registerPhotoWrap: select('[data-register-photo-wrap]'),
        photoCameraInput: select('[data-product-photo-camera]'),
        photoGalleryInput: select('[data-product-photo-gallery]'),
        productCameraView: select('[data-photo-camera-view]'),
        productCameraVideo: select('[data-photo-camera-video]'),
        productCameraPlaceholder: select('[data-photo-camera-placeholder]'),
        productCameraStatus: select('[data-photo-camera-status]'),
        productCameraError: select('[data-photo-camera-error]'),
        productCameraCapture: select('[data-photo-camera-capture]'),
        productCameraRetry: select('[data-photo-camera-retry]'),
        productCameraCancel: select('[data-photo-camera-cancel]'),
        productCameraGallery: select('[data-photo-camera-gallery]'),
        productCameraSystem: select('[data-photo-camera-system]'),
        registerPhotoPreview: select('[data-register-photo-preview]'),
        registerPhotoName: select('[data-register-photo-name]'),
        registerTotalPreview: select('[data-register-total-preview]'),
        cropViewport: select('[data-crop-viewport]'),
        cropImage: select('[data-crop-image]'),
        cropZoom: select('[data-crop-zoom]'),
        cropError: select('[data-crop-error]'),
        cropConfirm: select('[data-crop-confirm]'),
        cropConfirmIcon: select('[data-crop-confirm-icon]'),
        cropSpinner: select('[data-crop-spinner]'),
        cropReset: select('[data-crop-reset]'),
        cropCancel: select('[data-crop-cancel]'),
        successEyebrow: select('[data-success-eyebrow]'),
        successTitle: select('[data-success-title]'),
        successMessage: select('[data-success-message]'),
        errorMessage: select('[data-error-message]'),
        retryButton: select('[data-retry-action]'),
    };

    const endpoints = {
        lookup: root.dataset.lookupUrl,
        action: root.dataset.actionUrl,
        register: root.dataset.registerUrl,
    };
    const csrfToken = select('[data-scanner-csrf] input[name="csrfmiddlewaretoken"]')?.value || '';
    const barcodeFormats = ['ean_13', 'ean_8', 'upc_a', 'upc_e', 'code_128'];

    const state = {
        panel: 'scanning',
        scanning: false,
        stream: null,
        detector: null,
        detectionTimer: null,
        cameraWaitTimer: null,
        zxingReader: null,
        zxingControls: null,
        zxingPromise: null,
        cameraGeneration: 0,
        activeBarcode: null,
        scanId: null,
        product: null,
        catalog: null,
        registerMode: 'one',
        actionLocked: false,
        countdown: null,
        retryCallback: null,
        changed: false,
        changedProducts: new Set(),
        needsReload: false,
        photoFile: null,
        photoPreviewUrl: null,
        photoPickerPending: false,
        photoPickerInput: null,
        pendingPhotoRequest: null,
        crop: null,
        flowGeneration: 0,
        modalActive: false,
        photoUploadController: null,
        productCameraStream: null,
        productCameraRequest: null,
        productCameraGeneration: 0,
        lastActionCount: 1,
        autoActionEnabled: false,
        autoActionTimeoutMs: 5000,
    };

    function announce(message) {
        if (!elements.live) {
            return;
        }
        elements.live.textContent = '';
        window.setTimeout(() => {
            elements.live.textContent = message;
        }, 20);
    }

    function showPanel(name) {
        selectAll('[data-scanner-panel]').forEach((panel) => {
            panel.hidden = panel.dataset.scannerPanel !== name;
        });
        elements.cameraStage.classList.toggle('d-none', ['crop', 'photo-camera'].includes(name));
        state.panel = name;
    }

    function setCameraStatus(message, busy = false) {
        elements.cameraStatusText.textContent = message;
        const spinner = select('.spinner-border', elements.cameraStatus);
        spinner?.classList.toggle('d-none', !busy);
    }

    function showCameraAlert(message = '') {
        elements.cameraAlert.textContent = message;
        elements.cameraAlert.classList.toggle('d-none', !message);
    }

    // Adres tej samej strony przez HTTPS (Caddy na porcie 443), albo pusty,
    // gdy przejście na HTTPS nic nie da (już jest HTTPS albo to localhost,
    // który przeglądarki i tak traktują jako bezpieczny).
    function secureVersionUrl() {
        if (window.location.protocol !== 'http:' || ['localhost', '127.0.0.1', '[::1]'].includes(window.location.hostname)) {
            return '';
        }
        return `https://${window.location.hostname}${window.location.pathname}${window.location.search}`;
    }

    // Na http://192.168.x.x przeglądarka w ogóle nie udostępnia API aparatu.
    // Zamiast samego komunikatu dajemy link do tej samej strony przez HTTPS
    // i do instrukcji zaufania certyfikatowi.
    function showInsecureContextAlert() {
        const secureUrl = secureVersionUrl();
        showCameraAlert(cameraErrorMessage());
        if (!secureUrl) {
            return;
        }
        const links = document.createElement('div');
        links.className = 'd-flex flex-wrap gap-2 mt-2';
        const open = document.createElement('a');
        open.className = 'btn btn-sm btn-success';
        open.href = secureUrl;
        open.textContent = 'Otwórz przez HTTPS';
        const setup = document.createElement('a');
        setup.className = 'btn btn-sm btn-outline-secondary';
        setup.href = `http://${window.location.hostname}/certyfikat`;
        setup.textContent = 'Pierwszy raz? Zainstaluj certyfikat';
        links.append(open, setup);
        elements.cameraAlert.append(links);
    }

    function setCameraActive(active) {
        elements.cameraStage.classList.toggle('is-active', active);
        elements.cameraPlaceholder.classList.toggle('d-none', active);
    }

    function cleanBarcode(rawValue) {
        const barcode = String(rawValue || '').replace(/\s/g, '');
        if (!barcode) {
            throw new Error('Wpisz kod kreskowy.');
        }
        if (barcode.length > 64 || !/^[0-9A-Za-z._-]+$/.test(barcode)) {
            throw new Error('Kod kreskowy ma nieprawidłowy format.');
        }
        return barcode;
    }

    function makeScanId() {
        if (window.crypto?.randomUUID) {
            return window.crypto.randomUUID();
        }
        const bytes = new Uint8Array(16);
        if (window.crypto?.getRandomValues) {
            window.crypto.getRandomValues(bytes);
        } else {
            for (let index = 0; index < bytes.length; index += 1) {
                bytes[index] = Math.floor(Math.random() * 256);
            }
        }
        bytes[6] = (bytes[6] & 0x0f) | 0x40;
        bytes[8] = (bytes[8] & 0x3f) | 0x80;
        const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
        return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    }

    function formatNumber(value) {
        const number = Number(value);
        if (!Number.isFinite(number)) {
            return value;
        }
        return new Intl.NumberFormat('pl-PL', {
            minimumFractionDigits: Number.isInteger(number) ? 0 : 0,
            maximumFractionDigits: 2,
        }).format(number);
    }

    function formatQuantity(value, unitLabel) {
        return `${formatNumber(value)} ${unitLabel}`;
    }

    function formatProductStock(product) {
        const packages = Number(product.current_package_count || 0);
        const packageLabel = `${formatNumber(packages)} szt.`;
        const quantityPerScan = Number(product.quantity_per_scan || 1);
        if ((product.unit === 'szt' || product.unit === 'opak') && quantityPerScan === 1) {
            return packageLabel;
        }
        return `${packageLabel} · ${formatQuantity(product.current_quantity, product.unit_label)}`;
    }

    function catalogUnitLabel(unit) {
        const labels = {
            szt: 'szt.',
            g: 'g',
            kg: 'kg',
            ml: 'ml',
            l: 'l',
            opak: 'opak.',
        };
        return labels[unit] || unit || 'opak.';
    }

    function updateRegisterTotalPreview() {
        if (!elements.registerTotalPreview) {
            return;
        }
        const count = Number(elements.registerCount.value || 1);
        const quantityPerScan = Number(elements.registerQuantityPerScan.value || 0);
        if (!Number.isFinite(count) || !Number.isFinite(quantityPerScan)) {
            elements.registerTotalPreview.textContent = '—';
            return;
        }
        elements.registerTotalPreview.textContent = formatQuantity(
            count * quantityPerScan,
            catalogUnitLabel(elements.registerUnit.value),
        );
    }

    function updateKnownActionLabels() {
        if (!state.product) {
            return;
        }
        const count = Number(elements.knownCount?.value || 1);
        const quantityPerScan = Number(state.product.quantity_per_scan || 0);
        if (!Number.isInteger(count) || count < 1 || !Number.isFinite(quantityPerScan)) {
            elements.knownConsumeLabel.textContent = '—';
            elements.knownPurchaseLabel.textContent = '—';
            return;
        }
        const total = formatQuantity(
            quantityPerScan * count,
            state.product.unit_label,
        );
        elements.knownConsumeLabel.textContent = `−${total}`;
        elements.knownPurchaseLabel.textContent = `+${total}`;
    }

    function setResultImage(wrap, image, fallbackIcon, url, alt) {
        image.onload = null;
        image.onerror = null;
        image.removeAttribute('src');
        image.alt = '';
        wrap.hidden = true;
        fallbackIcon.hidden = false;
        if (!url) {
            return;
        }
        image.alt = alt;
        image.onload = () => {
            wrap.hidden = false;
            fallbackIcon.hidden = true;
        };
        image.onerror = () => {
            image.removeAttribute('src');
            image.alt = '';
            wrap.hidden = true;
            fallbackIcon.hidden = false;
        };
        image.src = url;
    }

    function setSourceLink(link, catalog) {
        const sourceUrl = String(catalog?.attribution_url || '');
        const isSafe = sourceUrl.startsWith('https://');
        link.hidden = !isSafe;
        if (isSafe) {
            link.href = sourceUrl;
        } else {
            link.removeAttribute('href');
        }
    }

    // Nazwa języka w narzędniku, do zdania "tylko pod nazwą w języku ...".
    const CATALOG_LANGUAGE_NAMES = {
        en: 'angielskim', de: 'niemieckim', cs: 'czeskim', sk: 'słowackim',
        fr: 'francuskim', it: 'włoskim', es: 'hiszpańskim', uk: 'ukraińskim',
        hu: 'węgierskim', nl: 'niderlandzkim', lt: 'litewskim', ro: 'rumuńskim',
        pt: 'portugalskim',
    };

    function catalogIsFound(catalog) {
        return ['found', 'found_incomplete'].includes(catalog?.status);
    }

    // Katalog zna produkt, ale tylko pod nazwą, która nie jest po polsku.
    // Taki produkt nie może trafić do spiżarni jednym dotknięciem - formularz
    // prosi o polską nazwę, a serwer zapamiętuje ją dla całego domu.
    function catalogNeedsPolishName(catalog) {
        return catalogIsFound(catalog) && Boolean(catalog.name) && !catalog.name_is_polish;
    }

    function catalogLanguageLabel(catalog) {
        return CATALOG_LANGUAGE_NAMES[catalog?.name_language] || 'obcym';
    }

    function catalogWhereFound(catalog) {
        return catalog?.remembered ? 'wśród produktów zapamiętanych w domu' : 'w Open Food Facts';
    }

    function updateRegisterCatalogNote(catalog) {
        const note = elements.registerCatalogNote;
        const found = catalogIsFound(catalog);
        note.hidden = !found;
        if (!found) {
            return;
        }
        const needsPolish = catalogNeedsPolishName(catalog);
        note.classList.toggle('alert-info', !needsPolish);
        note.classList.toggle('alert-warning', needsPolish);
        if (catalog.remembered) {
            note.textContent = 'Te dane zapisał wcześniej ktoś z domowników. Poprawki zapamiętam dla całego domu.';
        } else if (needsPolish) {
            note.textContent = `Baza zna ten produkt tylko pod nazwą w języku ${catalogLanguageLabel(catalog)}. `
                + 'Wpisz polską nazwę - zapamiętam ją dla całego domu.';
        } else {
            note.textContent = 'Formularz uzupełniono danymi z Open Food Facts. Poprawki zapamiętam dla całego domu.';
        }
    }

    function catalogDescription(catalog) {
        if (catalog?.description) {
            return catalog.description;
        }
        if (catalog?.ingredients) {
            const ingredients = String(catalog.ingredients);
            return `Skład: ${ingredients.length > 240 ? `${ingredients.slice(0, 237)}…` : ingredients}`;
        }
        return '';
    }

    function resetCatalogPresentation() {
        setResultImage(elements.knownImageWrap, elements.knownImage, elements.knownIcon, '', '');
        setResultImage(elements.unknownImageWrap, elements.unknownImage, elements.unknownIcon, '', '');
        [elements.knownCatalogMeta, elements.knownDescription, elements.knownSource,
            elements.unknownMeta, elements.unknownDescription, elements.unknownSource,
            elements.editCatalog, elements.registerCatalogNote].forEach((element) => {
            element.hidden = true;
        });
        elements.knownCatalogMeta.textContent = '';
        elements.knownDescription.textContent = '';
        elements.unknownMeta.textContent = '';
        elements.unknownDescription.textContent = '';
        elements.registerOneDetail.textContent = 'jedno opakowanie';
        if (elements.knownPhotoWrap) {
            elements.knownPhotoWrap.hidden = true;
        }
        if (elements.unknownPhotoWrap) {
            elements.unknownPhotoWrap.hidden = true;
        }
    }

    function resetProductPhoto() {
        if (state.photoPreviewUrl) {
            URL.revokeObjectURL(state.photoPreviewUrl);
        }
        state.photoPreviewUrl = null;
        state.photoFile = null;
        [elements.photoCameraInput, elements.photoGalleryInput].forEach((input) => {
            if (input) {
                input.value = '';
            }
        });
        if (elements.registerPhotoPreview) {
            elements.registerPhotoPreview.removeAttribute('src');
            elements.registerPhotoPreview.alt = '';
        }
        if (elements.registerPhotoName) {
            elements.registerPhotoName.textContent = 'Dodaj zdjęcie produktu';
        }
        if (elements.registerPhotoWrap) {
            elements.registerPhotoWrap.classList.remove('has-photo');
        }
    }

    function rememberProductPhoto(file) {
        if (state.photoPreviewUrl) {
            URL.revokeObjectURL(state.photoPreviewUrl);
        }
        state.photoFile = file;
        state.photoPreviewUrl = URL.createObjectURL(file);
        if (elements.registerPhotoPreview) {
            elements.registerPhotoPreview.src = state.photoPreviewUrl;
            elements.registerPhotoPreview.alt = 'Podgląd zdjęcia produktu';
        }
        if (elements.registerPhotoName) {
            elements.registerPhotoName.textContent = 'Zdjęcie gotowe';
        }
        if (elements.registerPhotoWrap) {
            elements.registerPhotoWrap.classList.add('has-photo');
        }
    }

    function showCropError(message = '') {
        if (!elements.cropError) {
            return;
        }
        elements.cropError.textContent = message;
        elements.cropError.hidden = !message;
    }

    function setCropBusy(busy) {
        elements.cropConfirm.disabled = busy || !state.crop?.ready;
        elements.cropSpinner?.classList.toggle('d-none', !busy);
        elements.cropConfirmIcon?.classList.toggle('d-none', busy);
    }

    function cleanupCrop() {
        const crop = state.crop;
        if (crop?.drag?.pointerId != null && elements.cropViewport?.hasPointerCapture?.(crop.drag.pointerId)) {
            elements.cropViewport.releasePointerCapture(crop.drag.pointerId);
        }
        if (crop?.sourceUrl) {
            URL.revokeObjectURL(crop.sourceUrl);
        }
        if (elements.cropImage) {
            elements.cropImage.onload = null;
            elements.cropImage.onerror = null;
            elements.cropImage.removeAttribute('src');
            elements.cropImage.removeAttribute('style');
        }
        if (elements.cropViewport) {
            elements.cropViewport.classList.add('is-loading');
            elements.cropViewport.classList.remove('is-dragging');
        }
        if (elements.cropZoom) {
            elements.cropZoom.value = '1';
            elements.cropZoom.disabled = true;
        }
        showCropError();
        state.crop = null;
        if (elements.cropConfirm) {
            elements.cropConfirm.disabled = true;
        }
        elements.cropSpinner?.classList.add('d-none');
        elements.cropConfirmIcon?.classList.remove('d-none');
    }

    function cropGeometry() {
        const crop = state.crop;
        if (!crop?.ready) {
            return null;
        }
        const viewportWidth = elements.cropViewport.clientWidth;
        const viewportHeight = elements.cropViewport.clientHeight;
        if (!viewportWidth || !viewportHeight || !crop.naturalWidth || !crop.naturalHeight) {
            return null;
        }
        const frameSize = Math.min(viewportWidth, viewportHeight) * 0.84;
        const frameLeft = (viewportWidth - frameSize) / 2;
        const frameTop = (viewportHeight - frameSize) / 2;
        const baseScale = Math.max(
            frameSize / crop.naturalWidth,
            frameSize / crop.naturalHeight,
        );
        const scale = baseScale * crop.zoom;
        const scaledWidth = crop.naturalWidth * scale;
        const scaledHeight = crop.naturalHeight * scale;
        return {
            viewportWidth,
            viewportHeight,
            frameSize,
            frameLeft,
            frameTop,
            baseScale,
            scale,
            scaledWidth,
            scaledHeight,
            maxOffsetX: Math.max(0, (scaledWidth - frameSize) / 2),
            maxOffsetY: Math.max(0, (scaledHeight - frameSize) / 2),
        };
    }

    function clamp(value, minimum, maximum) {
        return Math.min(maximum, Math.max(minimum, value));
    }

    function renderCrop() {
        const crop = state.crop;
        const geometry = cropGeometry();
        if (!crop || !geometry) {
            return;
        }
        crop.offsetX = clamp(crop.offsetX, -geometry.maxOffsetX, geometry.maxOffsetX);
        crop.offsetY = clamp(crop.offsetY, -geometry.maxOffsetY, geometry.maxOffsetY);
        crop.geometry = geometry;
        elements.cropImage.style.width = `${geometry.scaledWidth}px`;
        elements.cropImage.style.height = `${geometry.scaledHeight}px`;
        elements.cropImage.style.left = `${(geometry.viewportWidth - geometry.scaledWidth) / 2 + crop.offsetX}px`;
        elements.cropImage.style.top = `${(geometry.viewportHeight - geometry.scaledHeight) / 2 + crop.offsetY}px`;
    }

    function resetCropPosition() {
        if (!state.crop?.ready) {
            return;
        }
        state.crop.zoom = 1;
        state.crop.offsetX = 0;
        state.crop.offsetY = 0;
        elements.cropZoom.value = '1';
        renderCrop();
    }

    function initializeCrop() {
        const crop = state.crop;
        if (!crop || !elements.cropImage.naturalWidth || !elements.cropImage.naturalHeight) {
            return;
        }
        crop.naturalWidth = elements.cropImage.naturalWidth;
        crop.naturalHeight = elements.cropImage.naturalHeight;
        crop.zoom = 1;
        crop.offsetX = 0;
        crop.offsetY = 0;
        crop.ready = true;
        elements.cropViewport.classList.remove('is-loading');
        elements.cropZoom.disabled = false;
        elements.cropZoom.value = '1';
        renderCrop();
        setCropBusy(false);
        announce('Zdjęcie jest gotowe do kadrowania. Przesuń je palcem lub zmień powiększenie.');
    }

    async function readRasterDimensions(file) {
        const buffer = await file.slice(0, 512 * 1024).arrayBuffer();
        const bytes = new Uint8Array(buffer);
        const view = new DataView(buffer);

        const isPng = bytes.length >= 24
            && bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47;
        if (isPng) {
            return { width: view.getUint32(16), height: view.getUint32(20) };
        }

        const isJpeg = bytes.length >= 4 && bytes[0] === 0xff && bytes[1] === 0xd8;
        if (isJpeg) {
            const startOfFrameMarkers = new Set([
                0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf,
            ]);
            let offset = 2;
            while (offset + 9 < bytes.length) {
                while (offset < bytes.length && bytes[offset] !== 0xff) {
                    offset += 1;
                }
                while (offset < bytes.length && bytes[offset] === 0xff) {
                    offset += 1;
                }
                if (offset >= bytes.length) {
                    break;
                }
                const marker = bytes[offset];
                offset += 1;
                if (marker === 0xd8 || marker === 0xd9 || (marker >= 0xd0 && marker <= 0xd7)) {
                    continue;
                }
                if (offset + 1 >= bytes.length) {
                    break;
                }
                const segmentLength = view.getUint16(offset);
                if (segmentLength < 2 || offset + segmentLength > bytes.length) {
                    break;
                }
                if (startOfFrameMarkers.has(marker) && segmentLength >= 7) {
                    return {
                        width: view.getUint16(offset + 5),
                        height: view.getUint16(offset + 3),
                    };
                }
                offset += segmentLength;
            }
        }

        const isWebp = bytes.length >= 30
            && String.fromCharCode(...bytes.slice(0, 4)) === 'RIFF'
            && String.fromCharCode(...bytes.slice(8, 12)) === 'WEBP';
        if (isWebp) {
            const chunk = String.fromCharCode(...bytes.slice(12, 16));
            if (chunk === 'VP8X') {
                const width = 1 + bytes[24] + (bytes[25] << 8) + (bytes[26] << 16);
                const height = 1 + bytes[27] + (bytes[28] << 8) + (bytes[29] << 16);
                return { width, height };
            }
            if (chunk === 'VP8L' && bytes[20] === 0x2f) {
                const width = 1 + bytes[21] + ((bytes[22] & 0x3f) << 8);
                const height = 1 + (bytes[22] >> 6) + (bytes[23] << 2) + ((bytes[24] & 0x0f) << 10);
                return { width, height };
            }
            if (chunk === 'VP8 ' && bytes.length >= 30) {
                return {
                    width: view.getUint16(26, true) & 0x3fff,
                    height: view.getUint16(28, true) & 0x3fff,
                };
            }
        }
        return null;
    }

    async function prepareCropSource(file) {
        if (!window.createImageBitmap || file.size <= 3 * 1024 * 1024) {
            return file;
        }
        let bitmap = null;
        try {
            const dimensions = await readRasterDimensions(file);
            if (!dimensions?.width || !dimensions?.height) {
                return file;
            }
            const maximumDimension = 2400;
            const scale = Math.min(1, maximumDimension / Math.max(dimensions.width, dimensions.height));
            if (scale >= 1) {
                return file;
            }
            bitmap = await window.createImageBitmap(file, {
                imageOrientation: 'from-image',
                resizeWidth: Math.max(1, Math.round(dimensions.width * scale)),
                resizeHeight: Math.max(1, Math.round(dimensions.height * scale)),
                resizeQuality: 'high',
            });
            const canvas = document.createElement('canvas');
            canvas.width = bitmap.width;
            canvas.height = bitmap.height;
            const context = canvas.getContext('2d');
            if (!context) {
                return file;
            }
            context.drawImage(bitmap, 0, 0);
            const blob = await canvasToBlob(canvas, 'image/jpeg', 0.9);
            const originalName = String(file.name || 'produkt').replace(/\.[^.]+$/, '') || 'produkt';
            return new File([blob], `${originalName}-podglad.jpg`, {
                type: 'image/jpeg',
                lastModified: file.lastModified || Date.now(),
            });
        } catch (error) {
            return file;
        } finally {
            bitmap?.close?.();
        }
    }

    async function openCropEditor(file, request) {
        if (!file || !request) {
            resumeCountdown();
            return false;
        }
        if (file.type === 'image/svg+xml' || (file.type && !file.type.startsWith('image/'))) {
            announce('Wybrany plik nie jest obsługiwanym zdjęciem.');
            resumeCountdown();
            return false;
        }
        cleanupCrop();
        const crop = {
            file,
            sourceUrl: null,
            target: request.target,
            returnPanel: request.returnPanel,
            returnMode: request.registerMode,
            flowGeneration: state.flowGeneration,
            ready: false,
            zoom: 1,
            offsetX: 0,
            offsetY: 0,
            drag: null,
        };
        state.crop = crop;
        showPanel('crop');
        showCropError();
        setCropBusy(false);
        elements.cropZoom.disabled = true;
        elements.cropViewport.classList.add('is-loading');
        try {
            const preparedFile = await prepareCropSource(file);
            if (state.crop !== crop || crop.flowGeneration !== state.flowGeneration) {
                return;
            }
            crop.file = preparedFile;
            crop.sourceUrl = URL.createObjectURL(preparedFile);
            elements.cropImage.onload = initializeCrop;
            elements.cropImage.onerror = () => {
                if (state.crop !== crop) {
                    return;
                }
                showCropError('Nie udało się otworzyć tego zdjęcia. Wybierz plik JPG, PNG, WEBP lub zrób nowe zdjęcie.');
                elements.cropViewport.classList.remove('is-loading');
                setCropBusy(false);
                announce('Nie udało się otworzyć zdjęcia.');
            };
            elements.cropImage.src = crop.sourceUrl;
        } catch (error) {
            if (state.crop === crop) {
                showCropError('Nie udało się przygotować zdjęcia. Wybierz inne zdjęcie lub zrób je ponownie.');
                elements.cropViewport.classList.remove('is-loading');
                announce('Nie udało się przygotować zdjęcia.');
            }
        }
        return true;
    }

    function cancelCrop() {
        const returnPanel = state.crop?.returnPanel || 'unknown';
        cleanupCrop();
        showPanel(returnPanel);
        resumeCountdown();
        announce('Kadrowanie anulowane.');
    }

    function canvasToBlob(canvas, type, quality) {
        return new Promise((resolve, reject) => {
            canvas.toBlob((blob) => {
                if (blob) {
                    resolve(blob);
                } else {
                    reject(new Error('Nie udało się przygotować zdjęcia do zapisu.'));
                }
            }, type, quality);
        });
    }

    async function createCroppedPhoto() {
        const crop = state.crop;
        const geometry = cropGeometry();
        if (!crop?.ready || !geometry) {
            throw new Error('Zdjęcie nie jest jeszcze gotowe do kadrowania.');
        }
        crop.geometry = geometry;
        const imageLeft = (geometry.viewportWidth - geometry.scaledWidth) / 2 + crop.offsetX;
        const imageTop = (geometry.viewportHeight - geometry.scaledHeight) / 2 + crop.offsetY;
        const sourceX = clamp((geometry.frameLeft - imageLeft) / geometry.scale, 0, crop.naturalWidth);
        const sourceY = clamp((geometry.frameTop - imageTop) / geometry.scale, 0, crop.naturalHeight);
        const sourceWidth = Math.min(geometry.frameSize / geometry.scale, crop.naturalWidth - sourceX);
        const sourceHeight = Math.min(geometry.frameSize / geometry.scale, crop.naturalHeight - sourceY);
        const outputSize = Math.max(1, Math.min(1200, Math.round(Math.min(sourceWidth, sourceHeight))));
        const canvas = document.createElement('canvas');
        canvas.width = outputSize;
        canvas.height = outputSize;
        const context = canvas.getContext('2d');
        if (!context) {
            throw new Error('Ta przeglądarka nie potrafi przygotować kadru.');
        }
        context.fillStyle = '#ffffff';
        context.fillRect(0, 0, outputSize, outputSize);
        context.drawImage(
            elements.cropImage,
            sourceX,
            sourceY,
            sourceWidth,
            sourceHeight,
            0,
            0,
            outputSize,
            outputSize,
        );
        const blob = await canvasToBlob(canvas, 'image/jpeg', 0.9);
        const originalName = String(crop.file.name || 'produkt').replace(/\.[^.]+$/, '') || 'produkt';
        return new File([blob], `${originalName}-kadr.jpg`, {
            type: 'image/jpeg',
            lastModified: Date.now(),
        });
    }

    async function confirmCrop() {
        const crop = state.crop;
        if (!crop?.ready) {
            return;
        }
        setCropBusy(true);
        showCropError();
        try {
            const photo = await createCroppedPhoto();
            if (state.crop !== crop || crop.flowGeneration !== state.flowGeneration) {
                return;
            }
            const target = crop.target;
            const returnPanel = crop.returnPanel;
            const returnMode = crop.returnMode;
            cancelCountdown();
            cleanupCrop();
            if (target === 'known') {
                showPanel('known');
                await uploadKnownProductPhoto(photo);
                return;
            }
            rememberProductPhoto(photo);
            if (returnPanel === 'register') {
                showPanel('register');
                announce('Wykadrowane zdjęcie jest gotowe do zapisania.');
                return;
            }
            openRegistration(returnMode || state.registerMode || 'one');
            announce('Wykadrowane zdjęcie jest gotowe do zapisania.');
        } catch (error) {
            if (state.crop !== crop) {
                return;
            }
            showCropError(error.message || 'Nie udało się wykadrować zdjęcia.');
            setCropBusy(false);
            announce(`Nie udało się przygotować zdjęcia. ${error.message || ''}`.trim());
        }
    }

    async function fetchJson(url, options = {}) {
        const isFormData = options.body instanceof FormData;
        const requestHeaders = options.headers || {};
        const fetchOptions = { ...options };
        delete fetchOptions.headers;
        const response = await fetch(url, {
            credentials: 'same-origin',
            headers: {
                Accept: 'application/json',
                ...(options.body ? {
                    'X-CSRFToken': csrfToken,
                } : {}),
                ...(options.body && !isFormData ? { 'Content-Type': 'application/json' } : {}),
                ...requestHeaders,
            },
            ...fetchOptions,
        });

        let payload = {};
        try {
            payload = await response.json();
        } catch (error) {
            throw new Error('Serwer zwrócił nieprawidłową odpowiedź.');
        }
        if (!response.ok || payload.ok === false) {
            const requestError = new Error(payload.error || 'Nie udało się wykonać operacji.');
            requestError.status = response.status;
            throw requestError;
        }
        return payload;
    }

    function cancelCountdown() {
        if (!state.countdown) {
            return;
        }
        window.cancelAnimationFrame(state.countdown.frame);
        state.countdown = null;
    }

    function setCountdownAvailability(panel, available) {
        const countdown = select(`[data-countdown="${panel}"]`);
        const pausedMessage = select(`[data-countdown-paused="${panel}"]`);
        const manualMessage = select(`[data-countdown-manual="${panel}"]`);
        const automaticActionEnabled = state.autoActionEnabled;
        if (countdown) {
            countdown.hidden = !automaticActionEnabled || !available;
        }
        if (pausedMessage) {
            pausedMessage.hidden = !automaticActionEnabled || available;
        }
        if (manualMessage) {
            manualMessage.hidden = automaticActionEnabled;
        }
    }

    function renderCountdown(timer) {
        const container = select(`[data-countdown="${timer.panel}"]`);
        if (!container) {
            return;
        }
        const seconds = select('[data-countdown-seconds]', container);
        const bar = select('[data-countdown-bar]', container);
        const progress = select('[role="progressbar"]', container);
        const remainingSeconds = Math.max(0, Math.ceil(timer.remaining / 1000));
        seconds.textContent = String(remainingSeconds);
        bar.style.width = `${Math.max(0, Math.min(100, (timer.remaining / timer.duration) * 100))}%`;
        progress?.setAttribute('aria-valuenow', String(Math.max(0, timer.remaining / 1000)));
        progress?.setAttribute('aria-valuemax', String(timer.duration / 1000));
    }

    function countdownTick(now, timer) {
        if (state.countdown !== timer || !timer.running) {
            return;
        }
        timer.remaining -= now - timer.lastTick;
        timer.lastTick = now;
        renderCountdown(timer);
        if (timer.remaining <= 0) {
            state.countdown = null;
            timer.onComplete();
            return;
        }
        timer.frame = window.requestAnimationFrame((nextNow) => countdownTick(nextNow, timer));
    }

    function startCountdown(panel, duration, onComplete) {
        cancelCountdown();
        if (!state.autoActionEnabled) {
            return;
        }
        const timer = {
            panel,
            duration,
            remaining: duration,
            onComplete,
            lastTick: performance.now(),
            running: !document.hidden,
            frame: null,
        };
        state.countdown = timer;
        renderCountdown(timer);
        if (timer.running) {
            timer.frame = window.requestAnimationFrame((now) => countdownTick(now, timer));
        }
    }

    function pauseCountdown() {
        const timer = state.countdown;
        if (!timer?.running) {
            return;
        }
        const now = performance.now();
        timer.remaining = Math.max(0, timer.remaining - (now - timer.lastTick));
        timer.running = false;
        window.cancelAnimationFrame(timer.frame);
        renderCountdown(timer);
    }

    function resumeCountdown() {
        const timer = state.countdown;
        if (!timer || timer.running) {
            return;
        }
        timer.running = true;
        timer.lastTick = performance.now();
        timer.frame = window.requestAnimationFrame((now) => countdownTick(now, timer));
    }

    function stopCamera() {
        state.cameraGeneration += 1;
        state.scanning = false;
        window.clearTimeout(state.detectionTimer);
        window.clearTimeout(state.cameraWaitTimer);
        state.detectionTimer = null;
        state.cameraWaitTimer = null;

        if (state.zxingControls) {
            try {
                state.zxingControls.stop();
            } catch (error) {
                // The scanner may already have released the camera.
            }
        }
        state.zxingControls = null;
        state.zxingReader = null;

        const tracks = new Set([
            ...(state.stream?.getTracks?.() || []),
            ...(video.srcObject?.getTracks?.() || []),
        ]);
        tracks.forEach((track) => track.stop());
        state.stream = null;
        video.srcObject = null;
        setCameraActive(false);
    }

    function productCameraErrorMessage(error) {
        if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
            return 'Aparat w przeglądarce wymaga połączenia HTTPS albo adresu localhost. Możesz nadal wybrać zdjęcie z galerii.';
        }
        if (error?.name === 'NotAllowedError' || error?.name === 'SecurityError') {
            return 'Dostęp do kamery jest zablokowany. Zezwól tej stronie na użycie aparatu w ustawieniach przeglądarki i spróbuj ponownie.';
        }
        if (error?.name === 'NotFoundError' || error?.name === 'OverconstrainedError') {
            return 'Nie znaleziono dostępnej kamery. Podłącz aparat albo wybierz zdjęcie z galerii.';
        }
        if (error?.name === 'NotReadableError' || error?.name === 'AbortError') {
            return 'Kamera jest zajęta przez inną aplikację. Zamknij np. Teams lub Zoom i spróbuj ponownie.';
        }
        return error?.message || 'Nie udało się uruchomić aparatu. Spróbuj ponownie albo wybierz zdjęcie z galerii.';
    }

    function setProductCameraError(message = '') {
        elements.productCameraError.textContent = message;
        elements.productCameraError.hidden = !message;
    }

    function setProductCameraLoading(message = 'Uruchamiam aparat…') {
        elements.productCameraStatus.textContent = message;
        elements.productCameraView.classList.add('is-loading');
        elements.productCameraPlaceholder.hidden = false;
        elements.productCameraCapture.disabled = true;
    }

    function releaseProductCameraStream() {
        const tracks = new Set([
            ...(state.productCameraStream?.getTracks?.() || []),
            ...(elements.productCameraVideo?.srcObject?.getTracks?.() || []),
        ]);
        tracks.forEach((track) => track.stop());
        state.productCameraStream = null;
        if (elements.productCameraVideo) {
            elements.productCameraVideo.srcObject = null;
        }
        elements.productCameraView?.classList.remove('is-ready');
        elements.productCameraView?.classList.add('is-loading');
        if (elements.productCameraCapture) {
            elements.productCameraCapture.disabled = true;
        }
    }

    function stopProductCamera({ keepRequest = false } = {}) {
        state.productCameraGeneration += 1;
        releaseProductCameraStream();
        if (!keepRequest) {
            state.productCameraRequest = null;
        }
    }

    function openPhotoInput(input, request) {
        if (!input || !request) {
            return;
        }
        state.pendingPhotoRequest = request;
        state.photoPickerPending = true;
        state.photoPickerInput = input;
        input.value = '';
        input.click();
    }

    async function startProductCamera(request = state.productCameraRequest) {
        if (!request) {
            return;
        }
        stopCamera();
        stopProductCamera({ keepRequest: true });
        state.productCameraRequest = request;
        const generation = state.productCameraGeneration;
        const flowGeneration = state.flowGeneration;
        showPanel('photo-camera');
        setProductCameraError();
        setProductCameraLoading();
        elements.productCameraRetry.hidden = true;
        elements.productCameraSystem.hidden = true;
        announce('Uruchamiam aparat do zdjęcia produktu.');

        if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
            const message = productCameraErrorMessage();
            setProductCameraError(message);
            setProductCameraLoading('Aparat niedostępny');
            elements.productCameraRetry.hidden = false;
            elements.productCameraSystem.hidden = false;
            announce(message);
            return;
        }

        const waitTimer = window.setTimeout(() => {
            if (generation === state.productCameraGeneration && state.panel === 'photo-camera') {
                setProductCameraLoading('Czekam na zgodę…');
            }
        }, 8000);
        let stream = null;
        try {
            try {
                stream = await navigator.mediaDevices.getUserMedia({
                    audio: false,
                    video: {
                        facingMode: { ideal: 'environment' },
                        width: { ideal: 1920 },
                        height: { ideal: 1080 },
                    },
                });
            } catch (error) {
                if (error?.name !== 'OverconstrainedError') {
                    throw error;
                }
                stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: true });
            }
            if (
                generation !== state.productCameraGeneration
                || flowGeneration !== state.flowGeneration
                || !state.modalActive
                || state.panel !== 'photo-camera'
            ) {
                stream.getTracks().forEach((track) => track.stop());
                return;
            }
            state.productCameraStream = stream;
            elements.productCameraVideo.srcObject = stream;
            if (elements.productCameraVideo.readyState < HTMLMediaElement.HAVE_METADATA) {
                await new Promise((resolve) => {
                    const finish = () => resolve();
                    elements.productCameraVideo.addEventListener('loadedmetadata', finish, { once: true });
                    window.setTimeout(finish, 3000);
                });
            }
            await elements.productCameraVideo.play();
            if (
                generation !== state.productCameraGeneration
                || flowGeneration !== state.flowGeneration
                || state.panel !== 'photo-camera'
            ) {
                stream.getTracks().forEach((track) => track.stop());
                return;
            }
            const videoTrack = stream.getVideoTracks()[0];
            if (videoTrack) {
                videoTrack.addEventListener('ended', () => {
                    if (
                        generation !== state.productCameraGeneration
                        || state.panel !== 'photo-camera'
                        || !state.productCameraStream
                    ) {
                        return;
                    }
                    const message = 'Kamera przestała być dostępna. Spróbuj uruchomić ją ponownie.';
                    stopProductCamera({ keepRequest: true });
                    setProductCameraError(message);
                    setProductCameraLoading('Podgląd zatrzymany');
                    elements.productCameraRetry.hidden = false;
                    announce(message);
                }, { once: true });
            }
            elements.productCameraView.classList.remove('is-loading');
            elements.productCameraView.classList.add('is-ready');
            elements.productCameraPlaceholder.hidden = true;
            if (!elements.productCameraVideo.videoWidth || !elements.productCameraVideo.videoHeight) {
                throw new Error('Aparat nie przekazał obrazu. Spróbuj uruchomić go ponownie.');
            }
            elements.productCameraCapture.disabled = false;
            elements.productCameraRetry.hidden = true;
            announce('Aparat jest gotowy. Ustaw produkt w ramce i zrób zdjęcie.');
        } catch (error) {
            stream?.getTracks?.().forEach((track) => track.stop());
            if (generation !== state.productCameraGeneration || flowGeneration !== state.flowGeneration) {
                return;
            }
            const message = productCameraErrorMessage(error);
            stopProductCamera({ keepRequest: true });
            setProductCameraError(message);
            setProductCameraLoading('Nie udało się uruchomić aparatu');
            elements.productCameraRetry.hidden = false;
            elements.productCameraSystem.hidden = false;
            announce(message);
        } finally {
            window.clearTimeout(waitTimer);
        }
    }

    function cancelProductCamera() {
        const request = state.productCameraRequest;
        stopProductCamera();
        setProductCameraError();
        showPanel(request?.returnPanel || 'unknown');
        resumeCountdown();
        announce('Robienie zdjęcia anulowane.');
    }

    async function captureProductCameraPhoto() {
        const request = state.productCameraRequest;
        const stream = state.productCameraStream;
        const sourceWidth = elements.productCameraVideo.videoWidth;
        const sourceHeight = elements.productCameraVideo.videoHeight;
        if (!request || !stream || !sourceWidth || !sourceHeight || elements.productCameraCapture.disabled) {
            return;
        }
        const generation = state.productCameraGeneration;
        const flowGeneration = state.flowGeneration;
        elements.productCameraCapture.disabled = true;
        const maximumDimension = 2400;
        const scale = Math.min(1, maximumDimension / Math.max(sourceWidth, sourceHeight));
        const canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(sourceWidth * scale));
        canvas.height = Math.max(1, Math.round(sourceHeight * scale));
        const context = canvas.getContext('2d');
        if (!context) {
            setProductCameraError('Nie udało się przygotować zdjęcia w tej przeglądarce.');
            elements.productCameraCapture.disabled = false;
            return;
        }
        try {
            if (elements.productCameraVideo.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
                throw new Error('Aparat nie ma jeszcze gotowego obrazu. Spróbuj ponownie.');
            }
            context.drawImage(elements.productCameraVideo, 0, 0, canvas.width, canvas.height);
            releaseProductCameraStream();
            setProductCameraLoading('Przygotowuję zdjęcie…');
            const blob = await canvasToBlob(canvas, 'image/jpeg', 0.92);
            if (
                generation !== state.productCameraGeneration
                || flowGeneration !== state.flowGeneration
                || state.panel !== 'photo-camera'
            ) {
                return;
            }
            const photo = new File([blob], `produkt-${Date.now()}.jpg`, {
                type: 'image/jpeg',
                lastModified: Date.now(),
            });
            stopProductCamera();
            openCropEditor(photo, request);
        } catch (error) {
            if (generation !== state.productCameraGeneration) {
                return;
            }
            releaseProductCameraStream();
            setProductCameraError(error.message || 'Nie udało się zrobić zdjęcia.');
            setProductCameraLoading('Nie udało się przygotować zdjęcia');
            elements.productCameraRetry.hidden = false;
        }
    }

    async function loadZxing() {
        if (window.ZXingBrowser) {
            return window.ZXingBrowser;
        }
        if (state.zxingPromise) {
            return state.zxingPromise;
        }
        state.zxingPromise = new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = root.dataset.zxingUrl;
            script.async = true;
            script.onload = () => {
                if (window.ZXingBrowser) {
                    resolve(window.ZXingBrowser);
                } else {
                    reject(new Error('Nie udało się uruchomić czytnika kodów.'));
                }
            };
            script.onerror = () => reject(new Error('Nie udało się wczytać czytnika kodów.'));
            document.head.appendChild(script);
        });
        return state.zxingPromise;
    }

    async function supportedNativeDetector() {
        if (!('BarcodeDetector' in window)) {
            return null;
        }
        try {
            const supported = await window.BarcodeDetector.getSupportedFormats();
            const formats = barcodeFormats.filter((format) => supported.includes(format));
            return formats.length ? new window.BarcodeDetector({ formats }) : null;
        } catch (error) {
            return null;
        }
    }

    async function startNativeCamera(detector, generation) {
        const stream = await navigator.mediaDevices.getUserMedia({
            audio: false,
            video: {
                facingMode: { ideal: 'environment' },
                width: { ideal: 1280 },
                height: { ideal: 720 },
            },
        });
        if (generation !== state.cameraGeneration) {
            stream.getTracks().forEach((track) => track.stop());
            return;
        }
        state.stream = stream;
        state.detector = detector;
        video.srcObject = stream;
        await video.play();
        if (generation !== state.cameraGeneration) {
            stream.getTracks().forEach((track) => track.stop());
            return;
        }
        state.scanning = true;
        window.clearTimeout(state.cameraWaitTimer);
        state.cameraWaitTimer = null;
        showCameraAlert();
        setCameraActive(true);
        setCameraStatus('Skanowanie aktywne');

        const detectFrame = async () => {
            if (!state.scanning || generation !== state.cameraGeneration) {
                return;
            }
            try {
                if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
                    const results = await detector.detect(video);
                    const barcode = results.find((result) => result.rawValue)?.rawValue;
                    if (barcode) {
                        await handleBarcode(barcode);
                        return;
                    }
                }
            } catch (error) {
                // A single undecodable frame is expected while the product moves.
            }
            if (state.scanning && generation === state.cameraGeneration) {
                state.detectionTimer = window.setTimeout(detectFrame, 150);
            }
        };
        state.detectionTimer = window.setTimeout(detectFrame, 100);
    }

    async function startZxingCamera(generation) {
        const ZXingBrowser = await loadZxing();
        if (generation !== state.cameraGeneration) {
            return;
        }
        const reader = new ZXingBrowser.BrowserMultiFormatReader();
        state.zxingReader = reader;
        state.scanning = true;
        setCameraStatus('Skanowanie aktywne');

        const controls = await reader.decodeFromConstraints({
            audio: false,
            video: {
                facingMode: { ideal: 'environment' },
                width: { ideal: 1280 },
                height: { ideal: 720 },
            },
        }, video, (result, error, callbackControls) => {
            if (!result || !state.scanning || generation !== state.cameraGeneration) {
                return;
            }
            state.scanning = false;
            callbackControls.stop();
            const barcode = typeof result.getText === 'function' ? result.getText() : result.text;
            handleBarcode(barcode);
        });

        if (generation !== state.cameraGeneration) {
            controls.stop();
            return;
        }
        state.zxingControls = controls;
        state.stream = video.srcObject;
        window.clearTimeout(state.cameraWaitTimer);
        state.cameraWaitTimer = null;
        showCameraAlert();
        setCameraActive(true);
    }

    function cameraErrorMessage(error) {
        if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
            return secureVersionUrl()
                ? 'Aparat na żywo działa tylko przez HTTPS. Otwórz spiżarnię przez bezpieczne połączenie albo zrób zdjęcie kodu.'
                : 'Aparat wymaga bezpiecznego połączenia HTTPS. Nadal możesz zrobić zdjęcie lub wpisać kod ręcznie.';
        }
        if (error?.name === 'NotAllowedError') {
            return 'Brak dostępu do aparatu. Zezwól na użycie kamery albo wpisz kod ręcznie.';
        }
        if (error?.name === 'NotFoundError') {
            return 'Nie znaleziono aparatu. Zrób zdjęcie kodu lub wpisz go ręcznie.';
        }
        if (error?.name === 'NotReadableError') {
            return 'Aparat jest zajęty przez inną aplikację. Zamknij ją lub użyj kodu ręcznego.';
        }
        return error?.message || 'Nie udało się uruchomić aparatu. Możesz wpisać kod ręcznie.';
    }

    async function startScanner() {
        state.flowGeneration += 1;
        cancelCountdown();
        stopCamera();
        stopProductCamera();
        cleanupCrop();
        state.photoUploadController?.abort();
        state.photoUploadController = null;
        state.actionLocked = false;
        state.retryCallback = null;
        state.product = null;
        state.catalog = null;
        state.registerMode = 'one';
        state.activeBarcode = null;
        state.scanId = null;
        state.photoPickerPending = false;
        state.photoPickerInput = null;
        state.pendingPhotoRequest = null;
        elements.manualCode.value = '';
        elements.registerName.value = '';
        elements.registerQuantityPerScan.value = '1';
        elements.registerUnit.value = 'szt';
        elements.registerCount.value = '2';
        elements.registerCategory.value = '';
        resetProductPhoto();
        resetCatalogPresentation();
        showPanel('scanning');
        showCameraAlert();
        setCameraStatus('Uruchamiam aparat…', true);
        resetButtons();
        const generation = state.cameraGeneration;

        if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
            setCameraStatus('Tryb ręczny');
            showInsecureContextAlert();
            announce('Aparat jest niedostępny. Wpisz kod ręcznie lub zrób zdjęcie.');
            return;
        }

        try {
            state.cameraWaitTimer = window.setTimeout(() => {
                if (generation === state.cameraGeneration && !state.scanning) {
                    setCameraStatus('Czekam na zgodę');
                    showCameraAlert('Potwierdź dostęp do aparatu. W międzyczasie możesz zrobić zdjęcie lub wpisać kod ręcznie.');
                }
            }, 8000);
            const detector = await supportedNativeDetector();
            if (generation !== state.cameraGeneration) {
                return;
            }
            if (detector) {
                await startNativeCamera(detector, generation);
            } else {
                await startZxingCamera(generation);
            }
        } catch (error) {
            if (generation !== state.cameraGeneration) {
                return;
            }
            stopCamera();
            setCameraStatus('Tryb ręczny');
            const message = cameraErrorMessage(error);
            showCameraAlert(message);
            announce(message);
        }
    }

    function resetButtons() {
        selectAll('[data-known-action], [data-register-mode], [data-register-submit], [data-edit-catalog], [data-photo-source]').forEach((button) => {
            button.disabled = false;
        });
        selectAll('[data-button-spinner]').forEach((spinner) => spinner.classList.add('d-none'));
    }

    async function resolveBarcode() {
        setCameraStatus('Sprawdzam produkt…', true);
        try {
            const data = await fetchJson(`${endpoints.lookup}?barcode=${encodeURIComponent(state.activeBarcode)}`);
            state.retryCallback = null;
            state.autoActionEnabled = data.auto_action_enabled === true;
            const configuredTimeout = Number(data.timeout_ms);
            state.autoActionTimeoutMs = Number.isFinite(configuredTimeout) && configuredTimeout > 0
                ? configuredTimeout
                : 5000;
            if (data.status === 'known') {
                presentKnownProduct(data.product, data.catalog, state.autoActionTimeoutMs);
            } else {
                presentUnknownProduct(data.barcode, data.catalog, state.autoActionTimeoutMs);
            }
        } catch (error) {
            showError(error.message, resolveBarcode);
        }
    }

    async function handleBarcode(rawBarcode) {
        if (state.actionLocked || ['known', 'unknown', 'register', 'success'].includes(state.panel)) {
            return;
        }
        let barcode;
        try {
            barcode = cleanBarcode(rawBarcode);
        } catch (error) {
            showCameraAlert(error.message);
            announce(error.message);
            return;
        }

        state.actionLocked = true;
        stopCamera();
        state.activeBarcode = barcode;
        state.scanId = makeScanId();
        state.actionLocked = false;
        announce(`Rozpoznano kod ${barcode}. Sprawdzam produkt.`);
        await resolveBarcode();
    }

    function presentKnownProduct(product, catalog, timeout = state.autoActionTimeoutMs) {
        cancelCountdown();
        state.product = product;
        state.catalog = catalog || null;
        showPanel('known');
        setCameraStatus('Kod rozpoznany');
        const scanQuantity = formatQuantity(product.quantity_per_scan, product.unit_label);
        if (elements.knownCount) {
            elements.knownCount.value = '1';
        }
        elements.knownName.textContent = product.name;
        if (elements.knownPackageStock) {
            elements.knownPackageStock.textContent = formatNumber(product.current_package_count);
        }
        elements.knownStock.textContent = formatQuantity(product.current_quantity, product.unit_label);
        elements.knownScanQuantity.textContent = scanQuantity;
        updateKnownActionLabels();
        const catalogFound = ['found', 'found_incomplete'].includes(catalog?.status);
        const imageUrl = product.image_url || (catalogFound ? catalog.image_url : '');
        const hasImage = Boolean(imageUrl);
        setResultImage(
            elements.knownImageWrap,
            elements.knownImage,
            elements.knownIcon,
            imageUrl,
            `Zdjęcie produktu ${product.name}`,
        );
        if (elements.knownPhotoWrap) {
            elements.knownPhotoWrap.hidden = hasImage;
        }
        setCountdownAvailability('known', hasImage);
        const catalogMeta = catalogFound
            ? [catalog.brand, catalog.quantity_text].filter(Boolean).join(' · ')
            : '';
        elements.knownCatalogMeta.textContent = catalogMeta;
        elements.knownCatalogMeta.hidden = !catalogMeta;
        const description = catalogFound ? catalogDescription(catalog) : '';
        elements.knownDescription.textContent = description;
        elements.knownDescription.hidden = !description;
        setSourceLink(elements.knownSource, catalogFound ? catalog : null);
        if (!state.autoActionEnabled) {
            announce(`Rozpoznano ${product.name}. Wybierz ręcznie, czy produkt zużyto, czy dodano.`);
            return;
        }
        if (!hasImage) {
            announce(`Rozpoznano ${product.name}. Brakuje zdjęcia, więc automatyczne zużycie jest wstrzymane.`);
            return;
        }
        const timeoutSeconds = Math.max(1, Math.ceil(timeout / 1000));
        announce(`Rozpoznano ${product.name}. Domyślnie za ${timeoutSeconds} sekund zostanie zapisane zużycie.`);
        startCountdown('known', timeout, () => performKnownAction('consume'));
    }

    function applyCatalogSuggestion(catalog) {
        const catalogFound = ['found', 'found_incomplete'].includes(catalog?.status);
        if (!catalogFound) {
            return;
        }
        elements.registerName.value = catalog.name || '';
        elements.registerQuantityPerScan.value = catalog.suggested_quantity_per_scan || '1';
        elements.registerUnit.value = catalog.suggested_unit || 'opak';
        elements.registerQuantityPerScan.step = elements.registerUnit.value === 'szt' ? '1' : '0.01';
        elements.registerCategory.value = catalog.suggested_category || '';
    }

    function addCatalogProductAutomatically() {
        if (!state.catalog?.can_auto_register) {
            openRegistration('one');
            return;
        }
        state.registerMode = 'one';
        elements.registerCount.value = '1';
        registerProduct({ automatic: true });
    }

    function presentUnknownProduct(
        barcode,
        catalog = state.catalog,
        timeout = state.autoActionTimeoutMs,
    ) {
        cancelCountdown();
        state.catalog = catalog || null;
        showPanel('unknown');
        setCameraStatus('Nowy kod');
        elements.unknownCode.textContent = barcode;
        elements.registerCode.textContent = barcode;
        applyCatalogSuggestion(state.catalog);

        const catalogFound = ['found', 'found_incomplete'].includes(state.catalog?.status);
        const canAutoRegister = Boolean(catalogFound && state.catalog.can_auto_register);
        const hasImage = Boolean(catalogFound && state.catalog.image_url);
        elements.unknownEyebrow.classList.toggle('text-warning', !catalogFound);
        elements.unknownEyebrow.classList.toggle('text-success', catalogFound);
        elements.unknownEyebrow.textContent = catalogFound
            ? (state.catalog.remembered ? 'Zapamiętany w domu' : 'Znaleziono w katalogu')
            : 'Nowy kod';
        elements.unknownTitle.textContent = catalogFound
            ? (state.catalog.name || 'Produkt wymaga uzupełnienia')
            : 'Tego produktu jeszcze nie znamy';
        const catalogMeta = catalogFound
            ? [state.catalog.brand, state.catalog.quantity_text].filter(Boolean).join(' · ')
            : '';
        elements.unknownMeta.textContent = catalogMeta;
        elements.unknownMeta.hidden = !catalogMeta;
        const description = catalogFound ? catalogDescription(state.catalog) : '';
        elements.unknownDescription.textContent = description;
        elements.unknownDescription.hidden = !description;
        setResultImage(
            elements.unknownImageWrap,
            elements.unknownImage,
            elements.unknownIcon,
            hasImage ? state.catalog.image_url : '',
            `Zdjęcie produktu ${state.catalog?.name || barcode}`,
        );
        if (elements.unknownPhotoWrap) {
            elements.unknownPhotoWrap.hidden = hasImage;
        }
        setCountdownAvailability('unknown', hasImage);
        setSourceLink(elements.unknownSource, catalogFound ? state.catalog : null);
        elements.editCatalog.hidden = !canAutoRegister;
        elements.registerOneDetail.textContent = canAutoRegister
            ? formatQuantity(
                state.catalog.suggested_quantity_per_scan || '1',
                catalogUnitLabel(state.catalog.suggested_unit),
            )
            : 'jedno opakowanie';

        if (!state.autoActionEnabled) {
            if (canAutoRegister) {
                elements.unknownHelp.textContent = 'Sprawdź dane produktu, opcjonalnie dodaj zdjęcie, a następnie wybierz „Dodaj” albo „Dodaj wiele”.';
                announce(`Znaleziono ${state.catalog.name} ${catalogWhereFound(state.catalog)}. Wybierz sposób dodania produktu.`);
            } else if (catalogNeedsPolishName(state.catalog)) {
                elements.unknownHelp.textContent = `Baza zna ten produkt tylko pod nazwą w języku ${catalogLanguageLabel(state.catalog)}. `
                    + 'Wybierz „Dodaj” i wpisz polską nazwę - zapamiętam ją dla całego domu.';
                announce('Znaleziono produkt z nazwą w obcym języku. Przy dodawaniu wpisz polską nazwę.');
            } else if (state.catalog?.status === 'unavailable') {
                elements.unknownHelp.textContent = 'Nie udało się teraz sprawdzić katalogu. Dane możesz uzupełnić ręcznie.';
                announce('Nie udało się sprawdzić katalogu. Wybierz sposób ręcznego dodania produktu.');
            } else if (state.catalog?.status === 'unsupported') {
                elements.unknownHelp.textContent = 'Ten typ kodu nie występuje w katalogu produktów. Uzupełnij dane ręcznie.';
                announce('Kod nie jest obsługiwany przez katalog. Wybierz sposób ręcznego dodania produktu.');
            } else if (catalogFound) {
                elements.unknownHelp.textContent = 'Katalog nie ma pełnej nazwy produktu. Uzupełnij brakujące dane.';
                announce('Katalog nie ma pełnych danych. Wybierz sposób dodania i uzupełnij produkt.');
            } else {
                elements.unknownHelp.textContent = 'Nie znaleziono produktu w katalogu. Podaj nazwę, a kod zapiszemy lokalnie.';
                announce('Kod nie jest przypisany do spiżarni. Wybierz sposób dodania produktu.');
            }
            return;
        }

        if (!hasImage) {
            elements.unknownHelp.textContent = 'Brakuje zdjęcia produktu, dlatego automatyczne odliczanie jest wstrzymane. Zrób zdjęcie albo wybierz sposób dodania.';
            announce('Brakuje zdjęcia produktu. Automatyczne dodanie jest wstrzymane do czasu Twojej decyzji.');
            return;
        }

        if (canAutoRegister) {
            const timeoutSeconds = Math.max(1, Math.ceil(timeout / 1000));
            elements.unknownCountdownLabel.innerHTML = 'Domyślnie: <strong>dodaj 1</strong>';
            elements.unknownHelp.textContent = `Sprawdź podpowiedź. Jeśli nic nie zmienisz, za ${timeoutSeconds} sekund dodamy jedno opakowanie.`;
            announce(`Znaleziono ${state.catalog.name} ${catalogWhereFound(state.catalog)}. Za ${timeoutSeconds} sekund dodamy jedno opakowanie.`);
            startCountdown('unknown', timeout, addCatalogProductAutomatically);
            return;
        }

        elements.unknownCountdownLabel.innerHTML = 'Za chwilę: <strong>formularz dla 1 opakowania</strong>';
        if (catalogNeedsPolishName(state.catalog)) {
            elements.unknownHelp.textContent = `Baza zna ten produkt tylko pod nazwą w języku ${catalogLanguageLabel(state.catalog)}. Za chwilę otworzę formularz - wpisz polską nazwę.`;
        } else if (state.catalog?.status === 'unavailable') {
            elements.unknownHelp.textContent = 'Nie udało się teraz sprawdzić katalogu. Dane możesz uzupełnić ręcznie.';
        } else if (state.catalog?.status === 'unsupported') {
            elements.unknownHelp.textContent = 'Ten typ kodu nie występuje w katalogu produktów. Uzupełnij dane ręcznie.';
        } else if (catalogFound) {
            elements.unknownHelp.textContent = 'Katalog nie ma pełnej nazwy produktu. Uzupełnij brakujące dane.';
        } else {
            elements.unknownHelp.textContent = 'Nie znaleziono produktu w katalogu. Podaj nazwę, a kod zapiszemy lokalnie.';
        }
        announce('Kod nie jest przypisany do spiżarni. Uzupełnij dane produktu.');
        startCountdown('unknown', timeout, () => openRegistration('one'));
    }

    async function performKnownAction(action) {
        if (state.actionLocked) {
            return;
        }
        const count = Number(elements.knownCount?.value || 1);
        if (!Number.isInteger(count) || count < 1 || count > 9999) {
            elements.knownCount?.reportValidity();
            return;
        }
        state.actionLocked = true;
        state.lastActionCount = count;
        cancelCountdown();
        selectAll('[data-known-action]').forEach((button) => {
            button.disabled = true;
        });
        announce(action === 'consume' ? 'Zapisuję zużycie.' : 'Dodaję produkt do stanu.');

        const retry = () => performKnownAction(action);
        try {
            const data = await fetchJson(endpoints.action, {
                method: 'POST',
                body: JSON.stringify({
                    barcode: state.activeBarcode,
                    scan_id: state.scanId,
                    action,
                    count,
                }),
            });
            state.actionLocked = false;
            state.changed = true;
            state.changedProducts.add(data.product.id);
            updateVisibleStock(data.product);
            showSuccess(data);
        } catch (error) {
            state.actionLocked = false;
            resetButtons();
            showError(error.message, retry);
        }
    }

    function openRegistration(mode) {
        cancelCountdown();
        state.registerMode = mode;
        state.actionLocked = false;
        showPanel('register');
        const isMany = mode === 'many';
        elements.registerTitle.textContent = isMany ? 'Dodaj wiele opakowań' : 'Dodaj jedno opakowanie';
        elements.registerCountWrap.hidden = !isMany;
        elements.registerCount.required = isMany;
        // Pole ma min="2" (tryb "Dodaj wiele"). W trybie "Dodaj" jest ukryte
        // i ma wartość 1, więc bez wyłączenia walidacja HTML po cichu
        // blokowała wysłanie formularza - przeglądarka nie może nawet pokazać
        // komunikatu przy niewidocznym polu. Wyłączone pole nie jest
        // walidowane, a w tym trybie i tak wysyłamy count = 1.
        elements.registerCount.disabled = !isMany;
        elements.registerCount.value = isMany ? '2' : '1';
        elements.registerSubmitLabel.textContent = isMany ? 'Dodaj do spiżarni' : 'Dodaj produkt';
        updateRegisterCatalogNote(state.catalog);
        elements.registerName.focus({ preventScroll: false });
        if (catalogNeedsPolishName(state.catalog)) {
            // Obca nazwa jest zaznaczona - pierwsze naciśnięcie klawisza ją zastępuje.
            elements.registerName.select();
        }
        updateRegisterTotalPreview();
        announce(isMany ? 'Wpisz dane produktu i liczbę opakowań.' : 'Wpisz nazwę nowego produktu.');
    }

    async function registerProduct(options = {}) {
        if (state.actionLocked) {
            return;
        }
        state.actionLocked = true;
        cancelCountdown();
        elements.registerSubmit.disabled = true;
        select('[data-button-spinner]', elements.registerSubmit)?.classList.remove('d-none');
        selectAll('[data-register-mode]').forEach((button) => {
            button.disabled = true;
        });
        if (options.automatic) {
            setCameraStatus('Dodaję produkt z katalogu…', true);
            announce('Dodaję rozpoznany produkt do spiżarni.');
        }
        const count = state.registerMode === 'many' ? elements.registerCount.value : 1;
        state.lastActionCount = Number(count) || 1;
        const payload = {
            barcode: state.activeBarcode,
            scan_id: state.scanId,
            name: elements.registerName.value,
            quantity_per_scan: elements.registerQuantityPerScan.value,
            unit: elements.registerUnit.value,
            count,
            category: elements.registerCategory.value,
        };
        const retry = () => registerProduct(options);

        try {
            let requestBody;
            if (state.photoFile) {
                requestBody = new FormData();
                Object.entries(payload).forEach(([key, value]) => requestBody.append(key, value));
                requestBody.append('image', state.photoFile, state.photoFile.name);
            } else {
                requestBody = JSON.stringify(payload);
            }
            const data = await fetchJson(endpoints.register, {
                method: 'POST',
                body: requestBody,
            });
            state.actionLocked = false;
            state.changed = true;
            // Nowy produkt nie ma jeszcze kafelka - tu potrzebne jest pełne odświeżenie.
            state.needsReload = true;
            showSuccess(data);
        } catch (error) {
            state.actionLocked = false;
            resetButtons();
            showError(error.message, retry);
        }
    }

    function updateVisibleStock(product) {
        const row = select(`[data-pantry-product-id="${product.id}"]`);
        const packageStock = row ? select('[data-product-package-stock]', row) : null;
        const totalStock = row ? select('[data-product-stock]', row) : null;
        if (packageStock) {
            packageStock.textContent = formatNumber(product.current_package_count);
        }
        if (totalStock) {
            totalStock.textContent = formatNumber(product.current_quantity);
        }
    }

    function showSuccess(data) {
        showPanel('success');
        resetButtons();
        const wasConsumed = data.action === 'consume';
        elements.successEyebrow.textContent = wasConsumed ? 'Zapisano zużycie' : 'Dodano do spiżarni';
        elements.successTitle.textContent = data.product.name;
        const delta = formatQuantity(data.quantity, data.product.unit_label);
        const fulfilledCount = Number(data.count ?? state.lastActionCount ?? 1);
        const requestedCount = Number(data.requested_count ?? fulfilledCount);
        const packageDelta = `${formatNumber(fulfilledCount)} opak.`;
        const stock = formatProductStock(data.product);
        if (wasConsumed) {
            if (data.stock_was_insufficient && fulfilledCount === 0) {
                elements.successMessage.textContent = `Nie odjęto produktu — zapisany zapas był już pusty. Aktualny stan: ${stock}.`;
            } else if (data.stock_was_insufficient && requestedCount > fulfilledCount) {
                elements.successMessage.textContent = `Zużyto dostępne ${packageDelta} (${delta}) z żądanych ${formatNumber(requestedCount)} szt. Aktualny stan: ${stock}.`;
            } else if (data.stock_was_insufficient) {
                elements.successMessage.textContent = `Zużyto ${packageDelta} (${delta}). Stan wyzerowano, ponieważ zapisany zapas był mniejszy. Aktualny stan: ${stock}.`;
            } else {
                elements.successMessage.textContent = `Zużyto ${packageDelta} (${delta}). Aktualny stan: ${stock}.`;
            }
        } else {
            elements.successMessage.textContent = `Dodano ${packageDelta} (${delta}). Aktualny stan: ${stock}.`;
        }
        announce(`${elements.successEyebrow.textContent}: ${data.product.name}. ${elements.successMessage.textContent}`);
    }

    function showError(message, retryCallback = null) {
        cancelCountdown();
        showPanel('error');
        elements.errorMessage.textContent = message;
        state.retryCallback = retryCallback;
        elements.retryButton.classList.toggle('d-none', !retryCallback);
        announce(`Błąd. ${message}`);
    }

    async function decodePhoto(file) {
        stopCamera();
        showPanel('scanning');
        showCameraAlert();
        setCameraStatus('Odczytuję zdjęcie…', true);
        const image = new Image();
        const imageUrl = URL.createObjectURL(file);
        image.src = imageUrl;

        try {
            await image.decode();
            let barcode = null;
            const detector = await supportedNativeDetector();
            if (detector) {
                const results = await detector.detect(image);
                barcode = results.find((result) => result.rawValue)?.rawValue || null;
            }
            if (!barcode) {
                const ZXingBrowser = await loadZxing();
                const reader = new ZXingBrowser.BrowserMultiFormatReader();
                const result = await reader.decodeFromImageElement(image);
                barcode = typeof result.getText === 'function' ? result.getText() : result.text;
            }
            await handleBarcode(barcode);
        } catch (error) {
            setCameraStatus('Nie odczytano kodu');
            const message = 'Nie udało się znaleźć kodu na zdjęciu. Spróbuj ponownie lub wpisz kod ręcznie.';
            showCameraAlert(message);
            announce(message);
        } finally {
            URL.revokeObjectURL(imageUrl);
            elements.codePhoto.value = '';
        }
    }

    async function uploadKnownProductPhoto(file) {
        if (!file || !state.product?.image_upload_url || state.actionLocked) {
            return;
        }
        const flowGeneration = state.flowGeneration;
        state.photoUploadController?.abort();
        const controller = new AbortController();
        state.photoUploadController = controller;
        cancelCountdown();
        state.actionLocked = true;
        const body = new FormData();
        body.append('image', file, file.name);
        setCameraStatus('Zapisuję zdjęcie…', true);
        const retry = () => uploadKnownProductPhoto(file);
        try {
            const data = await fetchJson(state.product.image_upload_url, {
                method: 'POST',
                body,
                signal: controller.signal,
            });
            if (flowGeneration !== state.flowGeneration || !state.modalActive) {
                return;
            }
            state.actionLocked = false;
            state.changed = true;
            state.changedProducts.add(data.product.id);
            state.product = data.product;
            announce('Zdjęcie produktu zostało zapisane lokalnie. Wybierz akcję dla produktu.');
            presentKnownProduct(data.product, state.catalog, state.autoActionTimeoutMs);
        } catch (error) {
            if (error.name === 'AbortError' || flowGeneration !== state.flowGeneration || !state.modalActive) {
                return;
            }
            state.actionLocked = false;
            resetButtons();
            showError(error.message, retry);
        } finally {
            if (state.photoUploadController === controller) {
                state.photoUploadController = null;
            }
        }
    }

    modalElement.addEventListener('shown.bs.modal', () => {
        state.modalActive = true;
        startScanner();
    });
    modalElement.addEventListener('hidden.bs.modal', () => {
        state.modalActive = false;
        state.flowGeneration += 1;
        cancelCountdown();
        stopCamera();
        stopProductCamera();
        state.photoUploadController?.abort();
        state.photoUploadController = null;
        state.actionLocked = false;
        cleanupCrop();
        resetProductPhoto();
        state.photoPickerPending = false;
        state.photoPickerInput = null;
        state.pendingPhotoRequest = null;
        if (state.changed) {
            // Zmienione kafelki i podsumowanie podmieniają się bez przeładowania
            // i bez powrotu na górę strony. Nowy produkt wymaga przeładowania.
            const ids = [...state.changedProducts];
            state.changed = false;
            state.changedProducts.clear();
            if (state.needsReload || !window.AppPartial || !ids.length) {
                state.needsReload = false;
                window.location.reload();
            } else {
                const cards = ids.map((id) => `#product-${id}`).join(', ');
                window.AppPartial.refresh(`${cards}, #pantry-metrics, [data-panel-badges]`)
                    .catch(() => window.location.reload());
            }
        }
    });

    elements.manualForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        await handleBarcode(elements.manualCode.value);
    });

    elements.codePhoto.addEventListener('change', async () => {
        const [file] = elements.codePhoto.files;
        if (file) {
            await decodePhoto(file);
        }
    });

    selectAll('[data-photo-source]').forEach((button) => {
        button.addEventListener('click', () => {
            if (state.actionLocked) {
                return;
            }
            const request = {
                target: button.dataset.photoTarget || 'register',
                returnPanel: state.panel,
                registerMode: state.registerMode,
            };
            pauseCountdown();
            if (button.dataset.photoSource === 'camera') {
                startProductCamera(request);
                return;
            }
            if (!elements.photoGalleryInput) {
                announce('Wybór zdjęcia jest niedostępny w tej przeglądarce.');
                resumeCountdown();
                return;
            }
            openPhotoInput(elements.photoGalleryInput, request);
        });
    });

    elements.productCameraCapture?.addEventListener('click', captureProductCameraPhoto);
    elements.productCameraCancel?.addEventListener('click', cancelProductCamera);
    elements.productCameraRetry?.addEventListener('click', () => startProductCamera());
    elements.productCameraGallery?.addEventListener('click', () => {
        const request = state.productCameraRequest;
        stopProductCamera({ keepRequest: true });
        if (!request) {
            return;
        }
        openPhotoInput(elements.photoGalleryInput, request);
    });
    elements.productCameraSystem?.addEventListener('click', () => {
        const request = state.productCameraRequest;
        stopProductCamera({ keepRequest: true });
        if (!request) {
            return;
        }
        openPhotoInput(elements.photoCameraInput, request);
    });

    [elements.photoCameraInput, elements.photoGalleryInput].forEach((input) => {
        input?.addEventListener('change', () => {
            const [file] = input.files;
            const request = state.pendingPhotoRequest;
            state.photoPickerPending = false;
            state.photoPickerInput = null;
            state.pendingPhotoRequest = null;
            input.value = '';
            if (!file) {
                if (state.panel === 'photo-camera') {
                    setProductCameraLoading('Aparat wstrzymany');
                    elements.productCameraRetry.hidden = false;
                } else {
                    resumeCountdown();
                }
                return;
            }
            if (state.panel === 'photo-camera') {
                stopProductCamera({ keepRequest: true });
            }
            openCropEditor(file, request).then((opened) => {
                if (!opened && state.panel === 'photo-camera') {
                    state.productCameraRequest = request;
                    setProductCameraLoading('Nieobsługiwany plik');
                    elements.productCameraRetry.hidden = false;
                }
            });
        });
        input?.addEventListener('cancel', () => {
            state.photoPickerPending = false;
            state.photoPickerInput = null;
            state.pendingPhotoRequest = null;
            if (state.panel === 'photo-camera') {
                setProductCameraLoading('Aparat wstrzymany');
                elements.productCameraRetry.hidden = false;
            } else {
                resumeCountdown();
            }
        });
    });

    elements.cropViewport?.addEventListener('pointerdown', (event) => {
        const crop = state.crop;
        if (!crop?.ready || (event.pointerType === 'mouse' && event.button !== 0)) {
            return;
        }
        event.preventDefault();
        elements.cropViewport.setPointerCapture?.(event.pointerId);
        crop.drag = {
            pointerId: event.pointerId,
            startX: event.clientX,
            startY: event.clientY,
            offsetX: crop.offsetX,
            offsetY: crop.offsetY,
        };
        elements.cropViewport.classList.add('is-dragging');
    });

    elements.cropViewport?.addEventListener('pointermove', (event) => {
        const crop = state.crop;
        if (!crop?.ready || crop.drag?.pointerId !== event.pointerId) {
            return;
        }
        event.preventDefault();
        crop.offsetX = crop.drag.offsetX + event.clientX - crop.drag.startX;
        crop.offsetY = crop.drag.offsetY + event.clientY - crop.drag.startY;
        renderCrop();
    });

    const finishCropDrag = (event) => {
        const crop = state.crop;
        if (!crop || crop.drag?.pointerId !== event.pointerId) {
            return;
        }
        crop.drag = null;
        elements.cropViewport.classList.remove('is-dragging');
        if (elements.cropViewport.hasPointerCapture?.(event.pointerId)) {
            elements.cropViewport.releasePointerCapture(event.pointerId);
        }
    };
    elements.cropViewport?.addEventListener('pointerup', finishCropDrag);
    elements.cropViewport?.addEventListener('pointercancel', finishCropDrag);

    elements.cropZoom?.addEventListener('input', () => {
        const crop = state.crop;
        if (!crop?.ready) {
            return;
        }
        const nextZoom = Number(elements.cropZoom.value || 1);
        const ratio = nextZoom / crop.zoom;
        crop.offsetX *= ratio;
        crop.offsetY *= ratio;
        crop.zoom = nextZoom;
        renderCrop();
    });

    elements.cropReset?.addEventListener('click', resetCropPosition);
    elements.cropCancel?.addEventListener('click', cancelCrop);
    elements.cropConfirm?.addEventListener('click', confirmCrop);

    selectAll('[data-known-action]').forEach((button) => {
        button.addEventListener('click', () => performKnownAction(button.dataset.knownAction));
    });

    selectAll('[data-register-mode]').forEach((button) => {
        button.addEventListener('click', () => {
            const mode = button.dataset.registerMode;
            if (mode === 'one' && state.catalog?.can_auto_register) {
                addCatalogProductAutomatically();
                return;
            }
            openRegistration(mode);
        });
    });

    elements.editCatalog.addEventListener('click', () => openRegistration('one'));

    select('[data-back-to-unknown]').addEventListener('click', () => {
        presentUnknownProduct(state.activeBarcode);
    });

    elements.registerForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (!elements.registerForm.reportValidity()) {
            return;
        }
        await registerProduct();
    });

    elements.registerUnit.addEventListener('change', () => {
        const integerOnly = elements.registerUnit.value === 'szt';
        elements.registerQuantityPerScan.step = integerOnly ? '1' : '0.01';
        updateRegisterTotalPreview();
    });

    elements.registerQuantityPerScan.addEventListener('input', updateRegisterTotalPreview);
    elements.registerCount.addEventListener('input', updateRegisterTotalPreview);
    elements.knownCount?.addEventListener('input', () => {
        cancelCountdown();
        updateKnownActionLabels();
        const countdown = select('[data-countdown="known"]');
        if (countdown) {
            countdown.hidden = true;
        }
    });

    selectAll('[data-scan-again]').forEach((button) => {
        button.addEventListener('click', startScanner);
    });

    // Operacje ręczne wysyłają się w tle (data-partial, static/js/app-actions.js).
    // Stan na kafelku zmienia się od razu, zanim serwer odpowie; odpowiedź
    // podmienia kafelek na dokładny (prognoza, znacznik stanu, grupa).
    if (window.AppPartial) {
        const numberFormat = new Intl.NumberFormat('pl-PL', { maximumFractionDigits: 2 });
        window.AppPartial.optimistic['pantry-movement'] = (form) => {
            const card = form.closest('.pantry-product-card');
            if (!card) {
                return null;
            }
            const type = select('input[name="movement_type"]', form)?.value;
            const packagesInput = select('input[name="package_count"]', form);
            const quantityInput = select('input[name="quantity"]', form);
            const sign = type === 'consume' ? -1 : 1;
            const size = Number(card.dataset.scanSize) || 0;
            let packages = Number(card.dataset.stockPackages) || 0;
            let quantity = Number(card.dataset.stockQuantity) || 0;
            if (packagesInput) {
                const count = Number(packagesInput.value) || 0;
                packages = Math.max(0, packages + sign * count);
                quantity = Math.max(0, quantity + sign * count * size);
            } else if (quantityInput) {
                quantity = Math.max(0, quantity + sign * (Number(String(quantityInput.value).replace(',', '.')) || 0));
            }
            if (quantity === 0) {
                packages = 0;
            }
            const packageEl = select('[data-product-package-stock]', card);
            const quantityEl = select('[data-product-stock]', card);
            const before = [packageEl?.textContent, quantityEl?.textContent];
            if (packageEl && packagesInput && packageEl.textContent.trim() !== '—') {
                packageEl.textContent = numberFormat.format(packages);
            }
            if (quantityEl) {
                quantityEl.textContent = numberFormat.format(quantity);
            }
            card.classList.add('is-stock-changed');
            return () => {
                if (packageEl) {
                    packageEl.textContent = before[0];
                }
                if (quantityEl) {
                    quantityEl.textContent = before[1];
                }
                card.classList.remove('is-stock-changed');
            };
        };
    }

    elements.retryButton.addEventListener('click', () => {
        const retry = state.retryCallback;
        state.retryCallback = null;
        if (retry) {
            retry();
        }
    });

    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            pauseCountdown();
            if (state.panel === 'photo-camera') {
                stopProductCamera({ keepRequest: true });
                setProductCameraLoading('Aparat wstrzymany');
                elements.productCameraRetry.hidden = false;
            }
        } else if (!state.photoPickerPending && !['crop', 'photo-camera'].includes(state.panel)) {
            resumeCountdown();
        }
    });

    window.addEventListener('focus', () => {
        if (!state.photoPickerPending || !state.photoPickerInput) {
            return;
        }
        const activeInput = state.photoPickerInput;
        window.setTimeout(() => {
            if (!state.photoPickerPending || state.photoPickerInput !== activeInput || activeInput.files?.length) {
                return;
            }
            state.photoPickerPending = false;
            state.photoPickerInput = null;
            state.pendingPhotoRequest = null;
            if (state.panel === 'photo-camera') {
                setProductCameraLoading('Aparat wstrzymany');
                elements.productCameraRetry.hidden = false;
            } else {
                resumeCountdown();
            }
        }, 700);
    });

    window.addEventListener('resize', () => {
        if (state.panel === 'crop' && state.crop?.ready) {
            const previousScale = state.crop.geometry?.scale;
            const nextGeometry = cropGeometry();
            if (previousScale && nextGeometry) {
                const ratio = nextGeometry.scale / previousScale;
                state.crop.offsetX *= ratio;
                state.crop.offsetY *= ratio;
            }
            renderCrop();
        }
    });

    window.addEventListener('pagehide', () => {
        cancelCountdown();
        stopCamera();
        stopProductCamera();
        cleanupCrop();
        resetProductPhoto();
    });
})();
