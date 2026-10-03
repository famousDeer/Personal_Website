"""Wypełnia kategorie i reguły stanem sprzed edycji w aplikacji.

Dane pochodzą z cooking.category_defaults - tych samych list, które do tej
pory były zapisane w kodzie, więc po migracji automatyczny wybór kategorii
działa dokładnie tak jak wcześniej.
"""
from django.db import migrations


def seed(apps, schema_editor):
    from cooking import category_defaults as defaults

    Category = apps.get_model('cooking', 'PantryCategory')
    Rule = apps.get_model('cooking', 'PantryCategoryRule')
    if Category.objects.exists():
        return

    by_name = {}
    positions = {}
    for code, name, group in defaults.DEFAULT_CATEGORIES:
        positions[group] = positions.get(group, -1) + 1
        by_name[name] = Category.objects.create(
            code=code, name=name, group=group, position=positions[group],
        )

    for kind, rules in (('tags', defaults.DEFAULT_TAG_RULES), ('keywords', defaults.DEFAULT_KEYWORD_RULES)):
        for position, (name, patterns) in enumerate(rules):
            # Tagi są zbiorem (bez kolejności) - sortujemy dla czytelności.
            values = sorted(patterns) if kind == 'tags' else list(patterns)
            Rule.objects.create(
                category=by_name[name], kind=kind, position=position,
                patterns='\n'.join(values),
            )


def unseed(apps, schema_editor):
    apps.get_model('cooking', 'PantryCategory').objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('cooking', '0021_pantry_categories'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
