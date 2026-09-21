"""Lista zakupów: odhaczanie z uzupełnianiem spiżarni i synchronizacja z telefonem.

Telefon trzyma kopię aktywnych list i kolejkę zmian zrobionych bez połączenia
z domowym serwerem. Po powrocie do sieci wysyła kolejkę (``apply_operations``),
a w odpowiedzi dostaje świeży stan (``shopping_snapshot``).

Zasady:
* każda operacja ma własny UUID - powtórzona paczka nie zmienia niczego drugi
  raz, serwer odsyła zapamiętany wynik (tabela ShoppingSyncOperation);
* odhaczenie niesie stan ("kupione" / "niekupione"), a nie przełączenie, więc
  powtórka nie odwraca odhaczenia;
* "ostatnia zmiana wygrywa" według czasu akcji na urządzeniu, osobno dla
  odhaczenia i dla ilości; starsza zmiana niż zapisana na serwerze jest pomijana;
* pozycja ze spiżarni po odhaczeniu od razu uzupełnia zapas (ruch "Zakup"),
  a cofnięcie odhaczenia ten ruch usuwa; zmiana ilości odhaczonej pozycji
  poprawia ruch; usunięcie odhaczonej pozycji zostawia zakup w spiżarni;
* pozycji z list zakończonych albo usuniętych nie zmieniamy - operacja
  dostaje status "skipped" z wyjaśnieniem dla użytkownika.
"""
import uuid as uuid_module
from datetime import timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ..constants import PANTRY_CATEGORIES, PANTRY_CATEGORY_OTHER
from ..models import (
    PANTRY_UNIT_CHOICES,
    PantryMovement,
    PantryProduct,
    ShopLayout,
    ShoppingList,
    ShoppingListItem,
    ShoppingSyncOperation,
)
from .pantry_quantities import (
    convert_pantry_quantity,
    counts_in_packages,
    estimated_package_count,
    find_pantry_product,
    package_quantity_to_product_unit,
    parse_pantry_decimal,
    sync_package_count_from_quantity,
    tracks_packages,
    validate_pantry_quantity_for_unit,
    validate_pantry_storage_quantity,
)

PACKAGE_UNITS = (PantryProduct.UNIT_PIECE, PantryProduct.UNIT_PACKAGE)

OP_ADD = 'item.add'
OP_SET_PURCHASED = 'item.set_purchased'
OP_SET_QUANTITY = 'item.set_quantity'
OP_DELETE = 'item.delete'
OP_PANTRY_MOVEMENT = 'pantry.movement'
OP_SHOP_ADD = 'shop.add'
OP_SHOP_ORDER = 'shop.set_order'
OP_LIST_SHOP = 'list.set_shop'
OP_TYPES = (
    OP_ADD, OP_SET_PURCHASED, OP_SET_QUANTITY, OP_DELETE,
    OP_PANTRY_MOVEMENT, OP_SHOP_ADD, OP_SHOP_ORDER, OP_LIST_SHOP,
)

STATUS_APPLIED = 'applied'
STATUS_SKIPPED = 'skipped'
STATUS_REJECTED = 'rejected'

MAX_OPERATIONS_PER_SYNC = 500
TWO_PLACES = Decimal('0.01')
UNIT_LABELS = dict(PANTRY_UNIT_CHOICES)


class OperationSkipped(Exception):
    """Operacja poprawna, ale nieaktualna (konflikt) - pomijamy ją z wyjaśnieniem."""


class OperationRejected(Exception):
    """Operacja z błędnymi danymi - odrzucamy ją na stałe."""


# ---------------------------------------------------------------------------
# Spiżarnia
# ---------------------------------------------------------------------------

def _pantry_product_for(item, user=None, create_missing=False):
    product = item.pantry_product or find_pantry_product(item.name)
    if product is None and create_missing:
        product = PantryProduct.objects.create(
            created_by=user,
            name=item.name,
            category=item.category or PANTRY_CATEGORY_OTHER,
            unit=item.unit,
            current_quantity=Decimal('0.00'),
            minimum_quantity=Decimal('0.00'),
        )
    return product


