"""Odczytany tekst paragonu → sklep, data, suma i pozycje.

Czyste funkcje (bez bazy), testowane na próbkach z Lidla i Biedronki.
Polski paragon fiskalny ma stałą budowę:

    nagłówek (sklep, adres, NIP)          → sklep
    PARAGON FISKALNY                       → od tu pozycje
    Mleko 3,2% 1L    C  2 x3,49   6,98C    → nazwa, PTU, ilość x cena, wartość
    Banany luz          0,754 x 5,99 4,52C → towar ważony (kg)
      Rabat                        -1,00   → doliczany do pozycji powyżej
    SPRZEDAŻ OPODATK. / SUMA PTU           → koniec pozycji
    SUMA PLN                     54,23     → suma do kontroli
    2026-10-17 12:34  nr wydr. 1234        → data, godzina, numer

OCR myli się w drobiazgach (przecinek/kropka, „x”/„*”), więc wzorce są
tolerancyjne, a o poprawności odczytu mówi kontrola sumy.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

TWO_PLACES = Decimal('0.01')
PRICE_TOLERANCE = Decimal('0.02')

_AMOUNT = r'\d{1,5}[.,]\s?\d{2}'
_QTY = r'(?:\d{1,4}|[|lI])(?:[.,]\d{1,3})?'
_QTY_UNIT = r'(?P<kg>\s*kg)?'
_MULT = r'[x×X*«+]'
_PTU = r'[A-G]'
# Litera PTU za kwotą; na papierze „C” bywa czytane jako „0”, „O”, „Ć”, „(”.
_PTU_AFTER = r'[A-GOĆ0(]'
# Litera PTU przed ilością; OCR czyta ją różnie („Cc”, „Ć”, „|e”), Biedronka pisze też „brak”.
_PTU_BEFORE = rf'(?:\s{{2,}}(?:brak|[^\s\d]{{1,3}})|\s+{_PTU})?'

ITEM_RE = re.compile(
    rf'^(?P<name>.*?\S){_PTU_BEFORE}\s+(?P<qty>{_QTY}){_QTY_UNIT}\s*{_MULT}\s*(?P<price>{_AMOUNT})'
    rf'\s+(?P<total>{_AMOUNT})\s*(?P<ptu>{_PTU_AFTER})?\s*$'
)
# Długa nazwa w osobnej linii, ilość i cena w następnej.
QTY_ONLY_RE = re.compile(
    rf'^(?:(?:brak|[^\s\d]{{1,3}})\s+)?(?P<qty>{_QTY}){_QTY_UNIT}\s*{_MULT}\s*(?P<price>{_AMOUNT})\s+(?P<total>{_AMOUNT})\s*(?P<ptu>{_PTU_AFTER})?\s*$'
)
# Tekst z telefonu (Live Text, Obiektyw Google) dzieli pozycję na dwie linie:
# „NAZWA 670GC 1*11.69” i pod spodem sama wartość „11.690” (C czytane jako 0).
NAME_QTY_RE = re.compile(
    rf'^(?P<name>.*?\S){_PTU_BEFORE}\s+(?P<qty>{_QTY}){_QTY_UNIT}\s*{_MULT}\s*(?P<price>{_AMOUNT})\s*$'
)
TOTAL_ONLY_RE = re.compile(rf'^(?P<total>{_AMOUNT})\s*(?P<ptu>{_PTU_AFTER})?\s*$')
# „SUMA :” bez kwoty - kwota („PLN 57.27”) w następnej linii.
BARE_TOTAL_RE = re.compile(r'^\s*(?:SUMA|RAZEM|DO ZAPLATY)\b(?!\s*PTU)[^\d]*$')
AMOUNT_LINE_RE = re.compile(rf'^\s*(?:PLN\s*)?(?P<amount>{_AMOUNT})\s*(?:PLN)?\s*$')
# Kwota rabatu w zapisie OCR: „-1,00”, „=12,12”, „"12, 12”, „=|,26”, „1,00-”.
_OCR_AMOUNT = r'[\d|lI]{1,5}[.,]\s?\d{2}'
_MINUS = r'[-=~"\'—–_]'
DISCOUNT_LABELS = r'(?:rabat|opust|upust|obni[zż]ka|promocja|promo|kupon|lidl\s*plus|zni[zż]ka|taniej|oszcz[eę]dzasz)'
DISCOUNT_RE = re.compile(
    rf'^(?P<label>{DISCOUNT_LABELS}\b.*?)\s*{_MINUS}\s?(?P<amount>{_OCR_AMOUNT})\s*-?\s*{_PTU}?\s*$|'
    rf'^(?P<label2>{DISCOUNT_LABELS}\b.*?)\s+(?P<amount2>{_OCR_AMOUNT})\s*-\s*{_PTU}?\s*$',
    re.IGNORECASE,
)
PAYMENT_RE = re.compile(rf'^\s*(?:KARTA|GOTOWKA|BLIK)\b.*?(?P<amount>{_AMOUNT})\s*(?:PLN)?\s*$')
TOTAL_RE = re.compile(rf'^\s*(?:suma|razem|do\s+zap[lł]aty)\b(?!\s*ptu)(?P<label>[^\d]*)(?P<amount>{_AMOUNT})', re.IGNORECASE)
NIP_RE = re.compile(r'NIP[\s:.]*(?:PL)?\s*(?P<nip>\d[\d\- ]{8,14}\d)', re.IGNORECASE)
DATE_RE = re.compile(r'\b(?:(?P<y>2[08]\d{2})[-./](?P<m>[018]\d)[-./](?P<d>[0-38]\d)|(?P<d2>[0-38]\d)[-./](?P<m2>[018]\d)[-./](?P<y2>2[08]\d{2}))\b')
TIME_RE = re.compile(r'\b(?P<h>[01]\d|2[0-3]):(?P<mi>[0-5]\d)\b')
RECEIPT_NO_RE = re.compile(
    r'(?:nr\s*wydr\w*|wydruku|paragon\w*\s+nr|nr\s*parag\w*|numer\s+transakcji|\bnr\s*:)\s*[:.#]?\s*(?P<no>\d{1,10})|#\s*(?P<no2>\d{2,10})',
    re.IGNORECASE,
)
SIZE_RE = re.compile(r'(?P<count>\d{1,2})\s*[xX*]\s*(?P<size>\d+(?:[.,]\d+)?)\s*(?P<unit>KG|G|ML|L)\b|'
                     r'(?P<size2>\d+(?:[.,]\d+)?)\s*(?P<unit2>KG|G|ML|L)\b', re.IGNORECASE)

ITEMS_START_MARKERS = ('PARAGON FISKALNY', 'PARAGONFISKALNY')
ITEMS_END_MARKERS = (
    'SPRZEDAZ OPODATK', 'SPRZEDAZOPODATK', 'SUMA PTU', 'SUMAPTU', 'PTU A', 'PTU B', 'PTU C',
    'SPRZEDAZ ZWOLN', 'SUMA ', 'SUMA:', 'RAZEM', 'DO ZAPLATY',
)
NOT_ITEM_PREFIXES = ('NIP', 'PARAGON', 'KASA', 'KASJER', 'NR ', 'DATA')

KNOWN_STORES = (
    ('BIEDRONKA', 'Biedronka'), ('JERONIMO', 'Biedronka'), ('LIDL', 'Lidl'),
    ('KAUFLAND', 'Kaufland'), ('AUCHAN', 'Auchan'), ('CARREFOUR', 'Carrefour'),
    ('ALDEK', 'Aldek'), ('ZABKA', 'Żabka'), ('NETTO', 'Netto'), ('DINO', 'Dino'), ('ALDI', 'Aldi'),
    ('ROSSMANN', 'Rossmann'), ('HEBE', 'Hebe'), ('LEWIATAN', 'Lewiatan'),
    ('STOKROTKA', 'Stokrotka'), ('POLOMARKET', 'POLOmarket'), ('INTERMARCHE', 'Intermarché'),
    ('PEPCO', 'Pepco'), ('ACTION', 'Action'), ('SPOLEM', 'Społem'),
)

_BASE_UNIT_FACTORS = {'G': (Decimal('0.001'), 'kg'), 'KG': (Decimal('1'), 'kg'),
                      'ML': (Decimal('0.001'), 'l'), 'L': (Decimal('1'), 'l')}


@dataclass
class ParsedLine:
    raw_text: str
    name: str
    quantity: Decimal
    unit_price: Decimal
    total: Decimal
    discount: Decimal = Decimal('0.00')
    is_weighted: bool = False
    discount_labels: list = field(default_factory=list)
    ptu: str = ''

    @property
    def discount_kind(self) -> str:
        if not self.discount:
            return ''
        kind = discount_kind(self.discount_labels)
        # Sam „Rabat” równy pełnej cenie sztuki przy kilku sztukach to „3 za 2”.
        if (
            kind == DISCOUNT_PROMO and not self.is_weighted and self.quantity >= 2 and self.unit_price > 0
            and (self.discount / self.unit_price) == (self.discount / self.unit_price).to_integral_value()
        ):
            return DISCOUNT_MULTIBUY
        return kind

    @property
    def discount_label(self) -> str:
        return '; '.join(dict.fromkeys(self.discount_labels))[:80]

    @property
    def paid_price(self) -> Decimal:
        return (self.total - self.discount).quantize(TWO_PLACES)

    @property
    def normalized(self) -> str:
        return normalize_text(self.name)


@dataclass
class ParsedReceipt:
    store_name: str = ''
    nip: str = ''
    purchased_at: datetime | None = None
    total: Decimal | None = None
    receipt_no: str = ''
    lines: list[ParsedLine] = field(default_factory=list)

    @property
    def lines_total(self) -> Decimal:
        return sum((line.paid_price for line in self.lines), Decimal('0.00'))

    @property
    def total_matches(self) -> bool:
        return self.total is not None and self.lines_total == self.total


def _fold(value: str) -> str:
    """Wielkie litery bez polskich znaków (Ł nie rozkłada się w NFKD)."""
    value = value.replace('ł', 'l').replace('Ł', 'L')
    value = unicodedata.normalize('NFKD', value)
    return ''.join(ch for ch in value if not unicodedata.combining(ch)).upper()


def normalize_text(value: str) -> str:
    """Klucz słownika domu: „Jog.nat. Piątnica 400g” → „JOG NAT PIATNICA 400G”."""
    folded = _fold(value)
    folded = re.sub(r'(\d)[.,](\d)', r'\1,\2', folded)
    folded = re.sub(r'[^A-Z0-9,%]+', ' ', folded)
    folded = re.sub(r'(?<!\d),|,(?!\d)', ' ', folded)
    return re.sub(r'\s+', ' ', folded).strip()[:160]


def name_words(value: str) -> list[str]:
    """Słowa nazwy bez gramatury i liczb - do porównywania z produktami."""
    folded = normalize_text(SIZE_RE.sub(' ', value))
    return [word for word in folded.split() if len(word) >= 2 and not any(ch.isdigit() for ch in word)]


def to_decimal(value: str) -> Decimal | None:
    try:
        return Decimal(re.sub(r'[|lI]', '1', value.replace(' ', '')).replace(',', '.'))
    except (InvalidOperation, AttributeError):
        return None


def package_size(name: str):
    """Gramatura z nazwy: („400G”) → (Decimal('0.4'), 'kg', 1); („6X1,5L”) → (1.5, 'l', 6)."""
    match = SIZE_RE.search(_fold(name))
    if not match:
        return None
    size = to_decimal(match.group('size') or match.group('size2'))
    unit = (match.group('unit') or match.group('unit2')).upper()
    count = int(match.group('count')) if match.group('count') else 1
    if not size:
        return None
    factor, base = _BASE_UNIT_FACTORS[unit]
    return (size * factor, base, count)


DISCOUNT_APP = 'app'
DISCOUNT_MULTIBUY = 'multibuy'
DISCOUNT_PROMO = 'promo'
_APP_RE = re.compile(r'KUPON|LIDL ?PLUS|APLIKAC|MOJA BIEDRONKA|KARTA MOJA|Z KART|APP')
_MULTIBUY_RE = re.compile(
    r'TANIEJ ZA|\d\s*ZA\s*\d|\d\s*\+\s*\d|DRUG[AIY]|TRZECI|GRATIS|W CENIE|PRZY ZAKUPIE|\d\s*SZT'
)


def discount_kind(labels) -> str:
    """Rodzaj rabatu z opisu na paragonie: kupon z aplikacji, „taniej za 2”
    (zależy od liczby sztuk) albo zwykła promocja."""
    text = _fold(' '.join(labels))
    if _APP_RE.search(text):
        return DISCOUNT_APP
    if _MULTIBUY_RE.search(text):
        return DISCOUNT_MULTIBUY
    return DISCOUNT_PROMO


def price_per_base_unit(line: ParsedLine | object, size=None, paid=False):
    """Cena za kg / l / szt. - zwraca (cena, jednostka).

    Domyślnie cena **regularna** (półkowa, przed rabatami) - tylko ona
    pozwala porównać, czy produkt podrożał. ``paid=True`` liczy cenę
    faktycznie zapłaconą, po kuponach i promocjach.
    """
    quantity = Decimal(getattr(line, 'quantity'))
    if quantity <= 0:
        return None, ''
    if paid:
        amount_paid = Decimal(getattr(line, 'paid_price'))
    else:
        amount_paid = (Decimal(getattr(line, 'unit_price')) * quantity)
    if getattr(line, 'is_weighted', False):
        return (amount_paid / quantity).quantize(TWO_PLACES), 'kg'
    size = size or package_size(getattr(line, 'name', ''))
    if size:
        amount, base, count = size
        total_amount = amount * count * quantity
        if total_amount > 0:
            return (amount_paid / total_amount).quantize(TWO_PLACES), base
    return (amount_paid / quantity).quantize(TWO_PLACES), 'szt'


def _zero_variants(text: str, limit: int = 4):
    """Warianty liczby z „8” zamienionymi na „0” - czcionki z przekreślonym
    zerem (e-paragon Lidla) Tesseract czyta jako ósemkę."""
    positions = [i for i, ch in enumerate(text) if ch in '08'][:limit]
    variants = []
    for mask in range(1 << len(positions)):
        chars = list(text)
        for bit, pos in enumerate(positions):
            if mask & (1 << bit):
                chars[pos] = '0' if chars[pos] == '8' else '8'
        variants.append(''.join(chars))
    return variants


def _consistent(qty: Decimal, price: Decimal, total: Decimal) -> bool:
    return abs(qty * price - total) <= PRICE_TOLERANCE


def _repair_numbers(qty_text: str, price_text: str, total: Decimal):
    """Ilość i cena zgodne z wartością pozycji, jeśli da się to osiągnąć,
    zamieniając odczytane „8” na „0” (08,614 kg → 0,614 kg)."""
    for qty_variant in _zero_variants(qty_text):
        for price_variant in _zero_variants(price_text):
            qty, price = to_decimal(qty_variant), to_decimal(price_variant)
            if qty and price and _consistent(qty, price, total):
                return qty, price, qty_variant
    return to_decimal(qty_text), to_decimal(price_text), qty_text


def _clean_name(value: str) -> str:
    value = re.sub(rf'\s+[(\[{{|]?{_PTU}$', '', value.strip())
    return re.sub(r'\s{2,}', ' ', value).strip(' .:-—|„"!')[:160]


def _is_weighted(qty: Decimal, has_kg: bool = False) -> bool:
    # Biedronka pisze sztuki jako „1.000”, więc o wadze decyduje ułamek albo „kg”.
    return has_kg or qty != qty.to_integral_value()


def _make_line(raw: str, name: str, qty_text: str, price_text: str, total_text: str, has_kg: bool = False, ptu: str = '') -> ParsedLine | None:
    total = to_decimal(total_text)
    if total is None:
        return None
    qty, price, qty_text = _repair_numbers(qty_text, price_text, total)
    if qty is None or price is None or qty <= 0:
        return None
    name = _clean_name(name)
    if len(name) < 2 or re.fullmatch(_PTU, name) or _fold(name).startswith(NOT_ITEM_PREFIXES):
        return None
    # OCR potrafi zgubić cyfrę w cenie albo w wartości; wiarygodniejsza jest
    # wartość pozycji (to ją sumuje kasa), cenę jednostkową dopasowujemy do niej.
    if abs(qty * price - total) > PRICE_TOLERANCE and qty:
        price = (total / qty).quantize(TWO_PLACES)
    return ParsedLine(
        raw_text=raw[:255], name=name, quantity=qty.quantize(Decimal('0.001')),
        unit_price=price.quantize(TWO_PLACES), total=total.quantize(TWO_PLACES),
        is_weighted=_is_weighted(qty, has_kg),
        # „0”, „O”, „(”, „Ć” za kwotą to odczytane „C” (najczęstsza stawka: żywność 5%).
        ptu={'0': 'C', 'O': 'C', '(': 'C', 'Ć': 'C'}.get(ptu, ptu) if ptu else '',
    )


def _store_name(lines: list[str]) -> str:
    folded = _fold(' '.join(lines[:12]))
    compact = folded.replace(' ', '')
    for key, label in KNOWN_STORES:
        if key in compact:
            return label
    for line in lines[:4]:
        text = _clean_name(line)
        if len(text) >= 3 and not any(ch.isdigit() for ch in text[:3]):
            return text.title()[:120]
    return ''


def _date_from_parts(y: str, m: str, d: str):
    """Pierwsza sensowna data z wariantów 8 → 0 (2826-18-89 → 2026-10-09)."""
    latest = date.today() + timedelta(days=1)
    for yv in _zero_variants(y):
        for mv in _zero_variants(m):
            for dv in _zero_variants(d):
                try:
                    candidate = date(int(yv), int(mv), int(dv))
                except ValueError:
                    continue
                if date(2015, 1, 1) <= candidate <= latest:
                    return candidate
    return None


def _purchased_at(text: str) -> datetime | None:
    for date_match in DATE_RE.finditer(text):
        if date_match.group('y'):
            parts = date_match.group('y'), date_match.group('m'), date_match.group('d')
        else:
            parts = date_match.group('y2'), date_match.group('m2'), date_match.group('d2')
        day = _date_from_parts(*parts)
        if day is None:
            continue
        tail = text[date_match.end():date_match.end() + 40]
        time_match = TIME_RE.search(tail) or TIME_RE.search(text)
        hour, minute = (int(time_match.group('h')), int(time_match.group('mi'))) if time_match else (0, 0)
        return datetime(day.year, day.month, day.day, hour, minute)
    return None


def parse_receipt(text: str) -> ParsedReceipt:
    raw_lines = [line.strip() for line in (text or '').splitlines()]
    lines = [line for line in raw_lines if line]
    result = ParsedReceipt(store_name=_store_name(lines))

    nip_match = NIP_RE.search(text or '')
    if nip_match:
        digits = re.sub(r'\D', '', nip_match.group('nip'))
        if len(digits) == 10:
            result.nip = digits
    result.purchased_at = _purchased_at(text or '')
    no_match = RECEIPT_NO_RE.search(text or '')
    if no_match:
        result.receipt_no = no_match.group('no') or no_match.group('no2') or ''

    start = 0
    for index, line in enumerate(lines):
        if any(marker in _fold(line) for marker in ITEMS_START_MARKERS):
            start = index + 1
            break

    pending_name = ''
    in_items = True
    has_pln_total = False
    last_was_item = False
    payment_total = None
    awaiting_total = None
    pending_item = None
    body = lines[start:]

    def set_total(amount, with_pln):
        nonlocal has_pln_total
        if amount is None:
            return
        if with_pln:
            if not has_pln_total:
                result.total, has_pln_total = amount, True
        elif not has_pln_total:
            # Bez „PLN” bierzemy największą „Sumę” - mniejsza to zwykle suma VAT.
            result.total = max(result.total or amount, amount)

    def flush_item(total_text=None, ptu=''):
        name, qty_text, price_text, has_kg = pending_item
        if total_text is None:
            qty, price = to_decimal(qty_text), to_decimal(price_text)
            if qty is None or price is None:
                return None
            total_text = str((qty * price).quantize(TWO_PLACES))
        parsed = _make_line(f'{name} {qty_text} x {price_text} {total_text}', name, qty_text, price_text,
                            total_text, has_kg=has_kg, ptu=ptu)
        if parsed:
            result.lines.append(parsed)
        return parsed

    for index, line in enumerate(body):
        folded = _fold(line)
        if awaiting_total is not None:
            amount_line = AMOUNT_LINE_RE.match(folded)
            if amount_line:
                set_total(to_decimal(amount_line.group('amount')), awaiting_total or 'PLN' in folded)
            awaiting_total = None
        bare_total = BARE_TOTAL_RE.match(folded)
        if bare_total:
            awaiting_total = 'PLN' in folded
        total_match = TOTAL_RE.match(folded)
        payment = PAYMENT_RE.match(folded)
        if payment and payment_total is None:
            payment_total = to_decimal(payment.group('amount'))
        if total_match and 'PTU' not in folded:
            set_total(to_decimal(total_match.group('amount')), 'PLN' in _fold(total_match.group('label')))
        if not in_items:
            continue
        if pending_item is not None:
            total_only = TOTAL_ONLY_RE.match(line)
            if total_only:
                flush_item(total_only.group('total'), total_only.group('ptu') or '')
                pending_item = None
                last_was_item = False
                pending_name = ''
                continue
            flush_item()
            pending_item = None
        if folded.startswith(ITEMS_END_MARKERS) and result.lines:
            in_items = False
            continue

        discount = DISCOUNT_RE.match(line)
        if discount and result.lines:
            amount_text = discount.group('amount') or discount.group('amount2')
            amount = to_decimal(re.sub(r'[|lI]', '1', amount_text))
            if amount:
                result.lines[-1].discount += amount
                label = (discount.group('label') or discount.group('label2') or '').strip(' .:')
                result.lines[-1].discount_labels.append(re.sub(r'\s{2,}', ' ', label)[:60])
            pending_name = ''
            last_was_item = False
            continue

        qty_only = QTY_ONLY_RE.match(line)
        if qty_only and pending_name:
            parsed = _make_line(
                f'{pending_name} {line}', pending_name,
                qty_only.group('qty'), qty_only.group('price'), qty_only.group('total'),
                has_kg=bool(qty_only.group('kg')), ptu=qty_only.group('ptu') or '',
            )
            if parsed:
                result.lines.append(parsed)
            pending_name = ''
            continue

        item = ITEM_RE.match(line)
        if item:
            parsed = _make_line(
                line, item.group('name'), item.group('qty'), item.group('price'), item.group('total'),
                has_kg=bool(item.group('kg')), ptu=item.group('ptu') or '',
            )
            if parsed:
                result.lines.append(parsed)
                last_was_item = True
            pending_name = ''
            continue

        name_qty = NAME_QTY_RE.match(line)
        if name_qty and sum(ch.isalpha() for ch in name_qty.group('name')) >= 2:
            pending_item = (name_qty.group('name'), name_qty.group('qty'), name_qty.group('price'),
                            bool(name_qty.group('kg')))
            pending_name = ''
            last_was_item = False
            continue

        # Linia z samą kwotą (cena po rabacie) albo śmieć z OCR - pomijamy.
        letters = sum(ch.isalpha() for ch in line)
        is_text = letters >= 3 and not re.search(_AMOUNT + r'\s*' + _PTU + r'?\s*$', line)
        next_line = body[index + 1] if index + 1 < len(body) else ''
        if is_text and last_was_item and not QTY_ONLY_RE.match(next_line):
            # Biedronka zawija długą nazwę: „CzekGorzk64  A 3.000 x 9,99 29,97” / „Wedel90g”.
            previous = result.lines[-1]
            previous.name = _clean_name(f'{previous.name} {line}')
            previous.raw_text = f'{previous.raw_text} {line}'[:255]
            last_was_item = False
            pending_name = ''
            continue
        last_was_item = False
        pending_name = line if is_text else ''

    if pending_item is not None:
        flush_item()
    if result.total is None and payment_total is not None:
        # „SUMA PLN” nieczytelna - kwota płatności kartą jest tą samą sumą.
        result.total = payment_total
    _strip_glued_ptu(result.lines)
    return result


def _strip_glued_ptu(lines: list[ParsedLine]):
    """Aldek i podobne kasy doklejają literę PTU do nazwy („SOLONYC”, „670GC”).
    Odcinamy ją tylko wtedy, gdy robi tak cały paragon - pojedyncza nazwa
    kończąca się na „A” czy „C” może być prawdziwa."""
    tagged = [line for line in lines if line.ptu]
    if len(tagged) < 2:
        return
    glued = [line for line in tagged if _fold(line.name).endswith(line.ptu)]
    if len(glued) < 0.8 * len(tagged):
        return
    for line in glued:
        line.name = line.name[:-1].rstrip()


def fingerprint(parsed: ParsedReceipt, store_key: str = '') -> str:
    """Odcisk paragonu; pusty, gdy brakuje danych, żeby nie łączyć różnych paragonów."""
    if parsed.purchased_at is None or parsed.total is None:
        return ''
    key = store_key or parsed.nip or normalize_text(parsed.store_name)
    if not key:
        return ''
    return '|'.join([
        key, parsed.purchased_at.strftime('%Y%m%d%H%M'), str(parsed.total), parsed.receipt_no or '-',
    ])[:64]
