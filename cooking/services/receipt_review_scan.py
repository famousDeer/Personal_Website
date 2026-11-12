"""Skan kodu kreskowego na etapie „Sprawdź” - przed „Dodaj do spiżarni”.

Ta sama nazwa na paragonie potrafi oznaczać różne produkty (inna firma,
inny wariant), więc propozycja z nazwy bywa błędna. Skan opakowania wskazuje
produkt jednoznacznie:

- kod znany w spiżarni → pozycja dostaje ten produkt,
- kod nieznany → pozycja pamięta kod i nazwę z katalogu (pamięć domu albo
  Open Food Facts); produkt z tym kodem powstaje dopiero przy zatwierdzeniu,
  więc porzucony paragon nie zostawia pustych produktów, a nazwę można poprawić.

Skanuje się w dowolnej kolejności: pozycję dobiera serwer (produkt z propozycji
albo podobieństwo nazw), a pyta tylko wtedy, gdy nie ma wyraźnego zwycięzcy.
Skan zapisuje się na pozycji (``ReceiptLine.barcode``) i przy zatwierdzeniu
wygrywa ze słownikiem domu - patrz ``receipts._remember_alias``.
"""
from __future__ import annotations

from django.db import transaction

from ..models import PantryProduct, Receipt, ReceiptLine
from .pantry_names import product_label, same_product
from .receipt_matching import name_score

AUTO_LINE_SCORE = 0.55
AUTO_LINE_MARGIN = 0.15


def _catalog_product(barcode: str) -> tuple[str, str]:
    """(nazwa, firma) z pamięci domu albo Open Food Facts; ('', '') gdy brak."""
    from .product_catalog import CatalogUnavailable, lookup_product_catalog

    try:
        result = lookup_product_catalog(barcode)
    except CatalogUnavailable:
        return '', ''
    entry = result.entry
    if not entry or result.status not in ('found', 'found_incomplete'):
        return '', ''
    return (entry.product_name or '').strip(), (entry.brand or '').strip()


def _line_info(line: ReceiptLine) -> dict:
    return {'id': line.pk, 'name': line.name, 'price': str(line.paid_price)}


def _score(line: ReceiptLine, owner, names: list[str]) -> float:
    if owner is not None:
        if line.product_id == owner.pk:
            return 1.0
        if line.group_id and line.group_id == owner.group_id:
            return 0.9
    return max((name_score(line.name, name) for name in names if name), default=0.0)


def _pick_line(lines, barcode, owner, names):
    """Zwraca (pozycja, status, kandydaci). Pozycja jest wybrana tylko wtedy,
    gdy wybór jest pewny; inaczej status mówi, o co zapytać."""
    free = [
        line for line in lines
        if not line.is_weighted and not line.barcode and line.match_status != ReceiptLine.MATCH_IGNORED
    ]
    same_code = [line for line in lines if line.barcode == barcode]
    if same_code:
        # Drugie opakowanie tego samego towaru: kolejna identyczna linia albo nic.
        twin = next((line for line in free if line.normalized_text == same_code[0].normalized_text), None)
        return (twin, 'ok', []) if twin else (same_code[0], 'already', [])
    if not free:
        return None, 'none', []
    scored = sorted(
        ((_score(line, owner, names), index, line) for index, line in enumerate(free)),
        key=lambda row: (-row[0], row[1]),
    )
    best_score, _index, best = scored[0]
    rivals = [row for row in scored[1:] if row[2].normalized_text != best.normalized_text]
    second = rivals[0][0] if rivals else 0.0
    if best_score >= AUTO_LINE_SCORE and best_score - second >= AUTO_LINE_MARGIN:
        return best, 'ok', []
    return None, 'choose', [line for _score_value, _index, line in scored]


@transaction.atomic
def scan_review_line(receipt: Receipt, barcode: str, line_id: int | None = None) -> dict:
    """Jeden skan na etapie „Sprawdź”. Status odpowiedzi:
    known (kod jest w spiżarni - pozycja ma ten produkt), new (kod nieznany -
    pozycja ma kod i nazwę do poprawy), choose (wskaż pozycję), already (ten
    kod ma już inna pozycja), none (nie ma pozycji bez kodu)."""
    receipt = Receipt.objects.select_for_update().get(pk=receipt.pk)
    if receipt.status != Receipt.STATUS_REVIEW:
        raise ValueError('Ten paragon nie jest już na etapie sprawdzania.')
    lines = list(receipt.lines.select_related('product', 'group'))
    owner = PantryProduct.objects.filter(barcode=barcode).first()
    name, brand = ('', '') if owner else _catalog_product(barcode)
    label = owner.label if owner else product_label(name, brand)

    if line_id:
        line = next((item for item in lines if item.pk == line_id), None)
        if line is None:
            raise ValueError('Nie ma takiej pozycji na tym paragonie.')
    else:
        names = [owner.name, owner.label] if owner else [name, f'{brand} {name}'.strip()]
        line, status, candidates = _pick_line(lines, barcode, owner, names)
        if status != 'ok':
            return {
                'status': status, 'product': label, 'known': owner is not None,
                'line': _line_info(line) if line else None,
                'candidates': [_line_info(item) for item in candidates],
            }

    line.barcode = barcode
    if owner is not None:
        line.product, line.group, line.target_name = owner, None, ''
        status, target = 'known', owner.label
    else:
        # Propozycja bez kodu zostaje, gdy katalog nie mówi nic innego: to pewnie
        # ten sam produkt, któremu skan dopisze kod. Inna nazwa albo firma, albo
        # propozycja z własnym kodem → nowy produkt o nazwie z katalogu.
        current = None if line.group_id else line.product
        keep = current is not None and not current.barcode and (
            not name or same_product(current, PantryProduct(name=name, brand=brand))
        )
        if keep:
            line.target_name = ''
            target = current.label
        else:
            line.product, line.group, line.target_name = None, None, label[:200]
            target = line.target_name
        status = 'new'
    if line.match_status in (ReceiptLine.MATCH_IGNORED, ReceiptLine.MATCH_SKIPPED):
        line.match_status = ReceiptLine.MATCH_UNKNOWN
    line.save(update_fields=['barcode', 'product', 'group', 'target_name', 'match_status'])
    return {
        'status': status, 'line': _line_info(line), 'target': target, 'product': label,
        'locked': owner is not None,
    }


@transaction.atomic
def clear_review_scan(receipt: Receipt, line_id: int) -> dict:
    """„✕” przy kodzie: pozycja wraca do ręcznego wyboru produktu."""
    receipt = Receipt.objects.select_for_update().get(pk=receipt.pk)
    if receipt.status != Receipt.STATUS_REVIEW:
        raise ValueError('Ten paragon nie jest już na etapie sprawdzania.')
    line = receipt.lines.filter(pk=line_id).first()
    if line is None:
        raise ValueError('Nie ma takiej pozycji na tym paragonie.')
    line.barcode = ''
    line.save(update_fields=['barcode'])
    return {'status': 'cleared', 'line': _line_info(line)}
