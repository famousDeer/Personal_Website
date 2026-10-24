"""Składniki przepisu a spiżarnia.

- pantry_index(): nazwy do podpowiedzi w formularzu przepisu - produkty
  i grupy produktów („Jogurt naturalny” zamiast trzech marek osobno).
- resolve_pantry_product(): który produkt zdjąć ze stanu przy gotowaniu.
  Rozpoznaje też nazwę grupy - wtedy schodzi marka, której jest najwięcej.
  Dzięki temu składnik „Jogurt naturalny” nie zakłada pustego produktu.
  Sama nazwa produktu dostępnego w kilku firmach („Masło”) działa tak samo.
- ingredient_category(): kategoria dla składnika, którego nie podano
  ręcznie: z produktu lub grupy w spiżarni, a dla nowego produktu
  z reguł słów kluczowych (strona „Kategorie” w spiżarni).
"""
from collections import Counter

from ..models import PantryProduct, ProductGroup
from .categories import match_text, searchable_text, taxonomy
from .pantry_names import find_product_by_name, product_label
from .product_groups import restock_target


def _group_unit(members):
    units = Counter(member.unit for member in members)
    return units.most_common(1)[0][0] if units else PantryProduct.UNIT_GRAM


def pantry_index():
    """Lista do podpowiedzi: [{name, unit, category, kind}], bez powtórzeń nazw."""
    entries = {}
    groups = ProductGroup.objects.prefetch_related('products').order_by('name')
    for group in groups:
        members = list(group.products.all())
        entries[group.name.casefold()] = {
            'name': group.name,
            'unit': _group_unit(members),
            'category': group.category or (members[0].category if members else ''),
            'kind': 'group',
            'members': len(members),
        }
    for name, brand, unit, category in PantryProduct.objects.order_by('name', 'brand').values_list(
        'name', 'brand', 'unit', 'category',
    ):
        # Produkt o tej samej nazwie co grupa ma pierwszeństwo przy gotowaniu.
        # Kilka firm pod jedną nazwą: każda pod etykietą „Masło (Łaciate)”.
        label = product_label(name, brand)
        entries[label.casefold()] = {'name': label, 'unit': unit, 'category': category, 'kind': 'product'}
        # Przepis zwykle mówi po prostu „Masło” - przy gotowaniu schodzi
        # firma, której jest najwięcej (resolve_pantry_product).
        entries.setdefault(name.casefold(), {'name': name, 'unit': unit, 'category': category, 'kind': 'product'})
    return sorted(entries.values(), key=lambda entry: entry['name'].casefold())


def keyword_rules():
    """Reguły słów kluczowych dla skryptu: [[kategoria, [słowa...]], ...]."""
    return [[category, list(keywords)] for _, category, keywords in taxonomy().keyword_rules]


def find_group(name):
    return ProductGroup.objects.filter(name__iexact=name.strip()).first()


def resolve_pantry_product(name):
    """Produkt do zużycia przy gotowaniu albo None (nieznana nazwa)."""
    name = name.strip()
    # Sama nazwa albo etykieta z firmą; kilka firm - ta, której jest najwięcej.
    product = find_product_by_name(name)
    if product is not None:
        return product
    group = find_group(name)
    if group is None:
        return None
    members = list(group.products.order_by('name'))
    if not members:
        return None
    in_stock = [member for member in members if member.current_quantity > 0]
    if in_stock:
        return max(in_stock, key=lambda member: member.current_quantity)
    return restock_target(group, members)


def ingredient_category(name):
    product = find_product_by_name(name)
    if product is not None and product.category:
        return product.category
    group = find_group(name)
    if group is not None and group.category:
        return group.category
    match = match_text(searchable_text(name))
    return match.category if match else ''
