"""Uczy katalog domowy na podstawie tego, co już jest w spiżarni.

Uruchamiane jednorazowo po wdrożeniu nowych kategorii (i bezpiecznie
w dowolnym momencie później):

    docker compose exec web python manage.py learn_pantry_catalog --dry-run
    docker compose exec web python manage.py learn_pantry_catalog

Kroki:
1. Przelicza podpowiedzi kategorii we wpisach Open Food Facts zapisanych
   lokalnie - bez sieci, na danych z cache.
2. Produktom w "Inne" albo bez kategorii nadaje lepszą kategorię, jeśli
   reguły ją znajdują (z danych katalogu dla produktów z kodem, z nazwy dla
   pozostałych). Kategorii wybranych świadomie nie rusza.
3. Zapamiętuje dla całego domu nazwę, kategorię i opakowanie każdego
   produktu z kodem kreskowym, którego pamięć domu jeszcze nie zna. Jeśli
   kilku domowników ma ten sam kod, wygrywa ostatnio zmieniany produkt.
   Istniejących wpisów pamięci nie nadpisuje.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from cooking.constants import PANTRY_CATEGORY_OTHER
from cooking.models import PantryProduct, ProductCatalogEntry
from cooking.services.product_catalog import (
    category_suggestion_for_entry,
    remember_household_product,
    suggest_category_from_name,
)


class Command(BaseCommand):
    help = 'Uczy katalog domowy na podstawie spiżarni i poprawia kategorie produktów w "Inne".'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Pokaż, co by się zmieniło, niczego nie zapisując.',
        )

    def handle(self, *args, dry_run=False, **options):
        with transaction.atomic():
            refreshed = self._refresh_catalog_suggestions()
            recategorized = self._recategorize_products()
            remembered = self._remember_products()
            if dry_run:
                transaction.set_rollback(True)

        prefix = '[próba] ' if dry_run else ''
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}Podpowiedzi w katalogu Open Food Facts: {refreshed} zmienionych'
        ))
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}Produkty przeniesione z "Inne": {recategorized}'
        ))
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}Kody zapamiętane dla całego domu: {remembered}'
        ))
        if dry_run:
            self.stdout.write('Nic nie zostało zapisane. Uruchom bez --dry-run, aby zastosować zmiany.')

    def _refresh_catalog_suggestions(self):
        changed = 0
        entries = ProductCatalogEntry.objects.filter(
            source=ProductCatalogEntry.SOURCE_OPEN_FOOD_FACTS,
            status=ProductCatalogEntry.STATUS_FOUND,
        )
        for entry in entries.iterator():
            category = category_suggestion_for_entry(entry)
            if category and category != entry.suggested_category:
                ProductCatalogEntry.objects.filter(pk=entry.pk).update(suggested_category=category)
                changed += 1
        return changed

    def _recategorize_products(self):
        catalog = {
            entry.lookup_barcode: entry
            for entry in ProductCatalogEntry.objects.filter(
                source__in=[
                    ProductCatalogEntry.SOURCE_HOUSEHOLD,
                    ProductCatalogEntry.SOURCE_OPEN_FOOD_FACTS,
                ],
                status=ProductCatalogEntry.STATUS_FOUND,
            # W słowniku wygrywa ostatni wpis dla danego kodu. "open_food_facts"
            # sortuje się malejąco przed "household", więc pamięć domu nadpisuje
            # Open Food Facts - tak jak przy skanowaniu.
            ).order_by('-source')
        }
        changed = 0
        products = PantryProduct.objects.filter(
            category__in=['', PANTRY_CATEGORY_OTHER],
        ).select_related('user').order_by('user__username', 'name')
        for product in products:
            suggestion = ''
            entry = catalog.get(product.barcode) if product.barcode else None
            if entry:
                suggestion = category_suggestion_for_entry(entry)
            if not suggestion or suggestion == PANTRY_CATEGORY_OTHER:
                suggestion = suggest_category_from_name(product.name)
            if not suggestion or suggestion == PANTRY_CATEGORY_OTHER:
                continue
            self.stdout.write(
                f'  {product.user.username}: "{product.name}"  '
                f'{product.category or "bez kategorii"} -> {suggestion}'
            )
            PantryProduct.objects.filter(pk=product.pk).update(category=suggestion)
            changed += 1
        return changed

    def _remember_products(self):
        known = set(
            ProductCatalogEntry.objects.filter(
                source=ProductCatalogEntry.SOURCE_HOUSEHOLD,
            ).values_list('lookup_barcode', flat=True)
        )
        latest_by_barcode = {}
        products = PantryProduct.objects.exclude(barcode='').order_by('updated_at', 'pk')
        for product in products:
            latest_by_barcode[product.barcode] = product  # późniejszy nadpisuje
        remembered = 0
        for barcode, product in sorted(latest_by_barcode.items()):
            if barcode in known:
                continue
            remember_household_product(
                barcode,
                name=product.name,
                category=product.category,
                unit=product.unit,
                quantity_per_scan=product.quantity_per_scan,
            )
            remembered += 1
        return remembered
