"""Łączenie dwóch produktów spiżarni w jeden.

Typowy przypadek: produkt założony z paragonu (bez kodu) okazuje się tym
samym, który już stoi w spiżarni z kodem kreskowym. Stan, historia ruchów,
partie z terminami, linie paragonów (ceny), skróty z paragonów, oceny i pozycje
list zakupów przechodzą na produkt docelowy, a duplikat znika.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from ..models import PantryBatch, PantryMovement, PantryProduct, ReceiptAlias, ReceiptLine, ShoppingListItem
from .pantry_ratings import merge_ratings
from .pantry_quantities import convert_pantry_quantity, counts_in_packages, sync_package_count_from_quantity
from .pantry_names import find_product_by_name, name_taken, same_product  # noqa: F401 - receipts.py importuje stąd

COUNTED = (PantryProduct.UNIT_PIECE, PantryProduct.UNIT_PACKAGE)


def merge_factor(source: PantryProduct, target: PantryProduct) -> Decimal:
    """Ile jednostek produktu docelowego to jedna jednostka duplikatu."""
    if source.unit == target.unit or (source.unit in COUNTED and target.unit in COUNTED):
        return Decimal('1')
    if source.unit in COUNTED and counts_in_packages(target):
        return target.quantity_per_scan
    try:
        return convert_pantry_quantity(Decimal('1'), source.unit, target.unit)
    except ValueError:
        raise ValueError(
            f'Nie da się połączyć: „{source.name}” liczysz w {source.display_unit}, '
            f'a „{target.name}” w {target.display_unit}.'
        ) from None


def _scaled(quantity: Decimal, factor: Decimal) -> Decimal:
    return (quantity * factor).quantize(Decimal('0.01'))


@transaction.atomic
def merge_products(source: PantryProduct, target: PantryProduct) -> PantryProduct:
    """Przenosi wszystko z ``source`` na ``target`` i usuwa ``source``."""
    if source.pk == target.pk:
        return target
    source = PantryProduct.objects.select_for_update().get(pk=source.pk)
    target = PantryProduct.objects.select_for_update().get(pk=target.pk)
    factor = merge_factor(source, target)

    if factor == 1:
        PantryMovement.objects.filter(product=source).update(product=target)
        PantryBatch.objects.filter(product=source).update(product=target)
    else:
        for movement in PantryMovement.objects.filter(product=source):
            movement.product = target
            movement.quantity = _scaled(movement.quantity, factor)
            movement.save(update_fields=['product', 'quantity'])
        for batch in PantryBatch.objects.filter(product=source):
            batch.product = target
            batch.quantity = _scaled(batch.quantity, factor)
            batch.save(update_fields=['product', 'quantity'])
    ReceiptLine.objects.filter(product=source).update(product=target)
    ReceiptAlias.objects.filter(product=source).update(product=target)
    ShoppingListItem.objects.filter(pantry_product=source).update(pantry_product=target)
    merge_ratings(source, target)

    target.current_quantity += _scaled(source.current_quantity, factor)
    sync_package_count_from_quantity(target)
    fields = ['current_quantity', 'current_package_count', 'updated_at']
    if not target.barcode and source.barcode:
        # Kod może być tylko przy jednym produkcie: zdejmujemy go z duplikatu przed zapisem.
        barcode = source.barcode
        PantryProduct.objects.filter(pk=source.pk).update(barcode='')
        target.barcode = barcode
        fields.append('barcode')
    if not target.group_id and source.group_id:
        target.group_id = source.group_id
        fields.append('group')
    target.save(update_fields=fields)
    source_brand = source.brand
    source.delete()
    # Produkt bez firmy przejmuje firmę duplikatu - o ile nie ma już takiej pary nazwa + firma.
    if not target.brand and source_brand and not name_taken(target.name, source_brand, exclude_pk=target.pk):
        target.brand = source_brand
        target.save(update_fields=['brand', 'updated_at'])
    return target
