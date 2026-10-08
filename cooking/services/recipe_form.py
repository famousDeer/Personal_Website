"""Formularz przepisu: odczyt, sprawdzenie i zapis.

Jeden kod dla dodawania i edycji. Formularz zawsze renderuje się ze „stanu”
(słownik z tekstami tak, jak je wpisał użytkownik), więc po błędzie wraca
dokładnie to, co było wpisane - razem z krokami i składnikami - a przy
polach, które trzeba poprawić, stoi komunikat.

Składniki to lista przepisu; każdy może (nie musi) wskazywać krok, w którym
się go używa - wtedy „Gotuj” pokazuje go przy tym kroku.

Pola formularza:
    title, description, portions, kcal, preparation_time,
    kitchen_region, meal_type, type_of_dish, image, remove_image,
    step_title[], step_duration_minutes[], step_instruction[],
    step_mix_after[] (numery kroków od 0),
    ingredient_name[], ingredient_quantity[], ingredient_unit[],
    ingredient_category[], ingredient_step[] (numer kroku od 0 albo pusty)
"""
from decimal import Decimal

from django.db import transaction
from django.utils.html import escape

from ..models import PantryProduct, Recipe, RecipeStep, RecipeStepIngredient
from .pantry_quantities import parse_pantry_decimal
from .recipe_pantry import ingredient_category
from .recipe_units import NO_AMOUNT_UNITS, RECIPE_UNITS, UNIT_PINCH, amount_text

MAX_PORTIONS = 999
MAX_KCAL = 20000
MAX_MINUTES = 10000
UNIT_VALUES = RECIPE_UNITS
DEFAULT_UNIT = PantryProduct.UNIT_GRAM


def empty_ingredient(name='', quantity='', unit=DEFAULT_UNIT, category='', step=''):
    return {'name': name, 'quantity': quantity, 'unit': unit, 'category': category, 'step': step, 'error': ''}


def empty_step(title='', duration='', instruction='', mix_after=False):
    return {
        'title': title,
        'duration': duration,
        'instruction': instruction,
        'mix_after': mix_after,
        'error': '',
    }


def _decimal_text(value):
    """Ilość do pola składnika po polsku: 0,5 zamiast 0.50, 200 zamiast 200.00."""
    text = f'{Decimal(value):f}'
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text.replace('.', ',')


def _fields(**values):
    state = {
        'title': '', 'description': '', 'portions': '2', 'kcal': '', 'preparation_time': '',
        'kitchen_region': '', 'meal_type': '', 'type_of_dish': '',
        'steps': [empty_step()], 'ingredients': [empty_ingredient()], 'errors': {},
    }
    state.update(values)
    return state


def blank_state():
    return _fields()


def state_from_recipe(recipe):
    steps = []
    step_index = {}
    total = 0
    for step in recipe.steps.all():
        total += step.duration_minutes or 0
        step_index[step.pk] = str(len(steps))
        steps.append(empty_step(
            title=step.title,
            duration='' if step.duration_minutes is None else str(step.duration_minutes),
            instruction=step.instruction,
            mix_after=step.mix_after,
        ))
    if not steps:
        # Stary przepis zapisany jako tekst: opis trafia do jednego kroku.
        steps.append(empty_step(title='Przygotowanie'))
    ingredients = [
        empty_ingredient(
            name=item.name,
            quantity='' if item.unit in NO_AMOUNT_UNITS else _decimal_text(item.quantity),
            unit=item.unit,
            category=item.category,
            step=step_index.get(item.step_id, ''),
        )
        for item in recipe.ingredient_items.all()
    ] or [empty_ingredient()]
    # Czas równy sumie kroków zostaje pusty - dalej liczy się sam.
    preparation_time = '' if total and recipe.preparation_time == total else str(recipe.preparation_time)
    return _fields(
        title=recipe.title,
        description=recipe.description,
        portions=str(recipe.portions),
        kcal='' if not recipe.kcal else str(recipe.kcal),
        preparation_time=preparation_time,
        kitchen_region=recipe.kitchen_region,
        meal_type=recipe.meal_type,
        type_of_dish=recipe.type_of_dish,
        steps=steps,
        ingredients=ingredients,
    )


def _get(values, index, default=''):
    return values[index] if index < len(values) else default


