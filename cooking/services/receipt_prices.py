"""Ceny produktów z zatwierdzonych paragonów.

Porównujemy **cenę regularną** (półkową) za kg / l / szt. - kupon czy
„taniej za 2” nie zmienia ceny w sklepie, więc nie udaje podwyżki ani
obniżki. Cenę po rabacie pokazujemy obok, jako informację.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from ..models import PantryProduct, Receipt, ReceiptLine

YEAR = timedelta(days=365)


@dataclass
class PriceObservation:
    day: date
    store: str
    regular: Decimal
    paid: Decimal | None
    base_unit: str
    discount_kind: str
    discount_label: str
    receipt_uuid: str
    line_name: str

    @property
    def has_discount(self):
        return bool(self.discount_kind)


@dataclass
class PriceSummary:
    observations: list = field(default_factory=list)   # od najnowszej

    @property
    def last(self) -> PriceObservation | None:
        return self.observations[0] if self.observations else None

    def _previous_regular(self):
        """Ostatnia wcześniejsza cena w tej samej jednostce, z innego dnia."""
        last = self.last
        for item in self.observations[1:]:
            if item.base_unit == last.base_unit and item.day != last.day:
                return item
        return None

    @property
    def previous(self):
        return self._previous_regular() if self.last else None

    @property
    def change(self) -> Decimal | None:
        """Zmiana ceny regularnej względem poprzedniego zakupu, w procentach."""
        previous = self.previous
        if not previous or not previous.regular:
            return None
        return ((self.last.regular - previous.regular) / previous.regular * 100).quantize(Decimal('0.1'))

    @property
    def year_ago(self) -> PriceObservation | None:
        last = self.last
        if not last:
            return None
        limit = last.day - YEAR + timedelta(days=45)
        older = [item for item in self.observations if item.base_unit == last.base_unit and item.day <= limit]
        return older[0] if older else None

    @property
    def year_change(self) -> Decimal | None:
        old = self.year_ago
        if not old or not old.regular:
            return None
        return ((self.last.regular - old.regular) / old.regular * 100).quantize(Decimal('0.1'))

    def _recent(self):
        last = self.last
        if not last:
            return []
        limit = timezone.localdate() - YEAR
        return [item for item in self.observations if item.base_unit == last.base_unit and item.day >= limit]

    @property
    def minimum(self):
        recent = self._recent()
        return min(recent, key=lambda item: item.regular) if recent else None

    @property
    def maximum(self):
        recent = self._recent()
        return max(recent, key=lambda item: item.regular) if recent else None

    @property
    def cheapest_store(self) -> str:
        """Sklep z najniższą ostatnią ceną regularną (gdy kupujecie w kilku)."""
        by_store = {}
        for item in self.observations:
            if item.base_unit == self.last.base_unit:
                by_store.setdefault(item.store, item)
        if len(by_store) < 2:
            return ''
        return min(by_store.values(), key=lambda item: item.regular).store

    @property
    def trend(self) -> str:
        change = self.change
        if change is None or abs(change) < Decimal('1'):
            return 'same'
        return 'up' if change > 0 else 'down'


def _lines(product_ids=None):
    lines = (
        ReceiptLine.objects
        .filter(receipt__status=Receipt.STATUS_DONE)
        .filter(Q(match_status=ReceiptLine.MATCH_AUTO) | Q(match_status=ReceiptLine.MATCH_CONFIRMED))
        .exclude(product=None)
        .exclude(price_per_base_unit=None)
        .select_related('receipt__store')
    )
    if product_ids is not None:
        lines = lines.filter(product_id__in=product_ids)
    return lines


def _observation(line) -> PriceObservation:
    receipt = line.receipt
    return PriceObservation(
        day=receipt.purchased_on,
        store=receipt.store.name if receipt.store_id else 'Sklep',
        regular=line.price_per_base_unit,
        paid=line.paid_per_base_unit,
        base_unit=line.base_unit,
        discount_kind=line.get_discount_kind_display() if line.discount_kind else '',
        discount_label=line.discount_label,
        receipt_uuid=str(receipt.uuid),
        line_name=line.name,
    )


def price_summaries(product_ids=None) -> dict[int, PriceSummary]:
    """{product_id: PriceSummary} - jednym zapytaniem dla całej spiżarni."""
    rows = sorted(
        _lines(product_ids),
        key=lambda line: (line.receipt.purchased_at or line.receipt.created_at, line.receipt_id, line.position),
        reverse=True,
    )
    summaries: dict[int, PriceSummary] = {}
    for line in rows:
        summaries.setdefault(line.product_id, PriceSummary()).observations.append(_observation(line))
    return summaries


def price_summary(product: PantryProduct) -> PriceSummary:
    return price_summaries([product.pk]).get(product.pk, PriceSummary())


def chart_data(summary: PriceSummary) -> dict:
    """Seria na sklep (kolor = sklep), cena regularna; promocje osobno w podpowiedzi."""
    last = summary.last
    if not last:
        return {}
    points = [item for item in reversed(summary.observations) if item.base_unit == last.base_unit]
    stores = sorted({item.store for item in points})
    return {
        'unit': last.base_unit,
        'labels': sorted({item.day.isoformat() for item in points}),
        'series': [
            {
                'store': store,
                'points': [
                    {
                        'day': item.day.isoformat(),
                        'regular': float(item.regular),
                        'paid': float(item.paid) if item.paid is not None else None,
                        'discount': item.discount_kind,
                    }
                    for item in points if item.store == store
                ],
            }
            for store in stores
        ],
    }


def barcode_link_candidates(hint_name: str = '', limit: int = 6):
    """Produkty bez kodu kreskowego, które najpewniej właśnie zeskanowano:
    najpierw kupione niedawno z paragonu, a przy znanej nazwie z katalogu
    (Open Food Facts) - najbardziej podobne."""
    from .receipt_matching import name_score

    since = timezone.now() - timedelta(days=60)
    products = list(PantryProduct.objects.filter(barcode='').order_by('-updated_at')[:300])
    if not products:
        return []
    recent = {}
    for line in (
        ReceiptLine.objects.filter(product__in=products, receipt__status=Receipt.STATUS_DONE,
                                   receipt__created_at__gte=since)
        .select_related('receipt__store').order_by('-receipt__created_at')
    ):
        recent.setdefault(line.product_id, line)

    def score(product):
        similarity = name_score(hint_name, product.name) if hint_name else 0.0
        return (similarity >= 0.5, product.pk in recent, similarity, product.updated_at)

    ranked = sorted(products, key=score, reverse=True)
    result = []
    for product in ranked[:limit]:
        line = recent.get(product.pk)
        hint = ''
        if line:
            store = line.receipt.store.name if line.receipt.store_id else 'paragon'
            hint = f'{store}, {line.receipt.purchased_on:%d.%m}'
        result.append({'id': product.pk, 'name': product.name, 'category': product.category, 'hint': hint})
    return result
