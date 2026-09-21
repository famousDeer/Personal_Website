"""Grupy produktów: ten sam produkt różnych firm.

W spiżarni „Jogurt naturalny Pilos”, „... Piątnica” i „... Bakoma” to trzy
osobne produkty - każdy ma swój kod kreskowy i swój zapas, bo to trzy różne
rzeczy na półce. Ale w lodówce jest po prostu jogurt naturalny, więc o brakach
i o liście zakupów decyduje grupa, nie pojedyncza marka.

Wszystko liczymy w **opakowaniach**: grupa ma zapas 3 szt., minimum 2 szt.
Dzięki temu marki o różnych gramaturach dają się dodać do siebie, a lista
zakupów mówi „Jogurt naturalny 1 szt.” - w sklepie i tak bierze się opakowanie.

Prognoza korzysta z tego samego silnika co produkty (``pantry_forecast``):
podajemy mu zastępczy „produkt”, którego jednostką są opakowania, a ruchy
wszystkich marek przeliczamy na opakowania.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..models import PantryMovement, PantryProduct, ProductGroup
from .pantry_quantities import estimated_package_count


def packages_in_stock(product):
    """Ile opakowań tej marki stoi w domu.

    Produkt ważony bez skanera nie liczy opakowań, więc liczy się jako jedno,
    kiedy cokolwiek z niego zostało - lepsze to niż udawanie, że go nie ma.
    """
    if product.tracks_packages:
        return int(product.current_package_count)
    return 1 if product.current_quantity > 0 else 0


def movement_packages(movement, product):
    """Ruch marki przeliczony na opakowania."""
    if movement.package_count is not None:
        return int(movement.package_count)
    package_size = product.quantity_per_scan
    if product.unit in [PantryProduct.UNIT_PIECE, PantryProduct.UNIT_PACKAGE]:
        package_size = Decimal('1.00')
    return estimated_package_count(abs(movement.quantity), package_size)


@dataclass(frozen=True)
class GroupMovement:
    """Ruch grupy w opakowaniach - tyle, ile potrzebuje prognoza."""

    movement_type: str
    occurred_on: date
    quantity: Decimal


class GroupForecastSubject:
    """Grupa udająca produkt, żeby policzyć dla niej prognozę.

    Jednostką są opakowania, więc rozmiar opakowania to 1, a zapas i minimum
    to liczby opakowań. ``pantry_forecast`` czyta z produktu tylko te pola.
    """

    unit = PantryProduct.UNIT_PIECE
    barcode = ''
    quantity_per_scan = Decimal('1.00')

    def __init__(self, group, members=None):
        members = list(members) if members is not None else group.members()
        self.pk = f'group-{group.pk}'
        self.category = group.category
        self.current_quantity = Decimal(sum(packages_in_stock(product) for product in members))
        self.current_package_count = int(self.current_quantity)
        self.minimum_quantity = Decimal(group.minimum_packages)
        # Najdłuższy czas dostawy spośród marek: grupa jest gotowa na zakupy
        # dopiero wtedy, gdy zdąży każda z nich.
        self.restock_lead_days = max(
            [product.restock_lead_days for product in members] or [0]
        )
        created = [product.created_at for product in members if product.created_at]
        self.created_at = min(created) if created else None
        self.forecast_movements = group_movements(members)


def group_movements(members):
    """Ruchy wszystkich marek grupy, przeliczone na opakowania."""
    movements = []
    for product in members:
        cached = getattr(product, 'forecast_movements', None)
        if cached is None:
            cached = getattr(product, '_prefetched_objects_cache', {}).get('movements')
        if cached is None:
            cached = product.movements.all()
        for movement in cached:
            packages = movement_packages(movement, product)
            if packages <= 0 and movement.movement_type != PantryMovement.ADJUST:
                continue
            movements.append(GroupMovement(
                movement_type=movement.movement_type,
                occurred_on=movement.occurred_on,
                quantity=Decimal(packages),
            ))
    movements.sort(key=lambda movement: movement.occurred_on)
    return movements


def restock_target(group, members=None):
    """Do której marki dopisać zakup, gdy lista nie mówi której.

    Najpierw ta, którą kupowaliście ostatnio - zwykle po nią sięgacie znowu.
    Potem marka z kodem kreskowym, a na końcu pierwsza z brzegu.
    """
    members = list(members) if members is not None else group.members()
    if not members:
        return None
    last_purchase = (
        PantryMovement.objects
        .filter(product__in=members, movement_type=PantryMovement.PURCHASE)
        .order_by('-occurred_on', '-created_at')
        .select_related('product')
        .first()
    )
    if last_purchase is not None:
        return last_purchase.product
    with_barcode = [product for product in members if product.barcode]
    return (with_barcode or members)[0]


def groups_with_members():
    """Grupy z wczytanymi markami i ich ruchami - jedna porcja zapytań."""
    return ProductGroup.objects.prefetch_related('products__movements')


def normalized_name(value):
    return ' '.join(str(value or '').split()).casefold()


def name_words(product, brands=None):
    """Słowa nazwy bez marki, do porównywania „czy to to samo”.

    Markę znamy z katalogu (po kodzie kreskowym), więc „Jogurt naturalny Pilos”
    i „Jogurt naturalny Piątnica” zostają jako „jogurt naturalny”. Bez kodu
    zostaje cała nazwa - wtedy decyduje wspólny początek.
    """
    name = normalized_name(product.name)
    brand = normalized_name((brands or {}).get(product.barcode, ''))
    if brand and brand in name:
        name = name.replace(brand, ' ')
    return [word for word in name.replace('firmy', ' ').split() if word]


def _catalog_brands(products):
    from ..models import ProductCatalogEntry

    barcodes = [product.barcode for product in products if product.barcode]
    if not barcodes:
        return {}
    entries = ProductCatalogEntry.objects.filter(
        lookup_barcode__in=barcodes,
    ).exclude(brand='').values_list('lookup_barcode', 'brand')
    return dict(entries)


MIN_COMMON_WORDS = 2


def suggest_groups(products=None, min_members=2):
    """Propozycje: które produkty wyglądają na to samo, tylko innej firmy.

    Łączymy produkty z tej samej kategorii, których nazwy (po odjęciu marki)
    zaczynają się tak samo przez co najmniej dwa słowa. Dwa słowa, a nie jedno,
    bo „Mleko 3,2%” i „Mleko bez laktozy” to nie zamienniki, a „Jogurt naturalny
    Pilos” i „Jogurt naturalny Piątnica” już tak.

    Niczego nie łączymy sami - to podpowiedź do zatwierdzenia przez domownika.
    """
    products = [
        product for product in (products if products is not None else PantryProduct.objects.all())
        if not product.group_id
    ]
    brands = _catalog_brands(products)
    buckets = {}
    for product in products:
        words = name_words(product, brands)
        if len(words) < MIN_COMMON_WORDS:
            continue
        key = (' '.join(words[:MIN_COMMON_WORDS]), normalized_name(product.category))
        buckets.setdefault(key, []).append(product)

    proposals = []
    for (base, _category), members in buckets.items():
        if len(members) < min_members:
            continue
        # Kolejność zostaje ta z bazy (polska), więc niczego tu nie sortujemy.
        proposals.append({
            'name': common_name(members, brands) or base,
            'category': members[0].category,
            'products': members,
        })
    proposals.sort(key=lambda proposal: proposal['name'])
    return proposals


def common_name(products, brands=None):
    """Nazwa dla grupy: najdłuższy wspólny początek nazw, bez marek."""
    word_lists = [name_words(product, brands) for product in products]
    if not word_lists:
        return ''
    common = []
    for index in range(min(len(words) for words in word_lists)):
        word = word_lists[0][index]
        if any(words[index] != word for words in word_lists):
            break
        common.append(word)
    return ' '.join(common).capitalize()
