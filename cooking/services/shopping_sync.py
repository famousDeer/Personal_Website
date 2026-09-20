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
    ShoppingList,
    ShoppingListItem,
    ShoppingSyncOperation,
)
from .pantry_quantities import (
    convert_pantry_quantity,
    estimated_package_count,
    find_pantry_product,
    parse_pantry_decimal,
    sync_package_count_from_quantity,
    tracks_packages,
    validate_pantry_quantity_for_unit,
    validate_pantry_storage_quantity,
)

OP_ADD = 'item.add'
OP_SET_PURCHASED = 'item.set_purchased'
OP_SET_QUANTITY = 'item.set_quantity'
OP_DELETE = 'item.delete'
OP_TYPES = (OP_ADD, OP_SET_PURCHASED, OP_SET_QUANTITY, OP_DELETE)

STATUS_APPLIED = 'applied'
STATUS_SKIPPED = 'skipped'
STATUS_REJECTED = 'rejected'

MAX_OPERATIONS_PER_SYNC = 500
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
    """Ilość pozycji w jednostce produktu; ValueError, gdy się nie da."""
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


def shopping_snapshot():
    lists = ShoppingList.objects.filter(status=ShoppingList.ACTIVE).select_related(
        'created_by',
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
                'items': [item_json(item) for item in shopping_list.items.all()],
            }
            for shopping_list in lists
        ],
        # Podpowiedzi przy dopisywaniu offline: jednostka, kategoria
        # i wielkość opakowania produktów ze spiżarni.
        'products': [
            {'name': name, 'unit': unit, 'category': category, 'package': format(package, '.2f')}
            for name, unit, category, package in PantryProduct.objects.order_by('name').values_list(
                'name', 'unit', 'category', 'quantity_per_scan',
            )
        ],
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


HANDLERS = {
    OP_ADD: _op_add,
    OP_SET_PURCHASED: _op_set_purchased,
    OP_SET_QUANTITY: _op_set_quantity,
    OP_DELETE: _op_delete,
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
