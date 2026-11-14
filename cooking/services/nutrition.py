"""Kalorie i makroskładniki: produkty, przepisy, dziennik posiłków.

Źródło prawdy to produkt w spiżarni: kcal, białko, tłuszcz i węglowodany
na 100 g/ml. Gdy produkt ich nie ma, liczy się wpis Open Food Facts dla
jego kodu kreskowego (cache w ProductCatalogEntry - bez sięgania do sieci).
Nazwa, której nie ma w spiżarni, trafia do bazy produktów (Food, services/
foods.py) - stąd „pierś z kurczaka” i miary domowe (sztuka, łyżka, szklanka).

- NutritionIndex: wszystkie produkty i grupy wczytane raz, żeby lista
  przepisów nie robiła zapytania na każdy składnik.
- grams(): ilość składnika w gramach (ml liczymy 1:1). Łyżki i szklanki
  tylko dla produktów liczonych objętością - jak w „Gotuj”, bo łyżka mąki
  to nie łyżka oleju. Sztuki i opakowania potrzebują wagi sztuki.
- recipe_nutrition(): suma przepisu, porcja i lista składników, których
  nie dało się policzyć (z powodem).
"""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from ..models import (
    UNIT_GRAM,
    UNIT_KILOGRAM,
    UNIT_LITER,
    UNIT_MILLILITER,
    UNIT_PACKAGE,
    UNIT_PIECE,
    Food,
    PantryProduct,
    ProductCatalogEntry,
    ProductGroup,
)
from .foods import food_grams
from .receipt_parser import normalize_text
from .recipe_units import ML_PER_UNIT, SKIPPED_IN_COOKING, VOLUME_UNITS

ZERO = Decimal('0')
ONE_PLACE = Decimal('0.1')
BASE_FACTOR = {
    UNIT_GRAM: Decimal('1'),
    UNIT_MILLILITER: Decimal('1'),
    UNIT_KILOGRAM: Decimal('1000'),
    UNIT_LITER: Decimal('1000'),
}
NUTRIENT_FIELDS = ('kcal_100', 'protein_100', 'fat_100', 'carbs_100')

REASON_UNKNOWN = 'nie ma go w spiżarni ani w bazie produktów'
REASON_NO_DATA = 'produkt nie ma wartości odżywczych'
REASON_UNIT = 'nie da się przeliczyć na gramy'


def parse_nutrient(value, label, maximum=Decimal('9999')):
    """Liczba z formularza („12,5”) albo None dla pustego pola."""
    text = str(value or '').strip().replace(' ', '').replace(',', '.')
    if not text:
        return None
    try:
        number = Decimal(text)
        if not number.is_finite():
            raise InvalidOperation
    except InvalidOperation:
        raise ValueError(f'{label}: podaj liczbę.') from None
    if number < 0 or number > maximum:
        raise ValueError(f'{label}: podaj liczbę od 0 do {maximum}.')
    return number


@dataclass(frozen=True)
class Nutrients:
    kcal: Decimal = ZERO
    protein: Decimal = ZERO
    fat: Decimal = ZERO
    carbs: Decimal = ZERO

    def __add__(self, other):
        return Nutrients(
            self.kcal + other.kcal, self.protein + other.protein,
            self.fat + other.fat, self.carbs + other.carbs,
        )

    def scaled(self, factor):
        factor = Decimal(factor)
        return Nutrients(self.kcal * factor, self.protein * factor, self.fat * factor, self.carbs * factor)

    def rounded(self):
        return Nutrients(*(value.quantize(ONE_PLACE) for value in (self.kcal, self.protein, self.fat, self.carbs)))


def _own_nutrients(source):
    """Wartości na 100 g z produktu albo wpisu katalogu; bez kcal - None."""
    if source is None or source.kcal_100 is None:
        return None
    return Nutrients(
        source.kcal_100, source.protein_100 or ZERO, source.fat_100 or ZERO, source.carbs_100 or ZERO,
    )


class NutritionIndex:
    def __init__(self):
        products = list(PantryProduct.objects.all())
        barcodes = [product.barcode for product in products if product.barcode and product.kcal_100 is None]
        self._catalog = {
            entry.lookup_barcode: entry
            for entry in ProductCatalogEntry.objects.filter(
                source=ProductCatalogEntry.SOURCE_OPEN_FOOD_FACTS,
                status=ProductCatalogEntry.STATUS_FOUND,
                lookup_barcode__in=barcodes,
                kcal_100__isnull=False,
            )
        } if barcodes else {}
        self._by_id = {product.pk: product for product in products}
        self._by_name = {}
        for product in products:
            for key in {normalize_text(product.name), normalize_text(product.label)}:
                if key:
                    self._by_name.setdefault(key, []).append(product)
        # Grupa „ten sam produkt, inna firma”: składnik może nosić jej nazwę.
        group_names = dict(ProductGroup.objects.values_list('pk', 'name'))
        for product in products:
            key = normalize_text(group_names.get(product.group_id, ''))
            if key and product not in self._by_name.get(key, []):
                self._by_name.setdefault(key, []).append(product)

        # Baza produktów spożywczych: dla składników, których nie ma w spiżarni,
        # i dla miar domowych (sztuka, łyżka, szklanka).
        self._foods = {food.search_key: food for food in Food.objects.all()}

    def food(self, name):
        return self._foods.get(normalize_text(name or ''))

    def catalog_entry(self, product):
        return self._catalog.get(product.barcode) if product.barcode else None

    def per_100(self, product):
        return _own_nutrients(product) or _own_nutrients(self.catalog_entry(product))

    def products(self):
        return self._by_id.values()

    def product(self, name):
        """Produkt dla nazwy składnika: najpierw taki, który ma wartości
        odżywcze, potem bez firmy, potem ten, którego jest najwięcej."""
        candidates = self._by_name.get(normalize_text(name or ''), [])
        if not candidates:
            return None
        return min(candidates, key=lambda product: (
            self.per_100(product) is None, bool(product.brand), -product.current_quantity, product.pk,
        ))