def pantry_quantity_for(item, product):
    """Ilość pozycji w jednostce produktu; ValueError, gdy się nie da.

    „2 szt.” produktu mierzonego wagą albo objętością znaczy dwa opakowania,
    więc do spiżarni trafia dwa razy tyle, ile mieści jedno opakowanie.
    """
    if item.unit in PACKAGE_UNITS and counts_in_packages(product):
        quantity = package_quantity_to_product_unit(item.quantity, product)
    else:
        quantity = convert_pantry_quantity(item.quantity, item.unit, product.unit)
    validate_pantry_quantity_for_unit(quantity, product.unit)
    return quantity


def add_purchase_to_pantry(item, user=None, create_missing=False):
    """Dodaje kupioną pozycję do spiżarni. Zwraca ostrzeżenie albo ''.

    Bez ``create_missing`` działa tylko dla produktów, które już są w spiżarni
    (tak działa odhaczanie). Zakończenie listy tworzy brakujące produkty.
    Pozycja, która już uzupełniła spiżarnię, nie dodaje się drugi raz.
    """
    if item.pantry_movement_id:
        return ''
    product = _pantry_product_for(item, user=user, create_missing=create_missing)
    if product is None:
        return ''
    product = PantryProduct.objects.select_for_update().get(pk=product.pk)
    try:
        quantity = pantry_quantity_for(item, product)
        validate_pantry_storage_quantity(product.current_quantity + quantity)
    except ValueError as exc:
        return f'{item.name}: nie dodano do spiżarni - {exc}'

    if product.current_package_count == 0 and product.current_quantity > 0:
        sync_package_count_from_quantity(product)
    package_tracking = tracks_packages(product)
    package_count = (
        estimated_package_count(quantity, product.quantity_per_scan)
        if package_tracking
        else None
    )
    product.current_quantity += quantity
    if package_tracking:
        sync_package_count_from_quantity(product)
    if item.category and not product.category:
        product.category = item.category
    product.save(update_fields=['current_quantity', 'current_package_count', 'category', 'updated_at'])
    movement = PantryMovement.objects.create(
        product=product,
        movement_type=PantryMovement.PURCHASE,
        quantity=quantity,
        occurred_on=timezone.localdate(),
        note=f'Lista zakupów: {item.shopping_list.title}'[:255],
        package_count=package_count,
    )
    item.pantry_product = product
    item.pantry_movement = movement
    return ''


def remove_purchase_from_pantry(item):
    """Cofa zakup dodany przy odhaczeniu: zdejmuje ilość i usuwa ruch."""
    movement = item.pantry_movement
    if movement is None:
        return
    product = PantryProduct.objects.select_for_update().get(pk=movement.product_id)
    product.current_quantity = max(Decimal('0.00'), product.current_quantity - movement.quantity)
    if tracks_packages(product):
        sync_package_count_from_quantity(product)
    product.save(update_fields=['current_quantity', 'current_package_count', 'updated_at'])
    item.pantry_movement = None
    movement.delete()


def refresh_pantry_purchase(item, user=None):
    """Po zmianie ilości, jednostki albo nazwy odhaczonej pozycji poprawia zakup."""
    if not item.pantry_movement_id:
        return ''
    remove_purchase_from_pantry(item)
    return add_purchase_to_pantry(item, user=user)


ITEM_PURCHASE_FIELDS = [
    'is_purchased', 'purchased_at', 'purchased_by', 'purchased_changed_at',
    'pantry_product', 'pantry_movement', 'updated_at',
]


def set_item_purchased(item, purchased, user=None, when=None):
    """Ustawia stan "kupione". Zwraca ostrzeżenie (np. jednostka nie pasuje) albo ''."""
    when = when or timezone.now()
    warning = ''
    if purchased and not item.is_purchased:
        item.is_purchased = True
        item.purchased_at = when
        item.purchased_by = user
        warning = add_purchase_to_pantry(item, user=user)
    elif not purchased and item.is_purchased:
        item.is_purchased = False
        item.purchased_at = None
        item.purchased_by = None
        remove_purchase_from_pantry(item)
    item.purchased_changed_at = when
    item.save(update_fields=ITEM_PURCHASE_FIELDS)
    return warning


