"""Łączenie dwóch produktów spiżarni w jeden.

Typowy przypadek: produkt założony z paragonu (bez kodu) okazuje się tym
samym, który już stoi w spiżarni z kodem kreskowym. Stan, historia ruchów,
partie z terminami, linie paragonów (ceny), skróty z paragonów i pozycje list
zakupów przechodzą na produkt docelowy, a duplikat znika.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from ..models import PantryBatch, PantryMovement, PantryProduct, ReceiptAlias, ReceiptLine, ShoppingListItem
from .pantry_quantities import convert_pantry_quantity, counts_in_packages, sync_package_count_from_quantity
from .receipt_parser import normalize_text

COUNTED = (PantryProduct.UNIT_PIECE, PantryProduct.UNIT_PACKAGE)


def same_name(first: str, second: str) -> bool:
    """„Masło Extra” = „maslo  extra” – bez wielkości liter, ogonków i interpunkcji."""
    first, second = normalize_text(first or ''), normalize_text(second or '')
    return bool(first) and first == second


def find_product_by_name(name: str) -> PantryProduct | None:
    """Produkt o tej samej nazwie. Najpierw dokładnie (bez wielkości liter),
    potem po normalizacji, żeby „Maslo” trafiło w „Masło”."""
    product = PantryProduct.objects.filter(name__iexact=name).first()
    if product is not None:
        return product
    key = normalize_text(name or '')
    if not key:
        return None
    for candidate in PantryProduct.objects.only('id', 'name'):
        if normalize_text(candidate.name) == key:
            return PantryProduct.objects.get(pk=candidate.pk)
    return None


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
    source.delete()
    return target
