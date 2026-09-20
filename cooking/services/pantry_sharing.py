"""Łączenie prywatnych spiżarni w jedną wspólną.

Do września 2026 każdy użytkownik miał własną spiżarnię, więc dwie osoby
mogły mieć ten sam produkt dwa razy. Wspólna spiżarnia wymaga jednej
pozycji na nazwę i na kod kreskowy, dlatego przed dodaniem tych ograniczeń
migracja 0011 łączy duplikaty według planu z tego modułu. Ten sam plan
pokazuje komenda `preview_shared_pantry`, którą można uruchomić przed
migracją.

Funkcje przyjmują klasy modeli jako argumenty, bo migracja musi używać
modeli historycznych (apps.get_model), a komenda - bieżących.

Zasady:
* ten sam kod kreskowy - to ten sam produkt, łączymy;
* ta sama nazwa, a kod ma najwyżej jeden z nich - łączymy;
* ta sama nazwa, ale DWA RÓŻNE kody - to różne produkty (np. mleko dwóch
  marek); po połączeniu jeden kod przestałby być rozpoznawany, więc drugi
  produkt dostaje w nazwie dopisek z nazwą użytkownika;
* jednostek, których nie da się przeliczyć (szt. i g), nie łączymy -
  również dostają dopisek.
Zachowujemy ten produkt, który powstał najwcześniej.
"""
from dataclasses import dataclass, field
from decimal import Decimal

UNIT_BASE = {
    'g': ('masa', Decimal('1')),
    'kg': ('masa', Decimal('1000')),
    'ml': ('objętość', Decimal('1')),
    'l': ('objętość', Decimal('1000')),
    'szt': ('sztuki', Decimal('1')),
    'opak': ('opakowania', Decimal('1')),
}
EMPTY_CATEGORIES = {'', 'Inne'}
TWO_PLACES = Decimal('0.01')


def conversion_factor(source_unit, target_unit):
    """Mnożnik z jednostki źródłowej na docelową albo None, gdy się nie da."""
    if source_unit == target_unit:
        return Decimal('1')
    source = UNIT_BASE.get(source_unit)
    target = UNIT_BASE.get(target_unit)
    if not source or not target or source[0] != target[0]:
        return None
    return source[1] / target[1]


@dataclass
class MergeGroup:
    keeper: object
    merged: list = field(default_factory=list)                # produkty wchłonięte przez keeper
    renamed: list = field(default_factory=list)               # (produkt, nowa_nazwa, powód, czy_czyścić_kod)


def _owner(product):
    user = getattr(product, 'created_by', None)
    return getattr(user, 'username', '') or f'id{product.pk}'


def plan_pantry_merges(products):
    """Grupuje produkty o wspólnym kodzie albo nazwie i planuje, co z nimi zrobić."""
    products = sorted(products, key=lambda item: (item.created_at, item.pk))
    parent = {product.pk: product.pk for product in products}

    def find(pk):
        while parent[pk] != pk:
            parent[pk] = parent[parent[pk]]
            pk = parent[pk]
        return pk

    def union(first, second):
        first_root, second_root = find(first), find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    by_name, by_barcode = {}, {}
    for product in products:
        name_key = product.name.strip().casefold()
        if name_key in by_name:
            union(by_name[name_key], product.pk)
        else:
            by_name[name_key] = product.pk
        if product.barcode:
            if product.barcode in by_barcode:
                union(by_barcode[product.barcode], product.pk)
            else:
                by_barcode[product.barcode] = product.pk

    groups = {}
    for product in products:
        groups.setdefault(find(product.pk), []).append(product)

    plan = []
    taken_names = {product.name.strip().casefold() for product in products}
    for members in groups.values():
        if len(members) < 2:
            continue
        keeper, others = members[0], members[1:]
        group = MergeGroup(keeper=keeper)
        barcode = keeper.barcode
        for product in others:
            same_barcode = bool(product.barcode) and product.barcode == keeper.barcode
            barcode_conflict = bool(product.barcode) and bool(barcode) and product.barcode != barcode
            factor = conversion_factor(product.unit, keeper.unit)
            if factor is not None and (same_barcode or not barcode_conflict):
                group.merged.append(product)
                barcode = barcode or product.barcode
                continue
            if factor is None:
                reason = f'jednostki {product.unit} i {keeper.unit} nie dają się przeliczyć'
            else:
                reason = f'ta sama nazwa, ale inny kod kreskowy ({product.barcode})'
            # Kod zostaje, jeśli nikt inny w grupie go nie przejmuje.
            clear_barcode = bool(product.barcode) and product.barcode == barcode
            new_name = _unique_name(f'{product.name} ({_owner(product)})', taken_names)
            taken_names.add(new_name.casefold())
            group.renamed.append((product, new_name, reason, clear_barcode))
        plan.append(group)
    return plan


