"""Odczyt zdjęcia paragonu lokalnie, Tesseractem (pakiet tesseract-ocr-pol
w obrazie Dockera). Nic nie wychodzi poza dom."""
from __future__ import annotations

import io
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from PIL import Image, ImageChops, ImageFilter, ImageOps

# Zdjęcie z telefonu ma 3-5 MB; paragon czyta się dobrze już przy ~2000 px
# szerokości, więc większe zmniejszamy przed zapisem (karta SD, czas OCR).
MAX_STORED_SIDE = 3000
OCR_TARGET_WIDTH = 1600


class ReceiptOcrError(Exception):
    pass


def compress_upload(uploaded_file) -> ContentFile:
    """Zdjęcie z telefonu → JPEG o rozsądnym rozmiarze, obrócony zgodnie z EXIF."""
    try:
        image = Image.open(uploaded_file)
        image = ImageOps.exif_transpose(image)
    except Exception as exc:  # noqa: BLE001 - Pillow rzuca różne wyjątki
        raise ReceiptOcrError('To nie wygląda na zdjęcie.') from exc
    image = image.convert('RGB')
    image.thumbnail((MAX_STORED_SIDE, MAX_STORED_SIDE * 4))
    if max(image.size) > MAX_STORED_SIDE * 4:
        raise ReceiptOcrError('Zdjęcie jest zbyt duże.')
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=88, optimize=True)
    return ContentFile(buffer.getvalue())


# Ustawienia kolejnych prób odczytu: (szerokość, próg czerni/bieli albo None,
# wycięcie kartki i wyrównanie cieni). Kolejne próby ruszają tylko wtedy, gdy
# suma pozycji nie zgadza się z sumą paragonu - ta sama linia w innej skali
# albo bez cieni od zagięć często czyta się poprawnie.
OCR_VARIANTS = (
    (OCR_TARGET_WIDTH, None, False),
    (2400, 160, False),
    (2400, 180, True),
    (OCR_TARGET_WIDTH, None, True),
    (1200, 160, False),
)


def _otsu(histogram) -> int:
    """Próg oddzielający jasną kartkę od tła (metoda Otsu)."""
    total = sum(histogram)
    weighted_all = sum(index * count for index, count in enumerate(histogram))
    background = weighted_background = 0
    best, threshold = 0.0, 128
    for index, count in enumerate(histogram):
        background += count
        if not background:
            continue
        foreground = total - background
        if not foreground:
            break
        weighted_background += index * count
        mean_bg = weighted_background / background
        mean_fg = (weighted_all - weighted_background) / foreground
        between = background * foreground * (mean_bg - mean_fg) ** 2
        if between > best:
            best, threshold = between, index
    return threshold


