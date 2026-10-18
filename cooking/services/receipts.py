"""Paragony: odczyt w tle, propozycje i zatwierdzenie do spiżarni.

Bez Celery: po przesłaniu zdjęcia odczyt rusza w osobnym wątku, a strona
paragonu odświeża się, dopóki trwa. Paragon, którego odczyt utknął
(restart serwera), dokończy komenda ``process_receipts`` z crona.

Paragon nie dotyka Finansów - ceny są tylko informacją o zmianach cen.
Zdjęcie po zatwierdzeniu trafia do ``RECEIPT_ARCHIVE_DIR``, skąd skrypt
deploy/receipts_upload.sh przenosi je na Dysk Google (rclone move).
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from ..constants import PANTRY_CATEGORY_OTHER
from ..models import (
    PantryMovement, PantryProduct, ProductGroup, Receipt, ReceiptAlias, ReceiptLine,
    ReceiptStore, ShoppingList, ShoppingListItem,
)
from .pantry_quantities import (
    convert_pantry_quantity, counts_in_packages, estimated_package_count,
    sync_package_count_from_quantity, tracks_packages, validate_pantry_quantity_for_unit,
    validate_pantry_storage_quantity,
)
from .product_groups import restock_target
from .receipt_matching import MatchContext, find_alias, suggest
from .receipt_ocr import OCR_VARIANTS, ReceiptOcrError, read_text
from .receipt_parser import fingerprint, normalize_text, package_size, parse_receipt, price_per_base_unit

logger = logging.getLogger(__name__)

STUCK_AFTER = timedelta(minutes=10)
ACTION_ADD = 'add'
ACTION_SKIP = 'skip'
ACTION_IGNORE = 'ignore'


# --- odczyt ----------------------------------------------------------------

def start_processing(receipt: Receipt):
    receipt.status = Receipt.STATUS_PROCESSING
    receipt.processing_started_at = timezone.now()
    receipt.error = ''
    receipt.save(update_fields=['status', 'processing_started_at', 'error', 'updated_at'])
    if getattr(settings, 'RECEIPT_OCR_ASYNC', True):
        threading.Thread(target=_process_in_thread, args=(receipt.pk,), daemon=True).start()
    else:
        process_receipt(receipt.pk)


def _process_in_thread(receipt_id):
    close_old_connections()
    try:
        process_receipt(receipt_id)
    except Exception:  # noqa: BLE001 - wątek nie może zginąć po cichu
        logger.exception('Odczyt paragonu %s nie powiódł się', receipt_id)
        Receipt.objects.filter(pk=receipt_id).update(
            status=Receipt.STATUS_FAILED, error='Nieoczekiwany błąd odczytu.',
        )
    finally:
        close_old_connections()


def store_for(parsed) -> ReceiptStore | None:
    if parsed.nip:
        store = ReceiptStore.objects.filter(nip=parsed.nip).first()
        if store:
            return store
    if parsed.store_name:
        store = ReceiptStore.objects.filter(name__iexact=parsed.store_name).first()
        if store:
            if parsed.nip and not store.nip:
                store.nip = parsed.nip
                store.save(update_fields=['nip'])
            return store
        return ReceiptStore.objects.create(name=parsed.store_name, nip=parsed.nip)
    return None


def process_receipt(receipt_id):
    receipt = Receipt.objects.prefetch_related('photos').get(pk=receipt_id)
    paths = [photo.image.path for photo in receipt.photos.all() if photo.image]
    try:
        text = best_ocr_text(paths)
    except ReceiptOcrError as exc:
        receipt.status = Receipt.STATUS_FAILED
        receipt.error = str(exc)[:255]
        receipt.save(update_fields=['status', 'error', 'updated_at'])
        return receipt
    apply_ocr_text(receipt, text)
    return receipt


def _ocr_score(parsed):
    difference = abs(parsed.lines_total - parsed.total) if parsed.total is not None else Decimal('999999')
    return (parsed.total_matches, len(parsed.lines), -difference)


def best_ocr_text(paths) -> str:
    """Czyta zdjęcia kolejnymi ustawieniami, aż suma pozycji zgodzi się
    z sumą paragonu; inaczej zwraca najlepszą z prób."""
    best_text, best_score = '', None
    for variant in range(len(OCR_VARIANTS)):
        text = '\n'.join(read_text(path, variant) for path in paths)
        parsed = parse_receipt(text)
        score = _ocr_score(parsed)
        if best_score is None or score > best_score:
            best_text, best_score = text, score
        if parsed.total_matches:
            break
    return best_text


@transaction.atomic
def apply_ocr_text(receipt: Receipt, text: str):
    """Tekst z OCR → sklep, pozycje i propozycje produktów."""
    parsed = parse_receipt(text)
    store = store_for(parsed)
    receipt.ocr_text = text
    receipt.store = store
    if parsed.purchased_at:
        receipt.purchased_at = timezone.make_aware(parsed.purchased_at)
    receipt.total = parsed.total
    receipt.receipt_no = parsed.receipt_no
    receipt.lines.all().delete()

    receipt.fingerprint = ''
    receipt.duplicate_of = None
    key = fingerprint(parsed, store_key=str(store.pk) if store else '')
    if key:
        other = Receipt.objects.filter(fingerprint=key).exclude(pk=receipt.pk).first()
        if other:
            receipt.duplicate_of = other
        else:
            receipt.fingerprint = key

    context = MatchContext(receipt.purchased_on)
    for position, line in enumerate(parsed.lines, start=1):
        normalized = line.normalized
        record = ReceiptLine(
            receipt=receipt, position=position, raw_text=line.raw_text, name=line.name,
            normalized_text=normalized, quantity=line.quantity, is_weighted=line.is_weighted,
            unit_price=line.unit_price, discount=line.discount, paid_price=line.paid_price,
            discount_kind=line.discount_kind, discount_label=line.discount_label,
        )
        _set_unit_prices(record)
        alias = find_alias(store, normalized)
        if alias and alias.ignore:
            record.match_status = ReceiptLine.MATCH_IGNORED
        elif alias and (alias.product_id or alias.group_id):
            record.product, record.group = alias.product, alias.group
            record.match_status = ReceiptLine.MATCH_AUTO
        else:
            candidate = suggest(line.name, context)
            if candidate:
                record.product, record.group = candidate.product, candidate.group
            record.match_status = ReceiptLine.MATCH_UNKNOWN
        record.save()

    if not parsed.lines:
        receipt.status = Receipt.STATUS_FAILED
        receipt.error = 'Nie znalazłem pozycji na zdjęciu. Zrób zdjęcie z bliska, przy dobrym świetle, bez zagięć.'
    else:
        receipt.status = Receipt.STATUS_REVIEW
        receipt.error = ''
    receipt.save()
    return receipt


def _set_unit_prices(line, size=None):
    line.price_per_base_unit, line.base_unit = price_per_base_unit(line, size)
    line.paid_per_base_unit, _base = price_per_base_unit(line, size, paid=True)


def resume_stuck_receipts():
    """Dla crona: dokończ odczyty przerwane restartem serwera."""
    limit = timezone.now() - STUCK_AFTER
    stuck = Receipt.objects.filter(status=Receipt.STATUS_PROCESSING).filter(
        processing_started_at__lt=limit,
    ) | Receipt.objects.filter(status=Receipt.STATUS_PROCESSING, processing_started_at__isnull=True)
    done = 0
    for receipt in stuck:
        receipt.processing_started_at = timezone.now()
        receipt.save(update_fields=['processing_started_at'])
        process_receipt(receipt.pk)
        done += 1
    return done


# --- zatwierdzenie -----------------------------------------------------------

@dataclass
class LineDecision:
    action: str
    target: str = ''
    quantity: Decimal | None = None


def default_quantity(line: ReceiptLine, alias_multiplier: int = 1) -> Decimal:
    """Ilość proponowana w formularzu: kg dla ważonych, opakowania dla reszty."""
    if line.is_weighted:
        return line.quantity
    return (line.quantity * alias_multiplier).quantize(Decimal('1'))


def resolve_target(name: str, user, line: ReceiptLine):
    """Nazwa wpisana przy pozycji → (produkt, grupa). Nową nazwę zakłada jako produkt."""
    name = re.sub(r'\s+', ' ', name or '').strip()[:160]
    if not name:
        return None, None
    product = PantryProduct.objects.filter(name__iexact=name).first()
    if product:
        return product, None
    group = ProductGroup.objects.filter(name__iexact=name).first()
    if group:
        members = group.members()
        return restock_target(group) or (members[0] if members else None), group
    from .product_catalog import suggest_category_from_name

    size = package_size(line.name)
    unit = PantryProduct.UNIT_KILOGRAM if line.is_weighted else PantryProduct.UNIT_PIECE
    product = PantryProduct.objects.create(
        created_by=user, name=name,
        category=suggest_category_from_name(name) or PANTRY_CATEGORY_OTHER,
        unit=unit,
        notes=f'Dodany z paragonu: {line.name}'[:255] if size else '',
    )
    return product, None


def pantry_quantity(product: PantryProduct, line: ReceiptLine, quantity: Decimal) -> Decimal:
    """Ilość z formularza (kg albo opakowania) → ilość w jednostce produktu."""
    if line.is_weighted:
        if product.unit in (PantryProduct.UNIT_PIECE, PantryProduct.UNIT_PACKAGE):
            return Decimal('1')
        return convert_pantry_quantity(quantity, PantryProduct.UNIT_KILOGRAM, product.unit)
    if product.unit in (PantryProduct.UNIT_PIECE, PantryProduct.UNIT_PACKAGE):
        return quantity.to_integral_value(rounding=ROUND_CEILING)
    if counts_in_packages(product):
        return (quantity * product.quantity_per_scan).quantize(Decimal('0.01'))
    size = package_size(line.name)
    if size:
        amount, base, count = size
        source_unit = PantryProduct.UNIT_KILOGRAM if base == 'kg' else PantryProduct.UNIT_LITER
        return convert_pantry_quantity(amount * count * quantity, source_unit, product.unit)
    return (quantity * product.quantity_per_scan).quantize(Decimal('0.01'))


def _same_day_movement(product, day):
    linked = ReceiptLine.objects.exclude(movement=None).values('movement_id')
    return PantryMovement.objects.filter(
        product=product, movement_type=PantryMovement.PURCHASE, occurred_on=day,
    ).exclude(pk__in=linked).order_by('created_at').first()


def _add_to_pantry(product, quantity, receipt):
    product = PantryProduct.objects.select_for_update().get(pk=product.pk)
    validate_pantry_quantity_for_unit(quantity, product.unit)
    validate_pantry_storage_quantity(product.current_quantity + quantity)
    if product.current_package_count == 0 and product.current_quantity > 0:
        sync_package_count_from_quantity(product)
    package_count = estimated_package_count(quantity, product.quantity_per_scan) if tracks_packages(product) else None
    product.current_quantity += quantity
    sync_package_count_from_quantity(product)
    product.save(update_fields=['current_quantity', 'current_package_count', 'updated_at'])
    store = receipt.store.name if receipt.store_id else 'sklep'
    return PantryMovement.objects.create(
        product=product, movement_type=PantryMovement.PURCHASE, quantity=quantity,
        package_count=package_count, occurred_on=receipt.purchased_on,
        note=f'Paragon: {store} {receipt.purchased_on:%d.%m.%Y}'[:255],
    )


def _tick_shopping_list(product, group, movement, user):
    """Pozycja z aktywnej listy, którą właśnie kupiono, odhacza się sama -
    bez drugiego dopisania do spiżarni (ruch jest już z paragonu)."""
    filters = {'pantry_product': product}
    items = ShoppingListItem.objects.filter(
        shopping_list__status=ShoppingList.ACTIVE, is_purchased=False,
    )
    group_id = group.pk if group else product.group_id
    query = items.filter(**filters)
    if group_id:
        query = query | items.filter(pantry_group_id=group_id)
    item = query.order_by('created_at').first()
    if item is None:
        return None
    now = timezone.now()
    item.is_purchased = True
    item.purchased_at = now
    item.purchased_by = user
    item.purchased_changed_at = now
    item.pantry_product = product
    if not ShoppingListItem.objects.filter(pantry_movement=movement).exists():
        item.pantry_movement = movement
    item.save()
    return item


def _remember_alias(receipt, line, *, product=None, group=None, ignore=False, multiplier=1):
    alias = ReceiptAlias.objects.filter(store=receipt.store, normalized_text=line.normalized_text).first()
    if alias is None:
        alias = ReceiptAlias(store=receipt.store, normalized_text=line.normalized_text)
    alias.product = None if ignore else (None if group else product)
    alias.group = None if ignore else group
    alias.ignore = ignore
    alias.multiplier = max(1, min(int(multiplier or 1), 100))
    alias.hits += 1
    alias.last_used_at = timezone.now()
    alias.save()


@transaction.atomic
def confirm_receipt(receipt: Receipt, decisions: dict[int, LineDecision], user=None):
    """Zatwierdza paragon. Zwraca (dodane, połączone_z_istniejącymi, ostrzeżenia)."""
    receipt = Receipt.objects.select_for_update().get(pk=receipt.pk)
    if receipt.status != Receipt.STATUS_REVIEW:
        raise ValueError('Ten paragon jest już zatwierdzony albo jeszcze się odczytuje.')
    added = linked = 0
    warnings = []
    ticked = 0
    for line in receipt.lines.select_related('product', 'group'):
        decision = decisions.get(line.pk) or LineDecision(action=ACTION_SKIP)
        if decision.action == ACTION_IGNORE:
            line.match_status = ReceiptLine.MATCH_IGNORED
            line.product = line.group = None
            line.save(update_fields=['match_status', 'product', 'group'])
            _remember_alias(receipt, line, ignore=True)
            continue
        if decision.action != ACTION_ADD:
            line.match_status = ReceiptLine.MATCH_SKIPPED
            line.save(update_fields=['match_status'])
            continue

        product, group = resolve_target(decision.target, user, line)
        if product is None:
            warnings.append(f'{line.name}: nie wybrano produktu.')
            line.match_status = ReceiptLine.MATCH_SKIPPED
            line.save(update_fields=['match_status'])
            continue
        quantity = decision.quantity if decision.quantity and decision.quantity > 0 else default_quantity(line)
        was_auto = (
            line.match_status == ReceiptLine.MATCH_AUTO
            and line.product_id == product.pk and line.group_id == (group.pk if group else None)
        )
        line.product, line.group = product, group
        line.match_status = ReceiptLine.MATCH_AUTO if was_auto else ReceiptLine.MATCH_CONFIRMED

        if not line.is_weighted and product.unit in (PantryProduct.UNIT_GRAM, PantryProduct.UNIT_MILLILITER,
                                                      PantryProduct.UNIT_KILOGRAM, PantryProduct.UNIT_LITER):
            if counts_in_packages(product):
                factor = Decimal('0.001') if product.unit in (PantryProduct.UNIT_GRAM, PantryProduct.UNIT_MILLILITER) else Decimal('1')
                base = 'kg' if product.unit in (PantryProduct.UNIT_GRAM, PantryProduct.UNIT_KILOGRAM) else 'l'
                _set_unit_prices(line, (product.quantity_per_scan * factor, base, 1))

        movement = _same_day_movement(product, receipt.purchased_on)
        if movement is not None:
            linked += 1
        else:
            try:
                movement = _add_to_pantry(product, pantry_quantity(product, line, quantity), receipt)
                added += 1
            except ValueError as exc:
                warnings.append(f'{line.name}: nie dodano do spiżarni - {exc}')
                movement = None
        line.movement = movement
        line.save()
        if movement is not None and _tick_shopping_list(product, group, movement, user):
            ticked += 1
        multiplier = 1
        if not line.is_weighted and line.quantity and quantity != line.quantity:
            ratio = quantity / line.quantity
            if ratio == ratio.to_integral_value():
                multiplier = int(ratio)
        _remember_alias(receipt, line, product=product, group=group, multiplier=multiplier)

    receipt.status = Receipt.STATUS_DONE
    receipt.confirmed_at = timezone.now()
    receipt.save(update_fields=['status', 'confirmed_at', 'updated_at'])
    transaction.on_commit(lambda: archive_photos(receipt.pk))
    return added, linked, ticked, warnings


# --- zdjęcia → Dysk Google ----------------------------------------------------

def archive_dir() -> Path:
    path = Path(settings.RECEIPT_ARCHIVE_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', normalize_text(value).lower()).strip('-') or 'sklep'


def archive_photos(receipt_id):
    """Przenosi zdjęcia do folderu wysyłki na Dysk Google; na Pi nie zostają."""
    receipt = Receipt.objects.select_related('store').get(pk=receipt_id)
    target_dir = archive_dir()
    store = _slug(receipt.store.name if receipt.store_id else 'sklep')
    for photo in receipt.photos.all():
        if not photo.image:
            continue
        name = f'{receipt.purchased_on:%Y-%m-%d}_{store}_{str(receipt.uuid)[:8]}_{photo.position}.jpg'
        source = Path(photo.image.path)
        try:
            source.replace(target_dir / name)
        except OSError:
            logger.exception('Nie udało się przenieść zdjęcia paragonu %s', receipt.pk)
            continue
        photo.image = ''
        photo.archived_name = name
        photo.save(update_fields=['image', 'archived_name'])


def cleanup_orphan_photos(older_than=timedelta(days=1)):
    """Pliki po odrzuconych paragonach (po czasie na „Cofnij”)."""
    from ..models import ReceiptPhoto
    from ..storage import private_media_storage

    folder = Path(private_media_storage.location) / 'receipts'
    if not folder.exists():
        return 0
    used = {Path(name).name for name in ReceiptPhoto.objects.exclude(image='').values_list('image', flat=True)}
    limit = (timezone.now() - older_than).timestamp()
    removed = 0
    for path in folder.iterdir():
        if path.is_file() and path.name not in used and path.stat().st_mtime < limit:
            path.unlink()
            removed += 1
    return removed


# --- kody kreskowe produktów z paragonu ---------------------------------------
# Paragon nie ma kodów kreskowych. Po zatwierdzeniu tryb „Zeskanuj kody”
# skanuje opakowania przy wkładaniu do szafki; kod sam trafia do właściwego
# produktu, a pytanie pojawia się tylko, gdy nie da się zgadnąć.

AUTO_LINK_SCORE = 0.55
AUTO_LINK_MARGIN = 0.15


def assign_barcode(product: PantryProduct, barcode: str) -> PantryProduct:
    from .product_catalog import remember_household_product

    with transaction.atomic():
        product = PantryProduct.objects.select_for_update().get(pk=product.pk)
        if PantryProduct.objects.filter(barcode=barcode).exclude(pk=product.pk).exists():
            raise ValueError('Ten kod ma już inny produkt w spiżarni.')
        if product.barcode and product.barcode != barcode:
            raise ValueError('Ten produkt ma już swój kod kreskowy.')
        if product.barcode != barcode:
            product.barcode = barcode
            product.save(update_fields=['barcode', 'updated_at'])
        remember_household_product(
            barcode, name=product.name, category=product.category,
            unit=product.unit, quantity_per_scan=product.quantity_per_scan,
        )
    return product


def receipt_products_without_barcode(receipt: Receipt) -> list[PantryProduct]:
    """Produkty dodane z tego paragonu, które nie mają jeszcze kodu.
    Towar ważony (owoce, warzywa luzem) kodu nie ma, więc go pomijamy."""
    seen, products = set(), []
    lines = (
        receipt.lines.filter(match_status__in=[ReceiptLine.MATCH_AUTO, ReceiptLine.MATCH_CONFIRMED])
        .exclude(product=None).filter(is_weighted=False, product__barcode='')
        .select_related('product').order_by('position')
    )
    for line in lines:
        if line.product_id not in seen:
            seen.add(line.product_id)
            products.append(line.product)
    return products


def _catalog_name(barcode: str) -> str:
    from .product_catalog import CatalogUnavailable, lookup_product_catalog

    try:
        result = lookup_product_catalog(barcode)
    except CatalogUnavailable:
        return ''
    entry = result.entry
    if not entry or result.status not in ('found', 'found_incomplete'):
        return ''
    return ' '.join(part for part in [entry.brand, entry.product_name] if part).strip()


def scan_receipt_barcode(receipt: Receipt, barcode: str, product_id: int | None = None) -> dict:
    """Jeden skan w trybie „Zeskanuj kody”. Zwraca status:
    linked (przypisany), known (kod już jest w spiżarni), choose (trzeba wskazać produkt)."""
    from .receipt_matching import name_score

    remaining = receipt_products_without_barcode(receipt)
    owner = PantryProduct.objects.filter(barcode=barcode).first()
    if owner is not None:
        return {'status': 'known', 'product': owner.name}

    if product_id:
        product = next((item for item in remaining if item.pk == product_id), None)
        product = product or PantryProduct.objects.filter(pk=product_id).first()
        if product is None:
            raise ValueError('Nie ma takiego produktu.')
        assign_barcode(product, barcode)
        return {'status': 'linked', 'product': product.name, 'product_id': product.pk}

    if not remaining:
        return {'status': 'choose', 'catalog_name': '', 'candidates': []}
    catalog_name = _catalog_name(barcode)
    scored = sorted(
        ((name_score(catalog_name, item.name) if catalog_name else 0.0, index, item)
         for index, item in enumerate(remaining)),
        key=lambda row: (-row[0], row[1]),
    )
    best_score, _index, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if len(remaining) == 1 or (best_score >= AUTO_LINK_SCORE and best_score - second >= AUTO_LINK_MARGIN):
        assign_barcode(best, barcode)
        return {'status': 'linked', 'product': best.name, 'product_id': best.pk, 'catalog_name': catalog_name}
    return {
        'status': 'choose',
        'catalog_name': catalog_name,
        'candidates': [{'id': item.pk, 'name': item.name} for _score, _index, item in scored],
    }
