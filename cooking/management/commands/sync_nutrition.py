"""Uzupełnia wartości odżywcze z Open Food Facts dla produktów spiżarni.

Wpisy katalogu pobrane, zanim strona liczyła kalorie, nie mają kcal i są
ważne 30 dni, więc same się nie odświeżą. Komenda pobiera je ponownie dla
produktów z kodem kreskowym, które nie mają własnych kcal. Szanuje limit
zapytań - po jego wyczerpaniu kończy i można ją uruchomić jeszcze raz.

    python manage.py sync_nutrition
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from cooking.models import PantryProduct, ProductCatalogEntry
from cooking.services.product_catalog import lookup_open_food_facts


class Command(BaseCommand):
    help = 'Pobiera kcal i makroskładniki z Open Food Facts dla produktów spiżarni z kodem kreskowym.'

    def handle(self, *args, **options):
        barcodes = list(
            PantryProduct.objects.filter(kcal_100__isnull=True).exclude(barcode='')
            .values_list('barcode', flat=True)
        )
        known = set(ProductCatalogEntry.objects.filter(
            source=ProductCatalogEntry.SOURCE_OPEN_FOOD_FACTS,
            lookup_barcode__in=barcodes,
            kcal_100__isnull=False,
        ).values_list('lookup_barcode', flat=True))
        found = checked = 0
        for barcode in barcodes:
            if barcode in known:
                continue
            # Ważny wpis bez kcal: wymuszamy ponowne pobranie.
            ProductCatalogEntry.objects.filter(
                source=ProductCatalogEntry.SOURCE_OPEN_FOOD_FACTS, lookup_barcode=barcode,
            ).update(valid_until=timezone.now())
            result = lookup_open_food_facts(barcode)
            if result.cache_state == 'rate_limited':
                self.stdout.write('Limit zapytań wyczerpany - uruchom komendę ponownie za chwilę.')
                break
            if result.cache_state in ('disabled', 'skipped'):
                continue
            checked += 1
            if result.entry is not None and result.entry.kcal_100 is not None:
                found += 1
        self.stdout.write(f'Sprawdzono {checked}, z kcal: {found}, już znane: {len(known)}.')
