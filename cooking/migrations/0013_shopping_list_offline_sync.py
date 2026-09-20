"""Pola do synchronizacji listy zakupów z telefonem (tryb offline).

UUID pozycji dodajemy w dwóch krokach: tutaj jako pole dopuszczające NULL
i wypełniamy istniejące pozycje osobnymi wartościami, a unikalność
i NOT NULL ustawia migracja 0014. AddField z default=uuid4 dałby wszystkim
istniejącym wierszom ten sam UUID, a Postgres nie pozwala zmienić tabeli
w tej samej transakcji co wypełnienie danych.
"""
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def fill_item_uuids(apps, schema_editor):
    Item = apps.get_model('cooking', 'ShoppingListItem')
    items = list(Item.objects.filter(uuid__isnull=True).only('pk'))
    for item in items:
        item.uuid = uuid.uuid4()
    Item.objects.bulk_update(items, ['uuid'], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('cooking', '0012_shared_pantry_constraints'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='shoppinglistitem',
            name='uuid',
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.AddField(
            model_name='shoppinglistitem',
            name='purchased_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='shoppinglistitem',
            name='purchased_by',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='Kupione przez',
            ),
        ),
        migrations.AddField(
            model_name='shoppinglistitem',
            name='pantry_movement',
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='shopping_item', to='cooking.pantrymovement',
            ),
        ),
        migrations.AddField(
            model_name='shoppinglistitem',
            name='purchased_changed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='shoppinglistitem',
            name='quantity_changed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='ShoppingSyncOperation',
            fields=[
                ('op_id', models.UUIDField(primary_key=True, serialize=False)),
                ('op_type', models.CharField(max_length=40)),
                ('status', models.CharField(max_length=20)),
                ('message', models.CharField(blank=True, max_length=255)),
                ('received_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('user', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+', to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'db_table': 'shopping_sync_operations',
                'ordering': ['-received_at'],
            },
        ),
        migrations.RunPython(fill_item_uuids, migrations.RunPython.noop),
    ]
