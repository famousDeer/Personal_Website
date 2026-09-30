import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cooking', '0019_shopping_list_uuid'),
    ]

    operations = [
        migrations.AlterField(
            model_name='shoppinglist',
            name='uuid',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
