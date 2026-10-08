"""Składniki należą do przepisu, krok jest opcjonalny; jednostki kuchenne.

Dotychczasowe składniki dostają przepis swojego kroku i zostają przy tym
kroku. Kolejność przelicza się na wspólną dla przepisu: krok 1 (jego
składniki po kolei), krok 2, ... - tak jak wyglądały dotąd.
"""
import django.db.models.deletion
from django.db import migrations, models


def fill_recipe(apps, schema_editor):
    Ingredient = apps.get_model('cooking', 'RecipeStepIngredient')
    rows = (
        Ingredient.objects
        .select_related('step')
        .order_by('step__recipe_id', 'step__order', 'step_id', 'order', 'id')
    )
    position = {}
    for row in rows.iterator():
        recipe_id = row.step.recipe_id
        position[recipe_id] = position.get(recipe_id, 0) + 1
        Ingredient.objects.filter(pk=row.pk).update(recipe_id=recipe_id, order=position[recipe_id])


def back_to_steps(apps, schema_editor):
    """Cofnięcie: składnik bez kroku trafia do pierwszego kroku przepisu."""
    Ingredient = apps.get_model('cooking', 'RecipeStepIngredient')
    Step = apps.get_model('cooking', 'RecipeStep')
    for row in Ingredient.objects.filter(step__isnull=True):
        first = Step.objects.filter(recipe_id=row.recipe_id).order_by('order', 'id').first()
        if first is None:
            row.delete()
        else:
            Ingredient.objects.filter(pk=row.pk).update(step=first)


class Migration(migrations.Migration):

    dependencies = [
        ('cooking', '0022_seed_pantry_categories'),
    ]

    operations = [
        migrations.AddField(
            model_name='recipestepingredient',
            name='recipe',
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name='ingredient_items', to='cooking.recipe',
            ),
        ),
        migrations.RunPython(fill_recipe, back_to_steps),
        migrations.AlterField(
            model_name='recipestepingredient',
            name='recipe',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='ingredient_items', to='cooking.recipe',
            ),
        ),
        migrations.AlterField(
            model_name='recipestepingredient',
            name='step',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='ingredients', to='cooking.recipestep',
            ),
        ),
        migrations.AlterField(
            model_name='recipestepingredient',
            name='unit',
            field=models.CharField(
                choices=[
                    ('szt', 'szt.'), ('g', 'g'), ('kg', 'kg'), ('ml', 'ml'), ('l', 'l'), ('opak', 'opak.'),
                    ('lyzka', 'łyżka'), ('lyzeczka', 'łyżeczka'), ('szklanka', 'szklanka'),
                    ('szczypta', 'szczypta'), ('do_smaku', 'do smaku'),
                ],
                default='g', max_length=10,
            ),
        ),
        migrations.AlterModelOptions(
            name='recipestepingredient',
            options={'ordering': ['order', 'id']},
        ),
    ]