def _unique_name(candidate, taken_names):
    name, counter = candidate[:160], 2
    while name.casefold() in taken_names:
        suffix = f' {counter}'
        name = candidate[:160 - len(suffix)] + suffix
        counter += 1
    return name


def apply_pantry_merges(plan, movement_model, shopping_item_model):
    """Wykonuje plan. Wywoływać w transakcji (migracja robi to sama)."""
    for group in plan:
        keeper = group.keeper
        notes = [keeper.notes.strip()] if keeper.notes.strip() else []
        for product in group.merged:
            factor = conversion_factor(product.unit, keeper.unit)
            keeper.current_quantity = (
                keeper.current_quantity + product.current_quantity * factor
            ).quantize(TWO_PLACES)
            keeper.current_package_count += product.current_package_count
            keeper.minimum_quantity = max(
                keeper.minimum_quantity,
                (product.minimum_quantity * factor).quantize(TWO_PLACES),
            )
            keeper.restock_lead_days = max(keeper.restock_lead_days, product.restock_lead_days)
            if keeper.category in EMPTY_CATEGORIES and product.category not in EMPTY_CATEGORIES:
                keeper.category = product.category
            if not keeper.barcode and product.barcode:
                keeper.barcode = product.barcode
            if not keeper.image and product.image:
                keeper.image = product.image.name
            if product.notes.strip() and product.notes.strip() not in notes:
                notes.append(product.notes.strip())

            # Historia ruchów przechodzi do zachowanego produktu, w jego
            # jednostce - od niej zależy prognoza zużycia.
            for movement in movement_model.objects.filter(product_id=product.pk):
                movement.product_id = keeper.pk
                if factor != 1:
                    movement.quantity = (movement.quantity * factor).quantize(TWO_PLACES)
                    if movement.requested_quantity is not None:
                        movement.requested_quantity = (
                            movement.requested_quantity * factor
                        ).quantize(TWO_PLACES)
                movement.save()
            shopping_item_model.objects.filter(pantry_product_id=product.pk).update(
                pantry_product_id=keeper.pk,
            )
            # Kod musi zniknąć z usuwanego produktu, zanim trafi do zachowanego.
            type(product).objects.filter(pk=product.pk).delete()

        keeper.notes = '\n'.join(notes)
        keeper.save()

        for product, new_name, _reason, clear_barcode in group.renamed:
            product.name = new_name
            if clear_barcode:
                product.barcode = ''
            product.save()


def describe_plan(plan):
    """Czytelny opis planu - do podglądu i do logu migracji."""
    lines = []
    for group in plan:
        keeper = group.keeper
        lines.append(f'"{keeper.name}" ({_owner(keeper)}, {keeper.unit}) - zostaje')
        for product in group.merged:
            factor = conversion_factor(product.unit, keeper.unit)
            converted = (product.current_quantity * factor).quantize(TWO_PLACES)
            lines.append(
                f'    + "{product.name}" ({_owner(product)}): {product.current_quantity} {product.unit}'
                f' -> +{converted} {keeper.unit}, {product.current_package_count} opak.,'
                f' historia ruchów przeniesiona'
            )
        for product, new_name, reason, clear_barcode in group.renamed:
            extra = ', kod przejmuje produkt zachowany' if clear_barcode else ''
            lines.append(f'    ~ "{product.name}" ({_owner(product)}) -> "{new_name}" ({reason}{extra})')
    return lines
