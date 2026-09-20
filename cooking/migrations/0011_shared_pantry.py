"""Wspólna spiżarnia i wspólne listy zakupów dla całego domu.

Kolejność kroków ma znaczenie:
1. usuwamy ograniczenia "unikalne w obrębie użytkownika",
2. pole user staje się created_by - najpierw dostaje db_column='user_id',
   potem zmienia nazwę, więc kolumna w bazie się nie zmienia (dzięki temu
   `preview_shared_pantry` działa na bazie jeszcze przed tą migracją),
3. łączymy duplikaty (cooking/services/pantry_sharing.py).

Ograniczenia "unikalne w całej spiżarni" dodaje osobna migracja 0012. W jednej
transakcji z łączeniem PostgreSQL odmawia: zmiany danych zostawiają odroczone
sprawdzenia kluczy obcych, a przy nich nie wolno utworzyć indeksu na tej samej
tabeli ("cannot CREATE INDEX ... because it has pending trigger events").
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def merge_duplicates(apps, schema_editor):
    from cooking.services.pantry_sharing import apply_pantry_merges, describe_plan, plan_pantry_merges

    PantryProduct = apps.get_model('cooking', 'PantryProduct')
    PantryMovement = apps.get_model('cooking', 'PantryMovement')
    ShoppingListItem = apps.get_model('cooking', 'ShoppingListItem')
    plan = plan_pantry_merges(PantryProduct.objects.select_related('created_by'))
    if not plan:
        return
    print('\n  Łączenie spiżarni w jedną wspólną:')
    for line in describe_plan(plan):
        print(f'    {line}')
    apply_pantry_merges(plan, PantryMovement, ShoppingListItem)


class Migration(migrations.Migration):

    dependencies = [
        ('cooking', '0010_product_catalog_household_and_name_language'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='pantryproduct',
            name='unique_user_pantry_product_name',
        ),
        migrations.RemoveConstraint(
            model_name='pantryproduct',
            name='unique_user_pantry_product_barcode',
        ),
        migrations.AlterField(
            model_name='pantryproduct',
            name='user',
            field=models.ForeignKey(
                blank=True,
                db_column='user_id',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='pantry_products',
                to=settings.AUTH_USER_MODEL,
                verbose_name='Dodane przez',
            ),
        ),
        migrations.RenameField(
            model_name='pantryproduct',
            old_name='user',
            new_name='created_by',
        ),
        migrations.AlterField(
            model_name='shoppinglist',
            name='user',
            field=models.ForeignKey(
                blank=True,
                db_column='user_id',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='shopping_lists',
                to=settings.AUTH_USER_MODEL,
                verbose_name='Utworzona przez',
            ),
        ),
        migrations.RenameField(
            model_name='shoppinglist',
            old_name='user',
            new_name='created_by',
        ),
        migrations.RunPython(merge_duplicates, migrations.RunPython.noop),
    ]
