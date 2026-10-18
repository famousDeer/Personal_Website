"""Terminy przydatności: partie produktu (PantryBatch).

Zasady (decyzje domu, 17-18.10.2026):
- termin jest obowiązkowy tylko w kategoriach z ``requires_expiry`` (nabiał),
  wpisuje się go przy wkładaniu do szafki - bez szacowania;
- nie pytamy „zużyte czy wyrzucone” - zużycie zdejmuje po prostu partię
  z najkrótszym terminem.

Stan produktu liczy ``current_quantity`` (wszystkie dotychczasowe ścieżki:
skaner, lista zakupów, gotowanie, ręczne +/-). Partie się do niego dopasowują:
``reconcile`` przycina je od najkrótszego terminu, gdy stan spadł. Dzięki temu
żadna istniejąca ścieżka zużycia nie musi wiedzieć o partiach.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import PantryBatch, PantryCategory, PantryProduct

EAT_FIRST_DAYS = 3
QUICK_OFFSETS = (3, 7, 10, 14, 21)
MAX_DAYS_AHEAD = 3 * 365


def expiry_category_names() -> set[str]:
    return set(PantryCategory.objects.filter(requires_expiry=True).values_list('name', flat=True))


def requires_expiry(product: PantryProduct, names: set[str] | None = None) -> bool:
    names = expiry_category_names() if names is None else names
    return bool(product.category) and product.category in names


def parse_expiry(value) -> date | None:
    """Data z formularza (RRRR-MM-DD). Pusta → None; przeszłość dalej niż wczoraj
    i daty absurdalnie odległe są błędem."""
    value = str(value or '').strip()
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('Podaj poprawną datę przydatności.') from exc
    today = timezone.localdate()
    if parsed < today - timedelta(days=1):
        raise ValueError('Ten termin już minął - sprawdź datę.')
    if parsed > today + timedelta(days=MAX_DAYS_AHEAD):
        raise ValueError('Termin jest zbyt odległy - sprawdź datę.')
    return parsed


def dated_quantity(product: PantryProduct) -> Decimal:
    return product.batches.aggregate(total=Sum('quantity'))['total'] or Decimal('0')


def undated_quantity(product: PantryProduct) -> Decimal:
    return max(product.current_quantity - dated_quantity(product), Decimal('0'))


def reconcile(product: PantryProduct) -> None:
    """Przycina partie do stanu produktu, zaczynając od najkrótszego terminu."""
    batches = list(product.batches.order_by('expires_on', 'created_at'))
    excess = sum((batch.quantity for batch in batches), Decimal('0')) - max(product.current_quantity, Decimal('0'))
    for batch in batches:
        if excess <= 0:
            break
        take = min(batch.quantity, excess)
        excess -= take
        if take >= batch.quantity:
            batch.delete()
        else:
            batch.quantity -= take
            batch.save(update_fields=['quantity'])


@transaction.atomic
def add_batch(product: PantryProduct, quantity: Decimal, expires_on: date, purchased_on: date | None = None):
    """Nowa partia po dodaniu do stanu (skaner „Dodano”, rozpakowanie zakupów)."""
    if quantity <= 0 or expires_on is None:
        return None
    reconcile(product)
    quantity = min(quantity, undated_quantity(product)) or quantity
    batch = PantryBatch.objects.create(
        product=product, quantity=quantity, expires_on=expires_on,
        purchased_on=purchased_on or timezone.localdate(),
    )
    reconcile(product)
    return batch


@transaction.atomic
def date_undated_stock(product: PantryProduct, expires_on: date):
    """Termin dla całej ilości bez daty („Uzupełnij termin”)."""
    product = PantryProduct.objects.select_for_update().get(pk=product.pk)
    reconcile(product)
    quantity = undated_quantity(product)
    if quantity <= 0:
        return None
    return PantryBatch.objects.create(product=product, quantity=quantity, expires_on=expires_on)


@dataclass
class ExpiryOverview:
    eat_first: list          # partie z terminem do EAT_FIRST_DAYS dni (i przeterminowane)
    missing: list            # produkty z obowiązkowym terminem, które mają ilość bez daty


def expiry_overview(products=None) -> ExpiryOverview:
    names = expiry_category_names()
    products = list(products if products is not None else PantryProduct.objects.all())
    with_batches = set(PantryBatch.objects.filter(product__in=products).values_list('product_id', flat=True))
    for product in products:
        if product.pk in with_batches:
            reconcile(product)
    limit = timezone.localdate() + timedelta(days=EAT_FIRST_DAYS)
    eat_first = list(
        PantryBatch.objects.filter(product__in=products, expires_on__lte=limit)
        .select_related('product').order_by('expires_on', 'product__name')
    )
    missing = []
    for product in products:
        if product.current_quantity > 0 and requires_expiry(product, names) and undated_quantity(product) > 0:
            missing.append(product)
    return ExpiryOverview(eat_first=eat_first, missing=missing)


def nearest_expiry(product: PantryProduct):
    batch = product.batches.order_by('expires_on').first()
    return batch.expires_on if batch else None


def quick_dates(today: date | None = None):
    """Gotowe przyciski „ważne do”: pt 24.10 itd. - jedno dotknięcie zamiast kalendarza."""
    today = today or timezone.localdate()
    return [today + timedelta(days=offset) for offset in QUICK_OFFSETS]