def grams(quantity, unit, product):
    """Ilość w gramach (ml = g) albo None, gdy nie da się jej przeliczyć."""
    quantity = Decimal(quantity)
    if unit in BASE_FACTOR:
        return quantity * BASE_FACTOR[unit]
    if unit in ML_PER_UNIT:
        return quantity * ML_PER_UNIT[unit] if product.unit in VOLUME_UNITS else None
    if unit in (UNIT_PIECE, UNIT_PACKAGE):
        if product.unit_weight:
            return quantity * product.unit_weight
        # Opakowanie produktu liczonego wagą: jego wielkość już znamy.
        if unit == UNIT_PACKAGE and product.unit in BASE_FACTOR:
            return quantity * product.quantity_per_scan * BASE_FACTOR[product.unit]
    return None


def product_nutrients(index, product, quantity, unit):
    """(Nutrients, '') dla ilości produktu albo (None, powód)."""
    per_100 = index.per_100(product)
    if per_100 is None:
        return None, REASON_NO_DATA
    weight = grams(quantity, unit, product)
    if weight is None:
        return None, REASON_UNIT
    return per_100.scaled(weight / 100), ''


def food_nutrients(food, quantity, unit):
    """(Nutrients, '') dla ilości produktu z bazy albo (None, powód)."""
    weight = food_grams(quantity, unit, food)
    if weight is None:
        return None, REASON_UNIT
    return _own_nutrients(food).scaled(weight / 100), ''


def named_nutrients(index, name, quantity, unit):
    """Wartości dla nazwy (składnik przepisu, wpis w dzienniku).

    Zwraca (Nutrients, '', produkt spiżarni, pozycja bazy) albo
    (None, powód, ...). Wartości na 100 g: spiżarnia, potem baza produktów.
    Waga: spiżarnia, a gdy się nie da (łyżka masła, sztuka bez wagi) -
    miary domowe z bazy.
    """
    product, food = index.product(name), index.food(name)
    if product is None and food is None:
        return None, REASON_UNKNOWN, None, None
    per_100 = (index.per_100(product) if product else None) or _own_nutrients(food)
    if per_100 is None:
        return None, REASON_NO_DATA, product, food
    weight = grams(quantity, unit, product) if product else None
    if weight is None and food is not None:
        weight = food_grams(quantity, unit, food)
    if weight is None:
        return None, REASON_UNIT, product, food
    return per_100.scaled(weight / 100), '', product, food


@dataclass
class RecipeNutrition:
    total: Nutrients = Nutrients()
    portion: Nutrients = Nutrients()
    counted: int = 0
    missing: list = field(default_factory=list)   # [(nazwa, powód)]

    @property
    def complete(self):
        return self.counted > 0 and not self.missing

    @property
    def missing_names(self):
        return ', '.join(name for name, _ in self.missing)


def recipe_nutrition(recipe, index):
    result = RecipeNutrition()
    total = Nutrients()
    for item in recipe.ingredient_items.all():
        if item.unit in SKIPPED_IN_COOKING:
            continue  # szczypta, do smaku
        nutrients, reason, _, _ = named_nutrients(index, item.name, item.quantity, item.unit)
        if nutrients is None:
            result.missing.append((item.name, reason))
            continue
        total += nutrients
        result.counted += 1
    result.total = total.rounded()
    result.portion = total.scaled(Decimal(1) / max(recipe.portions, 1)).rounded()
    return result


def recipe_portion(recipe, index):
    """Porcja do dziennika i na listę przepisów: (Nutrients, RecipeNutrition).

    Kcal wpisane ręcznie w przepisie wygrywają z wyliczonymi; makro zawsze
    pochodzi ze składników. Nic nie wiadomo - (None, wynik).
    """
    computed = recipe_nutrition(recipe, index)
    portion = computed.portion if computed.counted else None
    if recipe.kcal:
        base = portion or Nutrients()
        portion = Nutrients(Decimal(recipe.kcal), base.protein, base.fat, base.carbs)
    return portion, computed
