from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cooking', '0005_shoppinglist_shoppinglistitem'),
    ]

    operations = [
        migrations.AddField(
            model_name='pantryproduct',
            name='barcode',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='pantryproduct',
            name='quantity_per_scan',
            field=models.DecimalField(decimal_places=2, default=1, max_digits=10),
        ),
        migrations.AddField(
            model_name='pantrymovement',
            name='scan_id',
            field=models.UUIDField(blank=True, null=True, unique=True),
        ),
        migrations.AddConstraint(
            model_name='pantryproduct',
            constraint=models.UniqueConstraint(
                condition=~models.Q(barcode=''),
                fields=('user', 'barcode'),
                name='unique_user_pantry_product_barcode',
            ),
        ),
        migrations.AddConstraint(
            model_name='pantryproduct',
            constraint=models.CheckConstraint(
                condition=models.Q(quantity_per_scan__gt=0),
                name='positive_pantry_product_quantity_per_scan',
            ),
        ),
    ]
