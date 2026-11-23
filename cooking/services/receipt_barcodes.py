"""Dokładne dopasowanie kodu na etapach sprawdzania i rozpakowywania paragonu."""
import re

from ..models import PantryProduct


def find_barcode_owner(barcode: str) -> PantryProduct | None:
    owner = PantryProduct.objects.filter(barcode=barcode).first()
    if owner is not None or not re.fullmatch(r'(?:0[0-9]{12}|[0-9]{12})', barcode):
        return owner

    # Niektóre czytniki zwracają UPC-A, inne EAN-13 z dopisanym zerem.
    # Porównujemy wyłącznie te dwie reprezentacje poprawnego kodu.
    upc = barcode[-12:]
    weighted = sum(int(digit) * (3 if index % 2 == 0 else 1) for index, digit in enumerate(upc))
    if weighted % 10:
        return None
    alternate = upc if len(barcode) == 13 else '0' + upc
    return PantryProduct.objects.filter(barcode=alternate).first()