def complete_shopping_list(shopping_list, user=None):
    """Kończy listę. Zwraca (dodane_teraz, dodane_przy_odhaczaniu, błędy).

    Pozycje, które uzupełniły spiżarnię przy odhaczeniu, są pomijane.
    Pozostałe kupione pozycje trafiają do spiżarni jak dotąd - brakujące
    produkty są tworzone. Przy jakimkolwiek błędzie nic się nie zmienia.
    """
    items = [
        item for item in shopping_list.items.select_related('pantry_product', 'pantry_movement')
        if item.is_purchased
    ]
    already = [item for item in items if item.pantry_movement_id]
    pending = [item for item in items if not item.pantry_movement_id]
    if not items:
        return 0, 0, ['Zaznacz przynajmniej jedną kupioną pozycję.']

    errors = []
    for item in pending:
        product = _pantry_product_for(item)
        try:
            validate_pantry_quantity_for_unit(item.quantity, item.unit)
            if product is not None:
                pantry_quantity_for(item, product)
        except ValueError as exc:
            errors.append(f'{item.name}: {exc}')
    if errors:
        return 0, len(already), errors

    for item in pending:
        warning = add_purchase_to_pantry(item, user=user, create_missing=True)
        if warning:
            raise ValueError(warning)
        item.save(update_fields=['pantry_product', 'pantry_movement', 'updated_at'])
    shopping_list.status = ShoppingList.COMPLETED
    shopping_list.save(update_fields=['status', 'updated_at'])
    return len(pending), len(already), []


# ---------------------------------------------------------------------------
# Stan dla telefonu
# ---------------------------------------------------------------------------

def item_json(item):
    return {
        'uuid': str(item.uuid),
        'name': item.name,
        'quantity': format(item.quantity, '.2f'),
        'unit': item.unit,
        'unit_label': UNIT_LABELS.get(item.unit, item.unit),
        'category': item.category,
        'note': item.note,
        'is_purchased': item.is_purchased,
        'purchased_by': item.purchased_by.username if item.purchased_by_id and item.purchased_by else '',
        'in_pantry': bool(item.pantry_product_id),
        'added_to_pantry': bool(item.pantry_movement_id),
    }


def sort_items_by_shop(shopping_list, items):
    """Pozycje w kolejności alejek sklepu; bez sklepu - jak dotąd (alfabetycznie)."""
    if not shopping_list.shop_id:
        return list(items)
    rank = {name: index for index, name in enumerate(shopping_list.shop.category_order)}
    return sorted(
        items,
        key=lambda item: (
            item.is_purchased,
            rank.get(item.category, len(rank) + 1),
            item.category,
            item.name,
        ),
    )


def shopping_item_defaults(product):
    """Ile i w czym dopisać produkt ze spiżarni do listy zakupów.

    Produkt mierzony wagą albo objętością, ale kupowany w opakowaniach
    (np. mąka 1 kg), idzie na listę jako „1 szt.” - w sklepie bierze się paczkę.
    Produkt bez znanego rozmiaru opakowania zostaje przy swojej jednostce.
    """
    if counts_in_packages(product) or product.unit in PACKAGE_UNITS:
        return Decimal('1.00'), PantryProduct.UNIT_PIECE
    quantity = product.quantity_per_scan if product.quantity_per_scan > 0 else Decimal('1.00')
    return quantity.quantize(TWO_PLACES), product.unit


