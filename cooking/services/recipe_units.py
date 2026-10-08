"""Jednostki w przepisach: spiżarniane (g, ml, szt...) i kuchenne (łyżka...).

- amount_text(): „500 g”, „2 łyżki”, „5 łyżek”, „½ szklanki”, „do smaku”.
- cook_prefill(): co wpisać w „Gotuj” w pole „Ilość z wagi”:
    łyżka/łyżeczka/szklanka -> ml (15 / 5 / 250 ml), gdy produkt w spiżarni
    jest liczony objętością albo jeszcze go nie ma; przy produkcie w g/szt.
    pole zostaje puste (zważ albo pomiń), bo łyżka mąki to nie łyżka oleju.
    szczypta, do smaku -> puste, wiersz można pominąć.
    sztuki -> zaokrąglone w górę (pół cebuli schodzi jako 1 szt.).
"""
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from ..models import (
    KITCHEN_UNIT_CHOICES,
    PANTRY_UNIT_CHOICES,
    RECIPE_UNIT_CHOICES,
    UNIT_GLASS,
    UNIT_MILLILITER,
    UNIT_PIECE,
    UNIT_PINCH,
    UNIT_TABLESPOON,
    UNIT_TEASPOON,
    UNIT_TO_TASTE,
)

PANTRY_UNITS = {value for value, _ in PANTRY_UNIT_CHOICES}
KITCHEN_UNITS = {value for value, _ in KITCHEN_UNIT_CHOICES}
RECIPE_UNITS = PANTRY_UNITS | KITCHEN_UNITS
ML_PER_UNIT = {UNIT_TABLESPOON: Decimal('15'), UNIT_TEASPOON: Decimal('5'), UNIT_GLASS: Decimal('250')}
NO_AMOUNT_UNITS = {UNIT_TO_TASTE}            # ilość niepotrzebna
SKIPPED_IN_COOKING = {UNIT_PINCH, UNIT_TO_TASTE}
VOLUME_UNITS = {'ml', 'l'}
LABELS = dict(RECIPE_UNIT_CHOICES)
# (1, 2-4, 5+, ułamek)
FORMS = {
    UNIT_TABLESPOON: ('łyżka', 'łyżki', 'łyżek', 'łyżki'),
    UNIT_TEASPOON: ('łyżeczka', 'łyżeczki', 'łyżeczek', 'łyżeczki'),
    UNIT_GLASS: ('szklanka', 'szklanki', 'szklanek', 'szklanki'),
    UNIT_PINCH: ('szczypta', 'szczypty', 'szczypt', 'szczypty'),
}
UNIT_GROUPS = [
    ('Do spiżarni', PANTRY_UNIT_CHOICES),
    ('Kuchenne', KITCHEN_UNIT_CHOICES),
]


def _number(quantity):
    from ..templatetags.pantry_extras import qty
    return qty(quantity)


def unit_label(unit, quantity=None):
    forms = FORMS.get(unit)
    if not forms or quantity is None:
        return LABELS.get(unit, unit)
    quantity = Decimal(quantity)
    if quantity != quantity.to_integral_value():
        return forms[3]
    whole = int(quantity)
    if whole == 1:
        return forms[0]
    if whole % 10 in (2, 3, 4) and whole % 100 not in (12, 13, 14):
        return forms[1]
    return forms[2]


def amount_text(quantity, unit):
    if unit in NO_AMOUNT_UNITS or (unit in SKIPPED_IN_COOKING and not quantity):
        return LABELS.get(unit, unit)
    return f'{_number(quantity)} {unit_label(unit, quantity)}'


@dataclass
class CookPrefill:
    quantity: str     # wartość pola (bez polskiego przecinka) albo ''
    unit: str
    optional: bool    # można zostawić puste - wiersz zostanie pominięty
    hint: str = ''


def cook_prefill(ingredient, product=None):
    """Podpowiedź do „Gotuj” dla składnika przepisu (product: z spiżarni lub None)."""
    unit, quantity = ingredient.unit, ingredient.quantity
    in_recipe = amount_text(quantity, unit)
    if unit in SKIPPED_IN_COOKING:
        target = product.unit if product else 'g'
        return CookPrefill('', target, True, f'W przepisie: {in_recipe}. Zostaw puste, jeśli nie ważysz.')
    if unit in ML_PER_UNIT:
        milliliters = (quantity * ML_PER_UNIT[unit]).quantize(Decimal('0.01'))
        if product is None or product.unit in VOLUME_UNITS:
            return CookPrefill(
                f'{milliliters.normalize():f}' if milliliters else '', UNIT_MILLILITER, False,
                f'W przepisie {in_recipe} ≈ {_number(milliliters)} ml.',
            )
        return CookPrefill(
            '', product.unit, True,
            f'W przepisie: {in_recipe}. W spiżarni liczone w {LABELS.get(product.unit, product.unit)} '
            '– zważ albo zostaw puste.',
        )
    if unit == UNIT_PIECE and quantity != quantity.to_integral_value():
        whole = quantity.to_integral_value(rounding=ROUND_CEILING)
        return CookPrefill(str(whole), unit, False, f'W przepisie {in_recipe} – ze spiżarni schodzą całe sztuki.')
    return CookPrefill(str(quantity), unit, False, '')
