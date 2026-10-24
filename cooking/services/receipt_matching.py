"""Pozycja paragonu → produkt w spiżarni.

1. Słownik domu (ReceiptAlias): dokładnie ta sama linia z tego sklepu,
   potem z dowolnego sklepu, potem bardzo podobna (literówka OCR).
2. Propozycja, gdy linii jeszcze nie znamy. Liczy się:
   - podobieństwo nazwy (skróty: „JOG NAT PILOS” ~ „Jogurt naturalny Pilos”),
   - zakup tego produktu tego samego dnia (skan „Dodaj”, „Kupiono” na liście),
   - pozycja na aktywnej liście zakupów,
   - zgodna gramatura (400G ~ opakowanie 400 g).
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from django.db.models import Q

from ..models import (
    PantryMovement, PantryProduct, ProductGroup, ReceiptAlias, ShoppingList, ShoppingListItem,
)
from .receipt_parser import name_words, normalize_text, package_size

ALIAS_FUZZY_RATIO = 0.9
SUGGESTION_MIN_SCORE = 0.55


@dataclass
class Candidate:
    product: PantryProduct | None = None
    group: ProductGroup | None = None
    score: float = 0.0
    reason: str = ''

    @property
    def label(self):
        return (self.group or self.product).label


def find_alias(store, normalized: str):
    aliases = ReceiptAlias.objects.select_related('product', 'group')
    if store is not None:
        alias = aliases.filter(store=store, normalized_text=normalized).first()
        if alias:
            return alias
    alias = aliases.filter(store__isnull=True, normalized_text=normalized).first()
    if alias or not normalized:
        return alias
    pool = aliases.filter(Q(store=store) | Q(store__isnull=True)) if store else aliases.filter(store__isnull=True)
    best, best_ratio = None, 0.0
    for candidate in pool.filter(normalized_text__startswith=normalized[:2])[:500]:
        ratio = SequenceMatcher(None, normalized, candidate.normalized_text).ratio()
        if ratio > best_ratio:
            best, best_ratio = candidate, ratio
    return best if best_ratio >= ALIAS_FUZZY_RATIO else None


def _abbrev_score(line_words: list[str], target_words: list[str]) -> float:
    """Ile słów z paragonu to początki słów nazwy produktu („JOG” → „JOGURT”)."""
    if not line_words or not target_words:
        return 0.0
    used = set()
    hits = 0.0
    for word in line_words:
        for index, target in enumerate(target_words):
            if index in used:
                continue
            if target == word or (len(word) >= 3 and target.startswith(word)) or (
                len(word) >= 4 and SequenceMatcher(None, word, target).ratio() >= 0.8
            ):
                used.add(index)
                hits += 1
                break
    coverage_line = hits / len(line_words)
    coverage_target = hits / len(target_words)
    return 0.7 * coverage_line + 0.3 * coverage_target


def name_score(line_name: str, target_name: str) -> float:
    line_words, target_words = name_words(line_name), name_words(target_name)
    ratio = SequenceMatcher(None, ' '.join(line_words), ' '.join(target_words)).ratio()
    return max(ratio * 0.9, _abbrev_score(line_words, target_words))


def _size_matches(line_name: str, product: PantryProduct) -> bool:
    size = package_size(line_name)
    if not size or not product.quantity_per_scan:
        return False
    amount, base, _count = size
    factors = {PantryProduct.UNIT_GRAM: 1000, PantryProduct.UNIT_KILOGRAM: 1,
               PantryProduct.UNIT_MILLILITER: 1000, PantryProduct.UNIT_LITER: 1}
    expected_base = 'kg' if product.unit in (PantryProduct.UNIT_GRAM, PantryProduct.UNIT_KILOGRAM) else 'l'
    if product.unit not in factors or base != expected_base:
        return False
    return abs(amount * factors[product.unit] - product.quantity_per_scan) < 1


class MatchContext:
    """Wspólne dane dla wszystkich linii jednego paragonu (jedno zapytanie na typ)."""

    def __init__(self, purchased_on):
        self.products = list(PantryProduct.objects.select_related('group'))
        self.groups = list(ProductGroup.objects.all())
        linked = PantryMovement.objects.filter(receipt_lines__isnull=False).values('pk')
        self.bought_today = set(
            PantryMovement.objects.filter(
                movement_type=PantryMovement.PURCHASE, occurred_on=purchased_on,
            ).exclude(pk__in=linked).values_list('product_id', flat=True)
        )
        items = ShoppingListItem.objects.filter(shopping_list__status=ShoppingList.ACTIVE)
        self.listed_products = set(items.exclude(pantry_product=None).values_list('pantry_product_id', flat=True))
        self.listed_groups = set(items.exclude(pantry_group=None).values_list('pantry_group_id', flat=True))


def suggest(line_name: str, context: MatchContext) -> Candidate | None:
    best = None
    for product in context.products:
        # Paragon często podaje firmę („MASLO LACIATE”) - liczy się lepsza ocena.
        score = name_score(line_name, product.name)
        if product.brand:
            score = max(score, name_score(line_name, product.label))
        if score < 0.3:
            continue
        reasons = []
        if product.pk in context.bought_today:
            score += 0.25
            reasons.append('dodane dziś')
        if product.pk in context.listed_products or product.group_id in context.listed_groups:
            score += 0.15
            reasons.append('na liście zakupów')
        if _size_matches(line_name, product):
            score += 0.1
            reasons.append('ta sama gramatura')
        if best is None or score > best.score:
            best = Candidate(product=product, score=score, reason=', '.join(reasons))
    for group in context.groups:
        score = name_score(line_name, group.name)
        if group.pk in context.listed_groups:
            score += 0.15
        if best is None or score > best.score + 0.05:
            best = Candidate(group=group, score=score, reason='grupa produktów')
    if best is None or best.score < SUGGESTION_MIN_SCORE:
        return None
    return best


def normalized_for_alias(name: str) -> str:
    return normalize_text(name)