def crop_to_paper(image):
    """Wycina jasną kartkę paragonu z tła (stół, dłoń)."""
    small = image.resize((max(1, image.width // 8), max(1, image.height // 8))).filter(ImageFilter.GaussianBlur(3))
    threshold = _otsu(small.histogram())
    mask = small.point(lambda value: 255 if value > threshold else 0)
    mask = mask.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5))
    box = mask.getbbox()
    if not box:
        return image
    left, top, right, bottom = (value * 8 for value in box)
    if (right - left) * (bottom - top) < 0.15 * image.width * image.height:
        return image
    margin = 16
    return image.crop((max(0, left - margin), max(0, top - margin),
                       min(image.width, right + margin), min(image.height, bottom + margin)))


def flatten_light(image):
    """Usuwa cienie od zagięć i nierówne światło: tło wyliczone z mocnego
    rozmycia odejmujemy od zdjęcia, zostaje sam tekst."""
    background = image.filter(ImageFilter.GaussianBlur(30))
    return ImageOps.invert(ImageChops.subtract(background, image))
MAX_PDF_PAGES = 4


def prepare_for_ocr(path, variant: int = 0) -> bytes:
    """Skala szarości, wyrównany kontrast i stała szerokość - Tesseract
    najlepiej czyta tekst o wysokości ~30 px."""
    target_width, threshold, flatten = OCR_VARIANTS[variant % len(OCR_VARIANTS)]
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert('L')
    if flatten:
        image = crop_to_paper(image)
    width, height = image.size
    if width and abs(width - target_width) > 200:
        scale = target_width / width
        image = image.resize((target_width, max(1, int(height * scale))), Image.LANCZOS)
    if flatten:
        image = flatten_light(image)
    image = ImageOps.autocontrast(image, cutoff=1)
    if threshold is not None:
        image = image.point(lambda value: 255 if value > threshold else 0)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def is_pdf(uploaded_file) -> bool:
    name = (getattr(uploaded_file, 'name', '') or '').lower()
    return name.endswith('.pdf') or getattr(uploaded_file, 'content_type', '') == 'application/pdf'


def pdf_pages(uploaded_file):
    """E-paragon w PDF (np. z aplikacji Biedronki) → (tekst z PDF-a, strony jako JPEG).

    Część PDF-ów ma warstwę tekstową - wtedy OCR jest zbędny i odczyt jest
    bezbłędny. Biedronka zapisuje paragon jako obrazki, więc strony
    renderujemy (pdftoppm, pakiet poppler-utils) i czytamy jak zdjęcia.
    """
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / 'paragon.pdf'
        with source.open('wb') as handle:
            for chunk in uploaded_file.chunks():
                handle.write(chunk)
        try:
            text = subprocess.run(
                ['pdftotext', '-layout', '-l', str(MAX_PDF_PAGES), str(source), '-'],
                capture_output=True, timeout=30, check=False,
            ).stdout.decode('utf-8', 'replace')
            rendered = subprocess.run(
                ['pdftoppm', '-r', '200', '-jpeg', '-l', str(MAX_PDF_PAGES), str(source), str(Path(folder) / 'strona')],
                capture_output=True, timeout=60, check=False,
            )
        except FileNotFoundError as exc:
            raise ReceiptOcrError('Brak programów do PDF na serwerze (poppler-utils).') from exc
        except subprocess.TimeoutExpired as exc:
            raise ReceiptOcrError('Otwieranie PDF-a trwało za długo.') from exc
        pages = sorted(Path(folder).glob('strona*.jpg'))
        if rendered.returncode != 0 or not pages:
            raise ReceiptOcrError('Nie udało się otworzyć PDF-a z paragonem.')
        images = []
        for page in pages:
            with page.open('rb') as handle:
                images.append(compress_upload(handle))
    return text.strip(), images


def read_text(path, variant: int = 0) -> str:
    if not getattr(settings, 'RECEIPT_OCR_ENABLED', True):
        raise ReceiptOcrError('Odczyt paragonów jest wyłączony (RECEIPT_OCR_ENABLED).')
    command = [
        settings.RECEIPT_TESSERACT_CMD, 'stdin', 'stdout',
        '-l', settings.RECEIPT_OCR_LANG,
        # 4 = jedna kolumna tekstu o różnej wielkości, czyli paragon.
        '--psm', '4',
        # Spacje między nazwą a ceną mają znaczenie dla parsera.
        '-c', 'preserve_interword_spaces=1',
    ]
    try:
        completed = subprocess.run(
            command, input=prepare_for_ocr(path, variant), capture_output=True,
            timeout=settings.RECEIPT_OCR_TIMEOUT, check=False,
        )
    except FileNotFoundError as exc:
        raise ReceiptOcrError('Brak programu tesseract na serwerze.') from exc
    except subprocess.TimeoutExpired as exc:
        raise ReceiptOcrError('Odczyt trwał za długo.') from exc
    if completed.returncode != 0:
        message = completed.stderr.decode('utf-8', 'replace').strip().splitlines()[-1:] or ['błąd OCR']
        raise ReceiptOcrError(message[0][:200])
    return completed.stdout.decode('utf-8', 'replace')