def pantry_product_json(product):
    add_quantity, add_unit = shopping_item_defaults(product)
    return {
        'id': product.id,
        'name': product.name,
        'barcode': product.barcode,
        'unit': product.unit,
        'unit_label': UNIT_LABELS.get(product.unit, product.unit),
        'category': product.category,
        'package': format(product.quantity_per_scan, '.2f'),
        'quantity': format(product.current_quantity, '.2f'),
        'packages': product.current_package_count,
        'tracks_packages': product.tracks_packages,
        'minimum': format(product.minimum_quantity, '.2f'),
        'status': product.stock_status,
        # Ile dopisać do listy jednym przyciskiem (serwer decyduje, nie telefon).
        'add_quantity': format(add_quantity, '.2f'),
        'add_unit': add_unit,
        'add_unit_label': UNIT_LABELS.get(add_unit, add_unit),
    }


def apply_pantry_scan(product, action, count, occurred_on, note=''):
    """Zeskanowane zużycie albo zakup: ten sam rachunek co w skanerze spiżarni."""
    quantity = (product.quantity_per_scan * Decimal(count)).quantize(TWO_PLACES)
    if product.current_package_count == 0 and product.current_quantity > 0:
        sync_package_count_from_quantity(product)
    before_quantity = product.current_quantity
    before_packages = product.current_package_count
    insufficient = False

    if action == PantryMovement.CONSUME:
        insufficient = quantity > before_quantity or count > before_packages
        fulfilled = min(quantity, before_quantity)
        fulfilled_packages = min(count, before_packages)
        product.current_quantity = before_quantity - fulfilled
        product.current_package_count = max(0, before_packages - fulfilled_packages)
        if product.current_quantity == 0:
            product.current_package_count = 0
    else:
        validate_pantry_storage_quantity(before_quantity + quantity)
        fulfilled = quantity
        fulfilled_packages = count
        product.current_quantity = before_quantity + quantity
        product.current_package_count = before_packages + count

    product.save(update_fields=['current_quantity', 'current_package_count', 'updated_at'])
    PantryMovement.objects.create(
        product=product,
        movement_type=action,
        quantity=fulfilled,
        requested_quantity=quantity,
        package_count=fulfilled_packages,
        requested_package_count=count,
        stock_was_insufficient=insufficient,
        occurred_on=occurred_on,
        note=note[:255],
    )
    return insufficient


def shopping_snapshot():
    lists = ShoppingList.objects.filter(status=ShoppingList.ACTIVE).select_related(
        'created_by', 'shop',
    ).prefetch_related('items__purchased_by')
    return {
        'server_time': timezone.now().isoformat(),
        'lists': [
            {
                'id': shopping_list.id,
                'title': shopping_list.title,
                'source': shopping_list.source,
                'created_by': shopping_list.created_by.username if shopping_list.created_by else '',
                'updated_at': shopping_list.updated_at.isoformat(),
                'shop': str(shopping_list.shop.uuid) if shopping_list.shop_id else '',
                'items': [item_json(item) for item in shopping_list.items.all()],
            }
            for shopping_list in lists
        ],
        'shops': [
            {'uuid': str(shop.uuid), 'name': shop.name, 'order': list(shop.category_order)}
            for shop in ShopLayout.objects.all()
        ],
        # Spiżarnia w telefonie: podpowiedzi przy dopisywaniu pozycji, podgląd
        # stanu przy półce w sklepie i skanowanie kodów bez połączenia.
        'products': [pantry_product_json(product) for product in PantryProduct.objects.order_by('name')],
    }


# ---------------------------------------------------------------------------
# Operacje z kolejki telefonu
# ---------------------------------------------------------------------------

def _operation_time(raw_value):
    now = timezone.now()
    parsed = parse_datetime(str(raw_value or '')) if raw_value else None
    if parsed is None:
        return now
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    # Zegar telefonu może się spieszyć - zmiana "z przyszłości" wygrywałaby
    # ze wszystkim, co nastąpi później.
    return min(parsed, now)


def _parse_uuid(value, label):
    try:
        return uuid_module.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise OperationRejected(f'{label}: nieprawidłowy identyfikator.') from None


