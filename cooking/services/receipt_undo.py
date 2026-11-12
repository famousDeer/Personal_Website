"""„Cofnij dodanie”: zdejmuje ze spiżarni to, co dodał paragon.

Zatwierdzenie zapisuje dziennik (``Receipt.confirm_log``): utworzone ruchy
zakupu i produkty, odhaczone pozycje list, poprzedni stan aliasów i nadane
kody kreskowe. Cofnięcie odtwarza stan według dziennika i wraca z paragonem
do etapu „Sprawdź” - wybory przy pozycjach zostają, więc wystarczy poprawić
pomyłkę i zatwierdzić ponownie.

Ruch zakupu dowiązany do paragonu (towar zeskanowany tego samego dnia) nie
jest w dzienniku: paragon go nie utworzył, więc zostaje w spiżarni.
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from ..models import (
    PantryMovement, PantryProduct, Receipt, ReceiptAlias, ShoppingListItem,
)
from .pantry_batches import reconcile
from .pantry_quantities import sync_package_count_from_quantity

logger = logging.getLogger(__name__)


def can_undo(receipt: Receipt) -> bool:
    return receipt.status == Receipt.STATUS_DONE and bool(receipt.confirm_log)


def _untick(log):
    for tick in log.get('ticks', []):
        item = ShoppingListItem.objects.filter(pk=tick['id'], is_purchased=True).first()
        if item is None:
            continue
        item.is_purchased = False
        item.purchased_at = None
        item.purchased_by = None
        item.purchased_changed_at = timezone.now()
        if item.pantry_movement_id in log.get('movements', []):
            item.pantry_movement = None
        previous = tick.get('product_id')
        item.pantry_product_id = previous if PantryProduct.objects.filter(pk=previous).exists() else None
        item.save()


def _remove_batches(product, quantity, since):
    """Partie z terminem wpisanym po zatwierdzeniu („Rozpakuj zakupy”) -
    od najnowszej, do ilości z cofanego ruchu."""
    for batch in product.batches.filter(created_at__gte=since).order_by('-created_at'):
        if quantity <= 0:
            break
        take = min(batch.quantity, quantity)
        quantity -= take
        if take >= batch.quantity:
            batch.delete()
        else:
            batch.quantity -= take
            batch.save(update_fields=['quantity'])


def _remove_movements(receipt, log):
    removed = 0
    for movement in PantryMovement.objects.filter(pk__in=log.get('movements', [])):
        product = PantryProduct.objects.select_for_update().get(pk=movement.product_id)
        # Część mogła już zostać zużyta - stan nie schodzi poniżej zera.
        product.current_quantity = max(product.current_quantity - movement.quantity, Decimal('0'))
        sync_package_count_from_quantity(product)
        product.save(update_fields=['current_quantity', 'current_package_count', 'updated_at'])
        if receipt.confirmed_at:
            _remove_batches(product, movement.quantity, receipt.confirmed_at)
        reconcile(product)
        movement.delete()
        removed += 1
    return removed


def _restore_aliases(log):
    # Od końca: ten sam skrót dwa razy na paragonie wraca do stanu sprzed pierwszej pozycji.
    for entry in reversed(log.get('aliases', [])):
        alias = ReceiptAlias.objects.filter(pk=entry['id']).first()
        if alias is None:
            continue
        previous = entry.get('previous')
        if previous is None:
            alias.delete()
            continue
        alias.product_id = previous['product_id']
        alias.group_id = previous['group_id']
        alias.ignore = previous['ignore']
        alias.multiplier = previous['multiplier']
        alias.ambiguous = previous['ambiguous']
        alias.hits = previous['hits']
        alias.last_used_at = datetime.fromisoformat(previous['last_used_at'])
        alias.save()


def _remove_created_products(receipt, log):
    """Produkt założony przez ten paragon znika, jeśli nie ma innej historii:
    zapasu, ruchów, partii, ocen ani pozycji innych paragonów czy list."""
    removed = 0
    for product in PantryProduct.objects.filter(pk__in=log.get('products', [])):
        has_history = (
            product.current_quantity > 0 or product.movements.exists() or product.batches.exists()
            or product.ratings.exists() or product.shopping_items.exists()
            or product.receipt_lines.exclude(receipt=receipt).exists()
        )
        if has_history:
            continue
        # Nazwa zostaje przy pozycji, żeby ponowne zatwierdzenie założyło go znowu.
        receipt.lines.filter(product=product).update(target_name=product.label[:200])
        product.delete()
        removed += 1
    return removed


def _restore_photos(receipt):
    """Zdjęcie wraca z folderu wysyłki, jeśli nie poszło jeszcze na Dysk Google."""
    from .receipts import archive_dir

    for photo in receipt.photos.exclude(archived_name=''):
        if photo.image:
            continue
        source = archive_dir() / photo.archived_name
        if not source.exists():
            continue
        name = f'receipts/{receipt.uuid}-{photo.position}.jpg'
        target = Path(photo.image.storage.location) / name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
        except OSError:
            logger.exception('Nie udało się przywrócić zdjęcia paragonu %s', receipt.pk)
            continue
        photo.image = name
        photo.archived_name = ''
        photo.save(update_fields=['image', 'archived_name'])


@transaction.atomic
def undo_confirmation(receipt: Receipt) -> dict:
    """Cofa zatwierdzenie paragonu. Zwraca liczby: movements, products."""
    receipt = Receipt.objects.select_for_update().get(pk=receipt.pk)
    if receipt.status != Receipt.STATUS_DONE:
        raise ValueError('Ten paragon nie jest dodany do spiżarni.')
    log = receipt.confirm_log
    if not log:
        raise ValueError('Tego paragonu nie da się cofnąć - dodano go, zanim powstało cofanie. Popraw stany w spiżarni.')

    _untick(log)
    movements = _remove_movements(receipt, log)
    _restore_aliases(log)
    # Kod nadany przy zatwierdzeniu (skan nieznanego kodu) wraca na pozycję paragonu.
    for product in PantryProduct.objects.filter(pk__in=log.get('barcodes', [])).exclude(barcode=''):
        if receipt.lines.filter(barcode=product.barcode).exists():
            product.barcode = ''
            product.save(update_fields=['barcode', 'updated_at'])
    products = _remove_created_products(receipt, log)
    receipt.lines.update(movement=None)

    receipt.status = Receipt.STATUS_REVIEW
    receipt.confirmed_at = None
    receipt.confirm_log = {}
    receipt.save(update_fields=['status', 'confirmed_at', 'confirm_log', 'updated_at'])
    _restore_photos(receipt)
    return {'movements': movements, 'products': products}
