from django.db import migrations, models
import django.core.validators
from decimal import Decimal
from django.conf import settings


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0002_alter_monthly_date'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='daily',
            name='cost',
            field=models.DecimalField(
                decimal_places=2,
                max_digits=12,
                validators=[django.core.validators.MinValueValidator(Decimal('0.01'))],
            ),
        ),
        migrations.AlterField(
            model_name='income',
            name='amount',
            field=models.DecimalField(
                decimal_places=2,
                max_digits=12,
                validators=[django.core.validators.MinValueValidator(Decimal('0.01'))],
            ),
        ),
        migrations.AlterField(
            model_name='monthly',
            name='total_expense',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('0.00'),
                max_digits=15,
            ),
        ),
        migrations.AlterField(
            model_name='monthly',
            name='total_income',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('0.00'),
                max_digits=15,
            ),
        ),

        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AddIndex(
                    model_name='monthly',
                    index=models.Index(
                        fields=['user', 'date'],
                        name='monthly_rec_user_id_89bb82_idx',
                    ),
                ),
                migrations.AddConstraint(
                    model_name='monthly',
                    constraint=models.UniqueConstraint(
                        fields=('user', 'date'),
                        name='unique_user_month',
                    ),
                ),
            ],
        ),

    ]