def _locked_item(raw, missing_ok=False):
    item_uuid = _parse_uuid(raw.get('item'), 'Pozycja')
    item = (
        ShoppingListItem.objects.select_for_update(of=('self',))
        .select_related('shopping_list', 'pantry_movement', 'pantry_product')
        .filter(uuid=item_uuid)
        .first()
    )
    if item is None:
        if missing_ok:
            return None
        raise OperationSkipped('Pozycja została usunięta z listy na innym urządzeniu.')
    if item.shopping_list.status == ShoppingList.COMPLETED:
        raise OperationSkipped(f'Lista „{item.shopping_list.title}” jest już zakończona.')
    return item


def _parse_quantity(raw_value, unit):
    try:
        quantity = parse_pantry_decimal(raw_value, default='1')
        if quantity <= 0:
            raise ValueError('Ilość musi być większa od zera.')
        validate_pantry_quantity_for_unit(quantity, unit)
    except ValueError as exc:
        raise OperationRejected(str(exc)) from None
    return quantity


def _touch_list(shopping_list):
    shopping_list.save(update_fields=['updated_at'])


def _op_add(raw, user, when):
    data = raw.get('data') or {}
    if not isinstance(data, dict):
        raise OperationRejected('Brak danych pozycji.')
    item_uuid = _parse_uuid(data.get('uuid'), 'Pozycja')
    if ShoppingListItem.objects.filter(uuid=item_uuid).exists():
        return 'Pozycja już jest na liście.'
    try:
        list_id = int(raw.get('list'))
    except (TypeError, ValueError):
        raise OperationRejected('Nieprawidłowa lista.') from None
    shopping_list = ShoppingList.objects.select_for_update().filter(pk=list_id).first()
    if shopping_list is None:
        raise OperationSkipped('Lista została usunięta na innym urządzeniu.')
    if shopping_list.status == ShoppingList.COMPLETED:
        raise OperationSkipped(f'Lista „{shopping_list.title}” jest już zakończona.')

    name = str(data.get('name') or '').strip()
    if not name:
        raise OperationRejected('Nazwa produktu jest wymagana.')
    if len(name) > 160:
        raise OperationRejected('Nazwa produktu może mieć maksymalnie 160 znaków.')
    unit = data.get('unit') or PantryProduct.UNIT_PIECE
    if unit not in UNIT_LABELS:
        raise OperationRejected('Nieznana jednostka.')
    quantity = _parse_quantity(data.get('quantity'), unit)
    category = str(data.get('category') or '').strip()
    if category and category not in PANTRY_CATEGORIES:
        category = ''
    product = find_pantry_product(name)
    ShoppingListItem.objects.create(
        shopping_list=shopping_list,
        uuid=item_uuid,
        pantry_product=product,
        name=name,
        quantity=quantity,
        unit=unit,
        category=category or (product.category if product else ''),
        note=str(data.get('note') or '').strip()[:255],
        quantity_changed_at=when,
    )
    _touch_list(shopping_list)
    return ''


def _op_set_purchased(raw, user, when):
    item = _locked_item(raw)
    purchased = raw.get('purchased')
    if not isinstance(purchased, bool):
        raise OperationRejected('Brak stanu odhaczenia.')
    if item.purchased_changed_at and when < item.purchased_changed_at:
        raise OperationSkipped(f'„{item.name}”: ktoś zmienił odhaczenie później.')
    warning = set_item_purchased(item, purchased, user=user, when=when)
    _touch_list(item.shopping_list)
    return warning


def _op_set_quantity(raw, user, when):
    item = _locked_item(raw)
    quantity = _parse_quantity(raw.get('quantity'), item.unit)
    if item.quantity_changed_at and when < item.quantity_changed_at:
        raise OperationSkipped(f'„{item.name}”: ktoś zmienił ilość później.')
    warning = ''
    if quantity != item.quantity:
        item.quantity = quantity
        warning = refresh_pantry_purchase(item, user=user)
    item.quantity_changed_at = when
    item.save(update_fields=['quantity', 'quantity_changed_at', 'pantry_product', 'pantry_movement', 'updated_at'])
    _touch_list(item.shopping_list)
    return warning


