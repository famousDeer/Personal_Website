"""Jedna pozycja na nazwę i na kod kreskowy w całej wspólnej spiżarni.

Osobno od 0011, bo musi zobaczyć zatwierdzone już łączenie duplikatów -
patrz opis w 0011_shared_pantry.py.
"""
import django.db.models.functions.text
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cooking', '0011_shared_pantry'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='pantryproduct',
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower('name'),
                name='unique_pantry_product_name_ci',
            ),
        ),
        migrations.AddConstraint(
            model_name='pantryproduct',
            constraint=models.UniqueConstraint(
                condition=models.Q(('barcode', ''), _negated=True),
                fields=('barcode',),
                name='unique_pantry_product_barcode',
            ),
        ),
    ]
