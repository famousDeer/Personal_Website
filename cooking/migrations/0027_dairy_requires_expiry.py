from django.db import migrations


def dairy_requires_expiry(apps, schema_editor):
    """Decyzja domu: obowiązkowy termin przydatności tylko dla nabiału."""
    PantryCategory = apps.get_model('cooking', 'PantryCategory')
    PantryCategory.objects.filter(code='nabial').update(requires_expiry=True)


class Migration(migrations.Migration):

    dependencies = [
        ('cooking', '0026_pantry_batches_expiry'),
    ]

    operations = [
        migrations.RunPython(dairy_requires_expiry, migrations.RunPython.noop),
    ]
