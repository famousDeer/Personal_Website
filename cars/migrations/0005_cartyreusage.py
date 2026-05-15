from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('cars', '0004_carservice_workshop_name_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='CarTyreUsage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('mounted_date', models.DateField()),
                ('mounted_odometer', models.PositiveIntegerField()),
                ('removed_date', models.DateField(blank=True, null=True)),
                ('removed_odometer', models.PositiveIntegerField(blank=True, null=True)),
                ('tyre', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='usage_periods', to='cars.cartyres')),
            ],
            options={
                'verbose_name': 'Car Tyre Usage',
                'verbose_name_plural': 'Car Tyre Usages',
                'db_table': 'car_tyre_usage',
                'ordering': ['-mounted_date', '-mounted_odometer'],
            },
        ),
    ]