def _op_delete(raw, user, when):
    item = _locked_item(raw, missing_ok=True)
    if item is None:
        return 'Pozycja była już usunięta.'
    shopping_list = item.shopping_list
    item.delete()  # zakup, który uzupełnił spiżarnię, zostaje w spiżarni
    _touch_list(shopping_list)
    return ''


def _op_pantry_movement(raw, user, when):
    try:
        product_id = int(raw.get('product'))
    except (TypeError, ValueError):
        raise OperationRejected('Nieprawidłowy produkt.') from None
    action = raw.get('action')
    if action not in (PantryMovement.CONSUME, PantryMovement.PURCHASE):
        raise OperationRejected('Wybierz „zużyto” albo „dokupiono”.')
    try:
        count = int(raw.get('count', 1))
    except (TypeError, ValueError):
        raise OperationRejected('Podaj poprawną liczbę opakowań.') from None
    if count < 1 or count > 9999:
        raise OperationRejected('Liczba opakowań musi mieścić się w zakresie 1-9999.')

    product = PantryProduct.objects.select_for_update().filter(pk=product_id).first()
    if product is None:
        raise OperationSkipped('Produkt został usunięty ze spiżarni.')
    try:
        insufficient = apply_pantry_scan(
            product, action, count, timezone.localtime(when).date(), note='Skan w trybie zakupów',
        )
    except ValueError as exc:
        raise OperationRejected(str(exc)) from None
    if insufficient:
        return f'{product.name}: w spiżarni było mniej, zapisano tyle, ile było.'
    return ''


def _op_shop_add(raw, user, when):
    shop_uuid = _parse_uuid(raw.get('shop'), 'Sklep')
    if ShopLayout.objects.filter(uuid=shop_uuid).exists():
        return 'Ten sklep już jest zapisany.'
    name = str(raw.get('name') or '').strip()
    if not name:
        raise OperationRejected('Nazwa sklepu jest wymagana.')
    if len(name) > 80:
        raise OperationRejected('Nazwa sklepu może mieć maksymalnie 80 znaków.')
    existing = ShopLayout.objects.filter(name__iexact=name).first()
    if existing:
        # Ten sam sklep dodany równolegle na drugim telefonie - nie tworzymy kopii.
        raise OperationSkipped(f'Sklep „{existing.name}” już istnieje.')
    ShopLayout.objects.create(
        uuid=shop_uuid, name=name, created_by=user,
        category_order=_clean_category_order(raw.get('order')),
    )
    return ''


def _clean_category_order(value):
    if not isinstance(value, list):
        return []
    order = []
    for name in value:
        name = str(name or '').strip()
        if name in PANTRY_CATEGORIES and name not in order:
            order.append(name)
    return order


def _op_shop_order(raw, user, when):
    shop_uuid = _parse_uuid(raw.get('shop'), 'Sklep')
    shop = ShopLayout.objects.select_for_update().filter(uuid=shop_uuid).first()
    if shop is None:
        raise OperationSkipped('Sklep został usunięty na innym urządzeniu.')
    order = _clean_category_order(raw.get('order'))
    if not order:
        raise OperationRejected('Pusta kolejność kategorii.')
    # Kategorie spoza przesłanej listy zostają na końcu w dotychczasowej kolejności.
    shop.category_order = order + [name for name in shop.category_order if name not in order]
    shop.save(update_fields=['category_order', 'updated_at'])
    return ''


def _op_list_shop(raw, user, when):
    try:
        list_id = int(raw.get('list'))
    except (TypeError, ValueError):
        raise OperationRejected('Nieprawidłowa lista.') from None
    shopping_list = ShoppingList.objects.select_for_update().filter(pk=list_id).first()
    if shopping_list is None:
        raise OperationSkipped('Lista została usunięta na innym urządzeniu.')
    raw_shop = raw.get('shop')
    if raw_shop in (None, ''):
        shopping_list.shop = None
    else:
        shop = ShopLayout.objects.filter(uuid=_parse_uuid(raw_shop, 'Sklep')).first()
        if shop is None:
            raise OperationSkipped('Sklep został usunięty na innym urządzeniu.')
        shopping_list.shop = shop
    shopping_list.save(update_fields=['shop', 'updated_at'])
    return ''


