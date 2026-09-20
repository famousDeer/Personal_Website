"""Pełna edycja i usuwanie produktów ze wspólnej spiżarni.

Edycja może zmienić wszystko, co jest zapisane w bazie: nazwę, kod kreskowy,
kategorię, jednostkę, wielkość opakowania, stan, próg, zdjęcie i notatki.
Kilka pól ma skutki poza samym produktem - ten moduł pilnuje, żeby dane
dookoła zostały spójne:

* zmiana jednostki na przeliczalną (g <-> kg, ml <-> l) przelicza historię
  ruchów, więc prognoza zużycia dalej się zgadza; przy jednostkach, których
  nie da się przeliczyć (np. szt. -> opak.), liczby w historii zostają
  bez zmian - to zwykle poprawka źle wybranej jednostki;
* zmiana stanu zapisuje ruch "Korekta" z różnicą, a nie znika bez śladu;
  prognoza nie traktuje korekty jako zużycia ani zakupu;
* pozycje na niezakończonych listach zakupów podążają za nową nazwą,
  kategorią i jednostką produktu.
"""
from decimal import Decimal

from django.db import transaction

from .pantry_sharing import conversion_factor

TWO_PLACES = Decimal('0.01')
ADJUST_NOTE = 'Korekta w edycji produktu'


def _q(value):
    return (value or Decimal('0')).quantize(TWO_PLACES)


def units_are_convertible(source_unit, target_unit):
    return conversion_factor(source_unit, target_unit) is not None


def convert_to_unit(quantity, source_unit, target_unit):
    """Ilość w jednostce docelowej; przy jednostkach nieprzeliczalnych - bez zmian."""
    factor = conversion_factor(source_unit, target_unit)
    if factor is None:
        return _q(quantity)
    return _q(quantity * factor)


def convert_movement_history(product, source_unit, target_unit, movement_model):
    """Przelicza ilości w historii ruchów produktu. Zwraca liczbę zmienionych ruchów."""
    factor = conversion_factor(source_unit, target_unit)
    if factor is None or factor == 1:
        return 0
    movements = list(movement_model.objects.filter(product=product))
    for movement in movements:
        movement.quantity = _q(movement.quantity * factor)
        if movement.requested_quantity is not None:
            movement.requested_quantity = _q(movement.requested_quantity * factor)
    movement_model.objects.bulk_update(movements, ['quantity', 'requested_quantity'])
    return len(movements)


def sync_open_shopping_items(product, *, old_name, old_category, old_unit, shopping_item_model, shopping_list_model):
    """Aktualizuje pozycje produktu na listach, które nie są jeszcze zakończone.

    Nazwa i kategoria zmieniają się tylko tam, gdzie pozycja miała dotychczasową
    wartość produktu (ręcznie wpisanej notatki czy innej nazwy nie ruszamy).
    Jednostka pozycji zmienia się tylko wtedy, gdy nie da się jej przeliczyć na
    nową jednostkę produktu - inaczej zamknięcie listy zgłosiłoby błąd.
    """
    items = shopping_item_model.objects.filter(
        pantry_product=product,
        is_purchased=False,
    ).exclude(shopping_list__status=shopping_list_model.COMPLETED)
    changed = []
    for item in items:
        fields = []
        if old_name and item.name.casefold() == old_name.casefold() and item.name != product.name:
            item.name = product.name
            fields.append('name')
        if item.category == old_category and item.category != product.category:
            item.category = product.category
            fields.append('category')
        if item.unit == old_unit and not units_are_convertible(item.unit, product.unit):
            item.unit = product.unit
            fields.append('unit')
        if fields:
            changed.append(item)
    if changed:
        shopping_item_model.objects.bulk_update(changed, ['name', 'category', 'unit'])
    return len(changed)


def delete_file_after_commit(field_file):
    """Usuwa plik zdjęcia dopiero, gdy zmiana w bazie na pewno się zapisała."""
    if not field_file:
        return
    storage = field_file.storage
    name = field_file.name

    def _delete():
        try:
            storage.delete(name)
        except OSError:
            pass

    transaction.on_commit(_delete)
