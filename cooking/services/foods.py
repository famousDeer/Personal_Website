"""Baza produktów spożywczych (model Food): lista wbudowana i wyszukiwanie.

- load_builtin_foods(): wczytuje cooking/data/foods.json (wartości z USDA
  FoodData Central, nazwy po polsku). Można wołać wiele razy: pozycje
  wbudowane są aktualizowane, a własne produkty domu zostają nietknięte -
  także wtedy, gdy noszą nazwę z listy.
- search_foods(): szukanie po słowach bez ogonków („piers kurcz”).
- food_grams(): ilość w gramach z miar domowych produktu.
"""
import json
from decimal import Decimal
from pathlib import Path

from ..models import (
    UNIT_GLASS,
    UNIT_GRAM,
    UNIT_KILOGRAM,
    UNIT_LITER,
    UNIT_MILLILITER,
    UNIT_PIECE,
    UNIT_TABLESPOON,
    UNIT_TEASPOON,
    Food,
)
from .receipt_parser import normalize_text

DATA_FILE = Path(__file__).resolve().parent.parent / 'data' / 'foods.json'
ONE_PLACE = Decimal('0.1')
BASE_FACTOR = {
    UNIT_GRAM: Decimal('1'), UNIT_MILLILITER: Decimal('1'),
    UNIT_KILOGRAM: Decimal('1000'), UNIT_LITER: Decimal('1000'),
}


def _number(value):
    return None if value is None else Decimal(str(value)).quantize(ONE_PLACE)


def load_builtin_foods():
    """Zwraca liczbę pozycji wbudowanych po wczytaniu."""
    existing = {food.name: food for food in Food.objects.all()}
    new, changed = [], []
    for item in json.loads(DATA_FILE.read_text(encoding='utf-8')):
        values = {
            'search_key': normalize_text(item['name']),
            'kcal_100': _number(item['kcal']),
            'protein_100': _number(item['protein']),
            'fat_100': _number(item['fat']),
            'carbs_100': _number(item['carbs']),
            'piece_weight': _number(item.get('piece')),
            'spoon_weight': _number(item.get('spoon')),
            'glass_weight': _number(item.get('glass')),
        }
        food = existing.get(item['name'])
        if food is None:
            new.append(Food(name=item['name'], source=Food.SOURCE_BUILTIN, **values))
        elif food.source == Food.SOURCE_BUILTIN:
            for field, value in values.items():
                setattr(food, field, value)
            changed.append(food)
    Food.objects.bulk_create(new)
    if changed:
        Food.objects.bulk_update(changed, list(values))
    return len(new) + len(changed)


def search_foods(query, limit=12):
    words = normalize_text(query or '').split()
    if not words:
        return []
    foods = Food.objects.all()
    for word in words:
        foods = foods.filter(search_key__contains=word)
    key = ' '.join(words)
    # Najpierw nazwy zaczynające się od wpisanego tekstu, potem krótsze.
    return sorted(foods[:60], key=lambda food: (not food.search_key.startswith(key), len(food.name), food.name))[:limit]


def food_grams(quantity, unit, food):
    """Ilość w gramach albo None, gdy produkt nie ma takiej miary."""
    quantity = Decimal(quantity)
    if unit in BASE_FACTOR:
        return quantity * BASE_FACTOR[unit]
    weight = {
        UNIT_PIECE: food.piece_weight,
        UNIT_TABLESPOON: food.spoon_weight,
        UNIT_TEASPOON: food.spoon_weight / 3 if food.spoon_weight else None,
        UNIT_GLASS: food.glass_weight,
    }.get(unit)
    return quantity * weight if weight else None
