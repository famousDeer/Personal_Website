from decimal import Decimal, ROUND_CEILING

from django.db import migrations, models
import cooking.storage


def package_count(quantity, quantity_per_scan):
    quantity = Decimal(quantity or 0)
    quantity_per_scan = Decimal(quantity_per_scan or 0)
    if quantity <= 0 or quantity_per_scan <= 0:
        return 0
    return int((quantity / quantity_per_scan).to_integral_value(rounding=ROUND_CEILING))


def populate_package_counts(apps, schema_editor):
    PantryProduct = apps.get_model('cooking', 'PantryProduct')
    PantryMovement = apps.get_model('cooking', 'PantryMovement')

    products = list(PantryProduct.objects.all())
    quantities_per_scan = {}
    for product in products:
        has_reliable_package_data = bool(product.barcode) or product.unit in ('szt', 'opak')
        product.current_package_count = (
            package_count(product.current_quantity, product.quantity_per_scan)
            if has_reliable_package_data
            else 0
        )
        quantities_per_scan[product.id] = product.quantity_per_scan
    if products:
        PantryProduct.objects.bulk_update(products, ['current_package_count'])

    movements = list(PantryMovement.objects.all())
    reliable_product_ids = {
        product.id for product in products
        if product.barcode or product.unit in ('szt', 'opak')
    }
    for movement in movements:
        movement.package_count = (
            package_count(movement.quantity, quantities_per_scan.get(movement.product_id))
            if movement.product_id in reliable_product_ids
            else None
        )
    if movements:
        PantryMovement.objects.bulk_update(movements, ['package_count'])


def clear_package_counts(apps, schema_editor):
    PantryProduct = apps.get_model('cooking', 'PantryProduct')
    PantryMovement = apps.get_model('cooking', 'PantryMovement')
    PantryProduct.objects.update(current_package_count=0)
    PantryMovement.objects.update(package_count=None)


class Migration(migrations.Migration):

    dependencies = [
        ('cooking', '0007_productcatalogentry_productcatalogquota'),
    ]

    operations = [
        migrations.AddField(
            model_name='pantryproduct',
            name='current_package_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='pantryproduct',
            name='image',
            field=models.ImageField(blank=True, storage=cooking.storage.private_media_storage, upload_to='pantry_product_images'),
        ),
        migrations.AddField(
            model_name='pantrymovement',
            name='package_count',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(populate_package_counts, clear_package_counts),
    ]
