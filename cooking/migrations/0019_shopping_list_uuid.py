"""UUID listy zakupów - tryb zakupów tworzy listy bez połączenia z serwerem.

Jak przy pozycjach (0013/0014): najpierw pole z NULL i osobne wartości dla
istniejących list, unikalność i NOT NULL w 0020. Postgres nie pozwala
zmienić tabeli w tej samej transakcji co wypełnienie danych.
"""
import uuid

from django.db import migrations, models


def fill_list_uuids(apps, schema_editor):
    ShoppingList = apps.get_model('cooking', 'ShoppingList')
    lists = list(ShoppingList.objects.filter(uuid__isnull=True).only('pk'))
    for shopping_list in lists:
        shopping_list.uuid = uuid.uuid4()
    ShoppingList.objects.bulk_update(lists, ['uuid'], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('cooking', '0018_pantry_product_one_off'),
    ]

    operations = [
        migrations.AddField(
            model_name='shoppinglist',
            name='uuid',
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.RunPython(fill_list_uuids, migrations.RunPython.noop),
    ]