def _step_number(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def state_from_post(post):
    titles = post.getlist('step_title')
    durations = post.getlist('step_duration_minutes')
    instructions = post.getlist('step_instruction')
    mixed = set(post.getlist('step_mix_after'))
    count = max(len(titles), len(durations), len(instructions))
    raw_steps = [
        empty_step(
            title=_get(titles, index).strip(),
            duration=_get(durations, index).strip(),
            instruction=_get(instructions, index).strip(),
            mix_after=str(index) in mixed,
        )
        for index in range(count)
    ]

    names = post.getlist('ingredient_name')
    step_numbers = post.getlist('ingredient_step')
    quantities = post.getlist('ingredient_quantity')
    units = post.getlist('ingredient_unit')
    categories = post.getlist('ingredient_category')
    raw_ingredients = []
    for index, raw_name in enumerate(names):
        name = raw_name.strip()
        quantity = _get(quantities, index).strip()
        if not name and not quantity:
            continue
        raw_ingredients.append((_step_number(_get(step_numbers, index)), empty_ingredient(
            name=name,
            quantity=quantity,
            unit=_get(units, index, DEFAULT_UNIT),
            category=_get(categories, index).strip(),
        )))

    # Całkiem puste kroki (bez treści i bez składników) znikają, a numery
    # kroków w składnikach przesuwają się razem z nimi.
    used = {number for number, _ in raw_ingredients}
    steps = []
    renumber = {}
    for index, step in enumerate(raw_steps):
        if _has_content(step) or index in used:
            renumber[index] = str(len(steps))
            steps.append(step)
    ingredients = []
    for number, ingredient in raw_ingredients:
        ingredient['step'] = renumber.get(number, '')
        ingredients.append(ingredient)

    return _fields(
        title=post.get('title', '').strip(),
        description=post.get('description', '').strip(),
        portions=post.get('portions', '').strip(),
        kcal=post.get('kcal', '').strip(),
        preparation_time=post.get('preparation_time', '').strip(),
        kitchen_region=post.get('kitchen_region', '').strip(),
        meal_type=post.get('meal_type', '').strip(),
        type_of_dish=post.get('type_of_dish', '').strip(),
        steps=steps or [empty_step()],
        ingredients=ingredients or [empty_ingredient()],
    )


def _has_content(step):
    return bool(step['title'] or step['instruction'] or step['duration'])


def _whole_number(text, minimum, maximum):
    try:
        number = int(text.replace(' ', ''))
    except ValueError:
        raise ValueError('podaj liczbę całkowitą')
    if number < minimum or number > maximum:
        raise ValueError(f'podaj liczbę od {minimum} do {maximum}')
    return number


def validate(state):
    """Sprawdza stan formularza.

    Zwraca (dane do zapisu, lista komunikatów). Błędy są też dopisywane do
    stanu: state['errors'][pole], step['error'], ingredient['error'] - tak
    szablon pokazuje je przy właściwym polu.
    """
    errors = state['errors']
    messages = []

    def fail(key, text):
        errors[key] = text
        messages.append(text)

    clean = {
        'title': state['title'],
        'description': state['description'],
        'kitchen_region': state['kitchen_region'][:100],
        'meal_type': state['meal_type'][:100],
        'type_of_dish': state['type_of_dish'][:100],
    }
    if not clean['title']:
        fail('title', 'Podaj nazwę dania.')
    elif len(clean['title']) > 255:
        fail('title', 'Nazwa dania może mieć najwyżej 255 znaków.')

    try:
        clean['portions'] = _whole_number(state['portions'], 1, MAX_PORTIONS)
    except ValueError as exc:
        fail('portions', f'Porcje: {exc}.')
    if state['kcal']:
        try:
            clean['kcal'] = _whole_number(state['kcal'], 0, MAX_KCAL)
        except ValueError as exc:
            fail('kcal', f'Kcal: {exc}.')
    else:
        clean['kcal'] = 0  # 0 = nie podano

    steps = []
    total_minutes = 0
    assigned = {ingredient['step'] for ingredient in state['ingredients'] if ingredient['step'] != ''}
    for number, step in enumerate(state['steps'], start=1):
        if not _has_content(step) and str(number - 1) not in assigned:
            continue
        problems = []
        duration = None
        if step['duration']:
            try:
                duration = _whole_number(step['duration'], 0, MAX_MINUTES)
                total_minutes += duration
            except ValueError as exc:
                problems.append(f'czas: {exc}')
        instruction = step['instruction'] or step['title']
        if not instruction:
            problems.append('opisz, co zrobić')
        if problems:
            step['error'] = '; '.join(problems)
            messages.append(f'Krok {number}: {step["error"]}.')
            continue
        steps.append({
            'title': step['title'][:160],
            'instruction': instruction,
            'duration': duration,
            'mix_after': step['mix_after'] and str(number - 1) in assigned,
        })

    has_steps = any(_has_content(step) for step in state['steps']) or bool(assigned)
    if not has_steps:
        fail('steps', 'Dodaj co najmniej jeden krok przygotowania.')
    clean['steps'] = steps

    ingredients = []
    for ingredient in state['ingredients']:
        if not ingredient['name'] and not ingredient['quantity']:
            continue  # pusty wiersz do wypełnienia
        issue = ''
        quantity = Decimal('0.00')
        unit = ingredient['unit'] = ingredient['unit'] if ingredient['unit'] in UNIT_VALUES else DEFAULT_UNIT
        if not ingredient['name']:
            issue = 'podaj nazwę'
        elif len(ingredient['name']) > 160:
            issue = 'nazwa może mieć najwyżej 160 znaków'
        elif not ingredient['quantity']:
            if unit == UNIT_PINCH:
                quantity = Decimal('1.00')
            elif unit not in NO_AMOUNT_UNITS:
                issue = 'podaj ilość (albo wybierz „do smaku”)'
        else:
            try:
                quantity = parse_pantry_decimal(ingredient['quantity'])
                if quantity <= 0 and unit not in NO_AMOUNT_UNITS:
                    raise ValueError('ilość musi być większa od zera')
            except ValueError as exc:
                issue = str(exc)[:1].lower() + str(exc)[1:].rstrip('.')
        if issue:
            ingredient['error'] = issue
            label = f'„{ingredient["name"]}”' if ingredient['name'] else 'bez nazwy'
            messages.append(f'Składnik {label}: {issue}.')
            continue
        step = _step_number(ingredient['step'])
        ingredients.append({
            'name': ingredient['name'],
            'quantity': quantity,
            'unit': unit,
            'category': ingredient['category'][:120],
            'step': step if step is not None and step < len(state['steps']) else None,
        })
    clean['ingredients'] = ingredients

    if state['preparation_time']:
        try:
            clean['preparation_time'] = _whole_number(state['preparation_time'], 1, MAX_MINUTES)
        except ValueError as exc:
            fail('preparation_time', f'Czas przygotowania: {exc}.')
    elif total_minutes:
        clean['preparation_time'] = total_minutes
    elif has_steps:
        fail('preparation_time', 'Podaj czas przygotowania albo czasy kroków.')

    return clean, messages


def legacy_text(clean):
    """Tekstowe pola ingredients/instructions (lista przepisów, stare widoki)."""
    ingredient_lines = [
        f'<p>{escape(amount_text(item["quantity"], item["unit"]))} - {escape(item["name"])}</p>'
        for item in clean['ingredients']
    ]
    instruction_lines = []
    for order, step in enumerate(clean['steps'], start=1):
        if step['title']:
            instruction_lines.append(f'<p><strong>Krok {order}: {escape(step["title"])}</strong></p>')
        if step['instruction'] != step['title']:
            instruction_lines.append(f'<p>{escape(step["instruction"])}</p>')
        if step['mix_after']:
            instruction_lines.append('<p>Wymieszaj składniki.</p>')
    return ''.join(ingredient_lines), ''.join(instruction_lines)


@transaction.atomic
def save_recipe(recipe, clean, image=None, remove_image=False):
    old_image = recipe.image.name if recipe.pk and recipe.image else ''
    for field in ('title', 'description', 'portions', 'kcal', 'preparation_time',
                  'kitchen_region', 'meal_type', 'type_of_dish'):
        setattr(recipe, field, clean[field])
    recipe.ingredients, recipe.instructions = legacy_text(clean)
    if image:
        recipe.image = image
    elif remove_image:
        recipe.image = None
    recipe.save()

    recipe.ingredient_items.all().delete()
    recipe.steps.all().delete()
    created = [
        RecipeStep.objects.create(
            recipe=recipe,
            order=order,
            title=step['title'],
            instruction=step['instruction'],
            mix_after=step['mix_after'],
            duration_minutes=step['duration'],
        )
        for order, step in enumerate(clean['steps'], start=1)
    ]
    rows = []
    for position, item in enumerate(clean['ingredients'], start=1):
        # Kategoria potrzebna jest tylko, gdy „Gotuj” zakłada nowy produkt.
        # Nie podana - bierzemy ją ze spiżarni albo z reguł słów kluczowych.
        category = item['category'] or ingredient_category(item['name'])
        step = created[item['step']] if item['step'] is not None and item['step'] < len(created) else None
        rows.append(RecipeStepIngredient(
            recipe=recipe, step=step, order=position, name=item['name'],
            quantity=item['quantity'], unit=item['unit'], category=category,
        ))
    RecipeStepIngredient.objects.bulk_create(rows)

    # Stare zdjęcie znika z karty dopiero po udanym zapisie.
    new_image = recipe.image.name if recipe.image else ''
    if old_image and old_image != new_image:
        storage = Recipe._meta.get_field('image').storage
        transaction.on_commit(lambda: storage.delete(old_image))
    return recipe
