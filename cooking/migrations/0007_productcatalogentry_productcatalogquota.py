from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('cooking', '0006_pantryproduct_barcode'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProductCatalogEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source', models.CharField(choices=[('open_food_facts', 'Open Food Facts')], default='open_food_facts', max_length=40)),
                ('lookup_barcode', models.CharField(max_length=64)),
                ('canonical_barcode', models.CharField(blank=True, max_length=64)),
                ('status', models.CharField(choices=[('pending', 'Oczekuje na pobranie'), ('found', 'Znaleziony'), ('not_found', 'Nie znaleziony')], default='pending', max_length=20)),
                ('product_type', models.CharField(blank=True, max_length=20)),
                ('product_name', models.CharField(blank=True, max_length=160)),
                ('brand', models.CharField(blank=True, max_length=160)),
                ('description', models.TextField(blank=True)),
                ('ingredients', models.TextField(blank=True)),
                ('external_category', models.CharField(blank=True, max_length=255)),
                ('suggested_category', models.CharField(blank=True, max_length=120)),
                ('quantity_text', models.CharField(blank=True, max_length=80)),
                ('suggested_quantity_per_scan', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ('suggested_unit', models.CharField(blank=True, choices=[('szt', 'szt.'), ('g', 'g'), ('kg', 'kg'), ('ml', 'ml'), ('l', 'l'), ('opak', 'opak.')], max_length=10)),
                ('image', models.ImageField(blank=True, upload_to='pantry_catalog_images')),
                ('image_source_url', models.URLField(blank=True, max_length=500)),
                ('attribution_url', models.URLField(blank=True, max_length=500)),
                ('source_updated_at', models.DateTimeField(blank=True, null=True)),
                ('fetched_at', models.DateTimeField(blank=True, null=True)),
                ('valid_until', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('refresh_started_at', models.DateTimeField(blank=True, null=True)),
                ('retry_after', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'product_catalog_entries',
                'ordering': ['product_name', 'lookup_barcode'],
                'indexes': [models.Index(fields=['source', 'valid_until'], name='product_catalog_valid_idx')],
                'constraints': [models.UniqueConstraint(fields=('source', 'lookup_barcode'), name='unique_product_catalog_source_barcode')],
            },
        ),
        migrations.CreateModel(
            name='ProductCatalogQuota',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source', models.CharField(max_length=40, unique=True)),
                ('window_started_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('request_count', models.PositiveIntegerField(default=0)),
            ],
            options={
                'db_table': 'product_catalog_quotas',
            },
        ),
    ]
