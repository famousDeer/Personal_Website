"""Produkt spiżarni po nazwie i firmie (marce).

W domu może stać „Masło” Łaciate i „Masło” Mlekovita - to dwa produkty
o tej samej nazwie. Tekstem jednoznacznym jest etykieta „Masło (Łaciate)”:
pod nią produkt trafia do podpowiedzi, na listę zakupów i do paragonów.
Sama nazwa „Masło” dalej coś znaczy (przepis, stara pozycja listy), więc
wtedy wybieramy jeden z produktów: bez firmy, potem ten, którego jest najwięcej.
"""
from __future__ import annotations

import re

from ..models import PantryProduct
from .receipt_parser import normalize_text

LABEL_RE = re.compile(r'^(?P<name>.+?)\s*\((?P<brand>[^()]+)\)\s*$')


def product_label(name: str, brand: str = '') -> str:
    name, brand = (name or '').strip(), (brand or '').strip()
    return f'{name} ({brand})' if brand else name


def split_label(text: str) -> tuple[str, str] | None:
    """„Masło (Łaciate)” → („Masło”, „Łaciate”); bez nawiasu - None."""
    match = LABEL_RE.match((text or '').strip())
    if not match:
        return None
    return match.group('name').strip(), match.group('brand').strip()


def same_name(first: str, second: str) -> bool:
    """„Masło Extra” = „maslo  extra” – bez wielkości liter, ogonków i interpunkcji."""
    first, second = normalize_text(first or ''), normalize_text(second or '')
    return bool(first) and first == second


def same_product(first: PantryProduct, second: PantryProduct) -> bool:
    """Ta sama nazwa i firma. Produkt bez firmy pasuje do każdej firmy -
    np. pozycja z paragonu („Masło”) i produkt z kodem („Masło”, Łaciate)."""
    if not same_name(first.name, second.name):
        return False
    return not first.brand or not second.brand or same_name(first.brand, second.brand)


def _pick(candidates: list[PantryProduct]) -> PantryProduct | None:
    """Kilka firm pod jedną nazwą: bez firmy, potem ten, którego jest najwięcej."""
    if not candidates:
        return None
    return min(candidates, key=lambda product: (bool(product.brand), -product.current_quantity, product.pk))


def find_product_by_name(name: str, brand: str | None = None) -> PantryProduct | None:
    """Produkt po nazwie (i firmie). Najpierw dokładnie (bez wielkości liter),
    potem po normalizacji, żeby „Maslo” trafiło w „Masło”.

    brand=None: firma nieznana - tekst może być etykietą „Masło (Łaciate)”
    albo samą nazwą. brand='' albo tekst: tylko produkt z tą firmą.
    """
    name = (name or '').strip()
    if not name:
        return None
    if brand is not None:
        product = PantryProduct.objects.filter(name__iexact=name, brand__iexact=brand.strip()).first()
        if product is not None:
            return product
        key = (normalize_text(name), normalize_text(brand))
        for candidate in PantryProduct.objects.only('id', 'name', 'brand'):
            if (normalize_text(candidate.name), normalize_text(candidate.brand)) == key:
                return PantryProduct.objects.get(pk=candidate.pk)
        return None

    parts = split_label(name)
    if parts is not None:
        product = find_product_by_name(*parts)
        if product is not None:
            return product
    product = _pick(list(PantryProduct.objects.filter(name__iexact=name)))
    if product is not None:
        return product
    key = normalize_text(name)
    if not key:
        return None
    matches = [
        candidate.pk
        for candidate in PantryProduct.objects.only('id', 'name', 'brand')
        if normalize_text(candidate.name) == key or normalize_text(candidate.label) == key
    ]
    return _pick(list(PantryProduct.objects.filter(pk__in=matches)))


def name_taken(name: str, brand: str, exclude_pk: int | None = None) -> bool:
    """Czy w spiżarni jest już produkt o tej nazwie i firmie (bez wielkości liter)."""
    query = PantryProduct.objects.filter(name__iexact=name.strip(), brand__iexact=(brand or '').strip())
    if exclude_pk:
        query = query.exclude(pk=exclude_pk)
    return query.exists()
