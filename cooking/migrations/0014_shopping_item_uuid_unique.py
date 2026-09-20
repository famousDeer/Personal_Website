import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cooking', '0013_shopping_list_offline_sync'),
    ]

    operations = [
        migrations.AlterField(
            model_name='shoppinglistitem',
            name='uuid',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
