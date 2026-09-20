"""Ilości, jednostki i opakowania w spiżarni.

Wspólne dla widoków spiżarni, list zakupów i synchronizacji listy z telefonem
(services/shopping_sync.py). Wcześniej funkcje mieszkały w views.py; widoki
nadal je stamtąd importują, więc stare odwołania działają.
"""
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from ..models import PantryProduct

PANTRY_MAX_QUANTITY = Decimal('99999999.99')


def parse_pantry_decimal(value, default='0'):
    if value in [None, '']:
        value = default
    try:
        parsed = Decimal(str(value).replace(',', '.'))
        if not parsed.is_finite():
            raise InvalidOperation
        parsed = parsed.quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        raise ValueError('Podaj poprawną liczbę.')
    if parsed < 0:
        raise ValueError('Ilość nie może być ujemna.')
    if parsed > PANTRY_MAX_QUANTITY:
        raise ValueError('Ilość jest zbyt duża.')
    return parsed


def parse_package_count(value, default=0):
    try:
        parsed = Decimal(str(value if value not in [None, ''] else default))
        if not parsed.is_finite() or parsed != parsed.to_integral_value():
            raise InvalidOperation
        count = int(parsed)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('Podaj poprawną liczbę sztuk.') from exc
    if count < 0 or count > 2147483647:
        raise ValueError('Liczba sztuk jest poza dozwolonym zakresem.')
    return count


def estimated_package_count(quantity, quantity_per_scan):
    if quantity <= 0 or quantity_per_scan <= 0:
        return 0
    return int((quantity / quantity_per_scan).to_integral_value(rounding=ROUND_CEILING))


def tracks_packages(product):
    return product.tracks_packages


def sync_package_count_from_quantity(product):
    if not tracks_packages(product):
        return False
    product.current_package_count = estimated_package_count(
        product.current_quantity,
        product.quantity_per_scan,
    )
    return True


def validate_pantry_storage_quantity(quantity):
    if quantity < 0:
        raise ValueError('Ilość nie może być ujemna.')
    if quantity > PANTRY_MAX_QUANTITY:
        raise ValueError('Ilość jest zbyt duża.')


def validate_pantry_quantity_for_unit(quantity, unit):
    if unit == PantryProduct.UNIT_PIECE and quantity != quantity.to_integral_value():
        raise ValueError('Dla jednostki "szt." podaj liczbę całkowitą.')


def convert_pantry_quantity(quantity, source_unit, target_unit):
    if source_unit == target_unit:
        return quantity
    conversions = {
        (PantryProduct.UNIT_GRAM, PantryProduct.UNIT_KILOGRAM): Decimal('0.001'),
        (PantryProduct.UNIT_KILOGRAM, PantryProduct.UNIT_GRAM): Decimal('1000'),
        (PantryProduct.UNIT_MILLILITER, PantryProduct.UNIT_LITER): Decimal('0.001'),
        (PantryProduct.UNIT_LITER, PantryProduct.UNIT_MILLILITER): Decimal('1000'),
    }
    factor = conversions.get((source_unit, target_unit))
    if factor is None:
        raise ValueError('Jednostka ważenia nie pasuje do jednostki produktu w spiżarni.')
    return (quantity * factor).quantize(Decimal('0.01'))


def normalize_shopping_quantity(quantity, unit):
    quantity = quantity.quantize(Decimal('0.01'))
    if unit == PantryProduct.UNIT_PIECE:
        quantity = quantity.to_integral_value(rounding=ROUND_CEILING)
    return quantity


def find_pantry_product(name):
    return PantryProduct.objects.filter(name__iexact=name).first()