HANDLERS = {
    OP_ADD: _op_add,
    OP_PANTRY_MOVEMENT: _op_pantry_movement,
    OP_SET_PURCHASED: _op_set_purchased,
    OP_SET_QUANTITY: _op_set_quantity,
    OP_DELETE: _op_delete,
    OP_SHOP_ADD: _op_shop_add,
    OP_SHOP_ORDER: _op_shop_order,
    OP_LIST_SHOP: _op_list_shop,
}


def _result(op_id, status, message='', duplicate=False):
    result = {'op_id': str(op_id) if op_id else '', 'status': status, 'message': message}
    if duplicate:
        result['duplicate'] = True
    return result


def _record(op_id, user, op_type, status, message):
    try:
        with transaction.atomic():
            ShoppingSyncOperation.objects.create(
                op_id=op_id, user=user, op_type=op_type[:40], status=status, message=message[:255],
            )
    except IntegrityError:
        pass


def apply_operation(raw, user):
    if not isinstance(raw, dict):
        return _result(None, STATUS_REJECTED, 'Nieprawidłowa operacja.')
    try:
        op_id = uuid_module.UUID(str(raw.get('op_id')))
    except (TypeError, ValueError, AttributeError):
        return _result(raw.get('op_id'), STATUS_REJECTED, 'Brak identyfikatora operacji.')

    existing = ShoppingSyncOperation.objects.filter(op_id=op_id).first()
    if existing:
        return _result(op_id, existing.status, existing.message, duplicate=True)

    op_type = str(raw.get('type') or '')
    handler = HANDLERS.get(op_type)
    if handler is None:
        _record(op_id, user, op_type or '?', STATUS_REJECTED, 'Nieznany typ operacji.')
        return _result(op_id, STATUS_REJECTED, 'Nieznany typ operacji.')

    when = _operation_time(raw.get('at'))
    try:
        with transaction.atomic():
            message = handler(raw, user, when) or ''
            ShoppingSyncOperation.objects.create(
                op_id=op_id, user=user, op_type=op_type, status=STATUS_APPLIED, message=message[:255],
            )
        return _result(op_id, STATUS_APPLIED, message)
    except OperationSkipped as exc:
        _record(op_id, user, op_type, STATUS_SKIPPED, str(exc))
        return _result(op_id, STATUS_SKIPPED, str(exc))
    except OperationRejected as exc:
        _record(op_id, user, op_type, STATUS_REJECTED, str(exc))
        return _result(op_id, STATUS_REJECTED, str(exc))
    except IntegrityError:
        # Ta sama operacja przyszła równolegle drugim żądaniem.
        existing = ShoppingSyncOperation.objects.filter(op_id=op_id).first()
        if existing:
            return _result(op_id, existing.status, existing.message, duplicate=True)
        _record(op_id, user, op_type, STATUS_REJECTED, 'Konflikt zapisu.')
        return _result(op_id, STATUS_REJECTED, 'Konflikt zapisu.')


def apply_operations(raw_operations, user):
    if not isinstance(raw_operations, list):
        raise ValueError('Oczekiwano listy operacji.')
    if len(raw_operations) > MAX_OPERATIONS_PER_SYNC:
        raise ValueError(f'Za dużo operacji naraz (maksymalnie {MAX_OPERATIONS_PER_SYNC}).')
    return [apply_operation(raw, user) for raw in raw_operations]


def prune_operations(days=90):
    """Usuwa stare wpisy idempotencji - telefon nie ponawia operacji sprzed miesięcy."""
    cutoff = timezone.now() - timedelta(days=days)
    return ShoppingSyncOperation.objects.filter(received_at__lt=cutoff).delete()[0]
