/* Aparat czytający kody kreskowe bez przerwy - wspólny dla stron paragonu
 * („Sprawdź” i „Rozpakuj zakupy”).
 *
 * Dekodowanie jak w skanerze spiżarni (pantry-scanner.js): obraz 1080p,
 * ciągły autofokus i lekki zoom, czytany jest tylko wycinek klatki wokół
 * ramki, natywny BarcodeDetector z pomocą ZXing, a kod z błędną cyfrą
 * kontrolną jest odrzucany.
 *
 * const camera = BarcodeCamera.create({ video, frame, zxingUrl, onCode });
 * await camera.start();   // rzuca błąd, gdy aparatu nie da się uruchomić
 * camera.stop();
 * onCode(kod) może być async - następna klatka czeka na jego koniec.
 */
window.BarcodeCamera = (() => {
    'use strict';

    const FORMATS = ['ean_13', 'ean_8', 'upc_a', 'upc_e', 'code_128'];
    const FRAME_DELAY_MS = 40;
    const MAX_DECODE_WIDTH = 1280;
    // Natywny czytnik nic nie znalazł przez tyle czasu → co drugą klatkę próbuje też ZXing.
    const ZXING_ASSIST_AFTER_MS = 3000;
    const NATIVE_ERROR_LIMIT = 5;

    let zxingPromise = null;

    function loadZxing(url) {
        if (window.ZXingBrowser) {
            return Promise.resolve(window.ZXingBrowser);
        }
        if (!zxingPromise) {
            zxingPromise = new Promise((resolve, reject) => {
                const script = document.createElement('script');
                script.src = url;
                script.async = true;
                script.onload = () => (window.ZXingBrowser
                    ? resolve(window.ZXingBrowser)
                    : reject(new Error('Nie udało się uruchomić czytnika kodów.')));
                script.onerror = () => reject(new Error('Nie udało się wczytać czytnika kodów.'));
                document.head.appendChild(script);
            });
        }
        return zxingPromise;
    }

    // DecodeHintType nie jest wystawiony w paczce UMD: POSSIBLE_FORMATS = 2, TRY_HARDER = 3.
    function createZxingReader(ZXingBrowser) {
        const format = ZXingBrowser.BarcodeFormat || { CODE_128: 4, EAN_8: 6, EAN_13: 7, UPC_A: 14, UPC_E: 15 };
        const hints = new Map([
            [2, [format.EAN_13, format.EAN_8, format.UPC_A, format.UPC_E, format.CODE_128]],
            [3, true],
        ]);
        return new ZXingBrowser.BrowserMultiFormatReader(hints);
    }

    async function nativeDetector() {
        if (!('BarcodeDetector' in window)) {
            return null;
        }
        try {
            const supported = await window.BarcodeDetector.getSupportedFormats();
            const formats = FORMATS.filter((format) => supported.includes(format));
            return formats.length ? new window.BarcodeDetector({ formats }) : null;
        } catch (error) {
            return null;
        }
    }

    // EAN-13 i UPC-A mają cyfrę kontrolną; odczyt z rozmytej klatki bywa przekłamany.
    function hasValidCheckDigit(code) {
        if (!/^\d{12,13}$/.test(code)) {
            return true;
        }
        const digits = code.split('').map(Number);
        const check = digits.pop();
        const sum = digits.reverse().reduce((total, digit, index) => total + digit * (index % 2 === 0 ? 3 : 1), 0);
        return (10 - (sum % 10)) % 10 === check;
    }

    const clamp = (value, minimum, maximum) => Math.min(Math.max(value, minimum), maximum);

    async function applyTrackSetting(track, setting) {
        try {
            await track.applyConstraints({ advanced: [setting] });
        } catch (error) {
            // Przeglądarka, która czegoś nie umie, nie blokuje reszty ustawień.
        }
    }

    async function tuneTrack(track) {
        const capabilities = track?.getCapabilities?.() || {};
        if (capabilities.focusMode?.includes('continuous')) {
            await applyTrackSetting(track, { focusMode: 'continuous' });
        }
        const zoom = capabilities.zoom;
        if (zoom && Number(zoom.max) >= 1.5) {
            await applyTrackSetting(track, { zoom: clamp(1.5, Number(zoom.min) || 1, Number(zoom.max)) });
        }
    }

    function create({ video, frame = null, zxingUrl, onCode }) {
        const state = {
            generation: 0, stream: null, timer: null, detector: null, zxingReader: null,
            canvas: null, context: null, nativeErrors: 0, startedAt: 0, frameIndex: 0, lastFrameTime: -1,
        };

        function ensureZxing(generation) {
            if (state.zxingReader) {
                return Promise.resolve(state.zxingReader);
            }
            return loadZxing(zxingUrl).then((ZXingBrowser) => {
                if (generation === state.generation && !state.zxingReader) {
                    state.zxingReader = createZxingReader(ZXingBrowser);
                }
                return state.zxingReader;
            }).catch(() => null);
        }

        // Wycinek klatki wokół ramki celownika (podgląd ma object-fit: cover).
        function scanRegion() {
            const width = video.videoWidth;
            const height = video.videoHeight;
            const box = video.getBoundingClientRect();
            const target = frame?.getBoundingClientRect();
            if (!box.width || !box.height || !target?.width) {
                return { x: width * 0.1, y: height * 0.25, width: width * 0.8, height: height * 0.5 };
            }
            const scale = Math.max(box.width / width, box.height / height);
            const offsetX = (width * scale - box.width) / 2;
            const offsetY = (height * scale - box.height) / 2;
            const regionWidth = Math.min(box.width, target.width * 1.25);
            const regionHeight = Math.min(box.height, Math.max(target.height * 2, regionWidth * 0.5));
            const centerX = target.left + target.width / 2 - box.left + offsetX;
            const centerY = target.top + target.height / 2 - box.top + offsetY;
            const x = clamp((centerX - regionWidth / 2) / scale, 0, width - 1);
            const y = clamp((centerY - regionHeight / 2) / scale, 0, height - 1);
            return {
                x, y,
                width: Math.min(width - x, regionWidth / scale),
                height: Math.min(height - y, regionHeight / scale),
            };
        }

        function grabFrame() {
            const region = scanRegion();
            const ratio = Math.min(1, MAX_DECODE_WIDTH / region.width);
            const width = Math.max(1, Math.round(region.width * ratio));
            const height = Math.max(1, Math.round(region.height * ratio));
            if (!state.canvas) {
                state.canvas = document.createElement('canvas');
                state.context = state.canvas.getContext('2d', { willReadFrequently: true });
            }
            if (state.canvas.width !== width || state.canvas.height !== height) {
                state.canvas.width = width;
                state.canvas.height = height;
            }
            state.context.drawImage(video, region.x, region.y, region.width, region.height, 0, 0, width, height);
            return state.canvas;
        }

        async function detectNative(canvas, generation) {
            try {
                const results = await state.detector.detect(canvas);
                state.nativeErrors = 0;
                return results.map((result) => result.rawValue).find(Boolean) || null;
            } catch (error) {
                // BarcodeDetector jest, ale detect() rzuca za każdym razem → ZXing.
                state.nativeErrors += 1;
                if (state.nativeErrors >= NATIVE_ERROR_LIMIT) {
                    state.detector = null;
                    ensureZxing(generation);
                }
                return null;
            }
        }

        function detectZxing(canvas) {
            try {
                const result = state.zxingReader.decodeFromCanvas(canvas);
                return typeof result.getText === 'function' ? result.getText() : result.text;
            } catch (error) {
                return null;   // w tej klatce nie ma czytelnego kodu
            }
        }

        async function readFrame(generation) {
            const canvas = grabFrame();
            if (state.detector) {
                const barcode = await detectNative(canvas, generation);
                if (barcode || performance.now() - state.startedAt < ZXING_ASSIST_AFTER_MS || state.frameIndex % 2) {
                    return barcode;
                }
                ensureZxing(generation);
            }
            return state.zxingReader ? detectZxing(canvas) : null;
        }

        function stop() {
            state.generation += 1;
            window.clearTimeout(state.timer);
            state.stream?.getTracks().forEach((track) => track.stop());
            state.stream = null;
            video.srcObject = null;
        }

        async function start() {
            stop();
            const generation = state.generation;
            if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
                const error = new Error('Aparat działa tylko przez HTTPS.');
                error.name = 'InsecureContextError';
                throw error;
            }
            state.detector = await nativeDetector();
            const zxingReady = state.detector ? null : ensureZxing(generation);
            // Wyższa rozdzielczość = więcej pikseli na kreskę: kod czyta się z dalszej odległości.
            const stream = await navigator.mediaDevices.getUserMedia({
                audio: false,
                video: {
                    facingMode: { ideal: 'environment' },
                    width: { ideal: 1920 }, height: { ideal: 1080 }, frameRate: { ideal: 30 },
                },
            });
            if (generation !== state.generation) {
                stream.getTracks().forEach((track) => track.stop());
                return;
            }
            state.stream = stream;
            video.srcObject = stream;
            await video.play();
            if (zxingReady && !(await zxingReady)) {
                stop();
                throw new Error('Nie udało się wczytać czytnika kodów.');
            }
            await tuneTrack(stream.getVideoTracks()[0]);
            if (generation !== state.generation) {
                return;
            }
            state.startedAt = performance.now();
            state.frameIndex = 0;
            state.lastFrameTime = -1;
            state.nativeErrors = 0;

            const tick = async () => {
                if (generation !== state.generation) {
                    return;
                }
                // Ta sama klatka co poprzednio - szkoda pracy na ponowny odczyt.
                if (video.readyState >= 2 && video.currentTime !== state.lastFrameTime) {
                    state.lastFrameTime = video.currentTime;
                    state.frameIndex += 1;
                    let barcode = null;
                    try {
                        barcode = await readFrame(generation);
                    } catch (error) {
                        // Pojedyncza nieczytelna klatka to norma, gdy opakowanie się rusza.
                    }
                    barcode = String(barcode || '').trim();
                    if (barcode && generation === state.generation && hasValidCheckDigit(barcode)) {
                        await onCode(barcode);
                    }
                }
                if (generation === state.generation) {
                    state.timer = window.setTimeout(tick, FRAME_DELAY_MS);
                }
            };
            state.timer = window.setTimeout(tick, FRAME_DELAY_MS);
        }

        return { start, stop, get active() { return Boolean(state.stream); } };
    }

    return { create };
})();
