from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cars', '0005_cartyreusage'),
    ]

    operations = [
        migrations.AlterField(
            model_name='cartyres',
            name='odometer',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterModelOptions(
            name='cartyres',
            options={
                'ordering': ['-purchase_date', '-id'],
                'verbose_name': 'Car Tyre',
                'verbose_name_plural': 'Car Tyres',
            },
        ),
    ]
