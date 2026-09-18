from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cooking', '0008_pantry_product_packages_and_images'),
    ]

    operations = [
        migrations.AddField(
            model_name='pantrymovement',
            name='requested_package_count',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='pantrymovement',
            name='requested_quantity',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='pantrymovement',
            name='stock_was_insufficient',
            field=models.BooleanField(default=False),
        ),
    ]
