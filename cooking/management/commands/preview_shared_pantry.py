"""Pokazuje, które produkty zostaną połączone przy przejściu na wspólną spiżarnię.

Niczego nie zmienia. Działa także PRZED migracją 0011, bo kolumny w bazie
są te same - dlatego na Raspberry Pi kolejność jest:

    docker compose build web
    docker compose run --rm web python manage.py preview_shared_pantry
    docker compose up web caddy -d        # migracja połączy dokładnie to, co pokazano
"""
from django.core.management.base import BaseCommand

from cooking.models import PantryProduct
from cooking.services.pantry_sharing import describe_plan, plan_pantry_merges


class Command(BaseCommand):
    help = 'Podgląd łączenia duplikatów przy przejściu na wspólną spiżarnię (bez zmian w bazie).'

    def handle(self, *args, **options):
        # Podgląd działa przed migracją 0011, więc wczytujemy tylko kolumny,
        # które wtedy już istniały - nowsze pola (np. grupa produktów) pominięte.
        products = list(
            PantryProduct.objects
            .select_related('created_by')
            .only(
                'name', 'barcode', 'category', 'unit', 'quantity_per_scan',
                'current_quantity', 'current_package_count', 'minimum_quantity',
                'restock_lead_days', 'notes', 'image', 'created_by',
            )
        )
        plan = plan_pantry_merges(products)
        owners = sorted({getattr(p.created_by, 'username', '?') for p in products})
        self.stdout.write(f'Produkty w bazie: {len(products)} (użytkownicy: {", ".join(owners) or "brak"})')
        if not plan:
            self.stdout.write(self.style.SUCCESS('Brak duplikatów - produkty po prostu staną się wspólne.'))
            return
        merged = sum(len(group.merged) for group in plan)
        renamed = sum(len(group.renamed) for group in plan)
        self.stdout.write('')
        for line in describe_plan(plan):
            self.stdout.write(line)
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Do połączenia: {merged} | ze zmienioną nazwą: {renamed} | '
            f'po migracji produktów: {len(products) - merged}'
        ))
        self.stdout.write('Nic nie zostało zmienione.')
