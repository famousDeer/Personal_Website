"""Formularz przepisu: nic nie przepada po błędzie, ułamki sztuk, krok bez składników."""
import io
import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from PIL import Image

from .models import PantryMovement, PantryProduct, ProductGroup, Recipe, RecipeStep, RecipeStepIngredient

User = get_user_model()
TEMP_MEDIA = tempfile.mkdtemp()


def jpeg(name='danie.jpg'):
    buffer = io.BytesIO()
    Image.new('RGB', (40, 30), (200, 120, 40)).save(buffer, format='JPEG')
    return SimpleUploadedFile(name, buffer.getvalue(), content_type='image/jpeg')


def recipe_post(**overrides):
    data = {
        'title': 'Leczo',
        'description': 'Na szybko',
        'portions': '4',
        'kcal': '',
        'preparation_time': '',
        'kitchen_region': 'Kuchnia polska',
        'meal_type': 'Obiady',
        'type_of_dish': 'Duszone',
        'step_title': ['Warzywa', 'Duszenie'],
        'step_duration_minutes': ['10', '25'],
        'step_instruction': ['Pokrój paprykę i cebulę.', 'Duś pod przykryciem.'],
        'step_mix_after': ['0'],
        'ingredient_step': ['0', '0'],
        'ingredient_name': ['Papryka', 'Cebula'],
        'ingredient_quantity': ['2', '0,5'],
        'ingredient_unit': [PantryProduct.UNIT_PIECE, PantryProduct.UNIT_PIECE],
        'ingredient_category': ['Warzywa i owoce', ''],
    }
    data.update(overrides)
    return data


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class RecipeFormTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA, ignore_errors=True)

    def setUp(self):
        self.user = User.objects.create_user(username='kucharz', password='pass12345')
        self.client.force_login(self.user)
        self.add_url = reverse('cooking:add-recipe')

    def test_add_page_has_one_step_and_templates(self):
        response = self.client.get(self.add_url)
        self.assertTemplateUsed(response, 'cooking/recipe_form.html')
        self.assertContains(response, 'data-step-template')
        self.assertContains(response, 'data-ingredient-template')
        self.assertContains(response, f'data-draft-key="recipe-draft:new:{self.user.pk}"')
        self.assertEqual(len(response.context['form']['steps']), 1)

    def test_saves_fraction_of_piece_step_without_ingredients_and_sums_time(self):
        response = self.client.post(self.add_url, recipe_post())
        recipe = Recipe.objects.get(title='Leczo')
        self.assertRedirects(response, f"{reverse('cooking:recipe-list')}#przepis-{recipe.pk}", fetch_redirect_response=False)
        self.assertEqual(recipe.preparation_time, 35)  # 10 + 25 z kroków
        self.assertEqual(recipe.kcal, 0)               # nie podano
        first, second = recipe.steps.all()
        self.assertTrue(first.mix_after)
        self.assertEqual(second.ingredients.count(), 0)
        onion = RecipeStepIngredient.objects.get(name='Cebula')
        self.assertEqual(onion.quantity, Decimal('0.50'))
        self.assertIn('0,5 szt. - Cebula', recipe.ingredients)
        self.assertEqual(self.client.session['recipe_draft_saved'], f'recipe-draft:new:{self.user.pk}')

    def test_list_clears_draft_once_and_hides_missing_kcal(self):
        self.client.post(self.add_url, recipe_post())
        recipe = Recipe.objects.get(title='Leczo')
        response = self.client.get(reverse('cooking:recipe-list'))
        self.assertContains(response, f'data-recipe-draft-saved="recipe-draft:new:{self.user.pk}"')
        self.assertContains(response, f'id="przepis-{recipe.pk}"')
        self.assertContains(response, 'Dodano przepis „Leczo”.')
        self.assertNotContains(response, '0 kcal')
        again = self.client.get(reverse('cooking:recipe-list'))
        self.assertNotContains(again, 'data-recipe-draft-saved')

    def test_error_keeps_everything_that_was_typed(self):
        response = self.client.post(self.add_url, recipe_post(
            ingredient_quantity=['2', ''],  # cebula bez ilości
            image=jpeg(),
        ))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Recipe.objects.exists())
        self.assertContains(response, 'Przepis nie został zapisany')
        self.assertContains(response, 'Składnik „Cebula”: podaj ilość (albo wybierz „do smaku”).')
        self.assertContains(response, 'value="Leczo"')
        self.assertContains(response, 'Duś pod przykryciem.')
        self.assertContains(response, 'value="Papryka"')
        self.assertContains(response, 'value="Cebula"')
        self.assertContains(response, 'name="type_of_dish" value="Duszone" checked')
        self.assertContains(response, 'Wybierz zdjęcie jeszcze raz')
        self.assertContains(response, 'data-failed="1"')
        steps = response.context['form']['steps']
        ingredients = response.context['form']['ingredients']
        self.assertEqual(ingredients[1]['error'], 'podaj ilość (albo wybierz „do smaku”)')
        self.assertEqual(ingredients[1]['step'], '0')
        self.assertTrue(steps[0]['mix_after'])

    def test_bad_image_keeps_form(self):
        bad = SimpleUploadedFile('zle.jpg', b'to nie obraz', content_type='image/jpeg')
        response = self.client.post(self.add_url, recipe_post(image=bad))
        self.assertContains(response, 'Nie udało się odczytać zdjęcia')
        self.assertContains(response, 'Pokrój paprykę i cebulę.')
        self.assertNotContains(response, 'Wybierz zdjęcie jeszcze raz')
        self.assertFalse(Recipe.objects.exists())

    def test_missing_title_portions_time_and_steps(self):
        response = self.client.post(self.add_url, {
            'title': '', 'portions': '', 'preparation_time': '',
            'step_title': [''], 'step_duration_minutes': [''], 'step_instruction': [''],
            'ingredient_step': ['0'], 'ingredient_name': [''], 'ingredient_quantity': [''],
            'ingredient_unit': ['g'], 'ingredient_category': [''],
        })
        errors = response.context['form']['errors']
        self.assertEqual(set(errors), {'title', 'portions', 'steps'})
        self.assertContains(response, 'Dodaj co najmniej jeden krok przygotowania.')
        # Pusty formularz wraca z jednym krokiem do wypełnienia.
        self.assertContains(response, 'data-step-card', count=2)  # krok + wzór <template>

    def test_step_needs_text_and_time_needs_value(self):
        response = self.client.post(self.add_url, recipe_post(
            step_title=['', ''], step_duration_minutes=['', ''],
            step_instruction=['', 'Duś.'],
        ))
        self.assertContains(response, 'Krok 1: opisz, co zrobić.')
        self.assertContains(response, 'Podaj czas przygotowania albo czasy kroków.')

    def test_html_in_recipe_is_escaped_in_text_version(self):
        self.client.post(self.add_url, recipe_post(
            step_instruction=['<b>Pokrój</b>', 'Duś.'], ingredient_name=['Papryka <i>', 'Cebula'],
        ))
        recipe = Recipe.objects.get()
        self.assertIn('&lt;b&gt;Pokrój&lt;/b&gt;', recipe.instructions)
        self.assertIn('Papryka &lt;i&gt;', recipe.ingredients)

    def make_recipe(self, **extra):
        recipe = Recipe.objects.create(
            user=self.user, title='Zupa', ingredients='', instructions='',
            portions=3, kcal=0, preparation_time=40, **extra,
        )
        step = RecipeStep.objects.create(recipe=recipe, order=1, title='Gotowanie', instruction='Gotuj.', duration_minutes=40)
        RecipeStepIngredient.objects.create(step=step, order=1, name='Marchew', quantity=Decimal('0.50'), unit='szt')
        return recipe

    def test_edit_form_shows_auto_time_and_empty_kcal(self):
        recipe = self.make_recipe()
        response = self.client.get(reverse('cooking:edit-recipe', args=[recipe.pk]))
        form = response.context['form']
        self.assertEqual(form['preparation_time'], '')  # równy sumie kroków
        self.assertEqual(form['kcal'], '')
        self.assertEqual((form['ingredients'][0]['quantity'], form['ingredients'][0]['step']), ('0,5', '0'))
        self.assertContains(response, f'data-draft-key="recipe-draft:edit:{recipe.pk}:{self.user.pk}"')

    def test_edit_with_empty_portions_is_an_error_not_500(self):
        recipe = self.make_recipe()
        response = self.client.post(reverse('cooking:edit-recipe', args=[recipe.pk]), recipe_post(portions='', title='Nowa'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Porcje: podaj liczbę całkowitą.')
        self.assertContains(response, 'value="Nowa"')
        recipe.refresh_from_db()
        self.assertEqual(recipe.title, 'Zupa')
        self.assertEqual(recipe.steps.get().ingredients.get().name, 'Marchew')

    def test_edit_replaces_steps_and_keeps_unknown_category(self):
        recipe = self.make_recipe(kitchen_region='Kuchnia gruzińska')
        response = self.client.get(reverse('cooking:edit-recipe', args=[recipe.pk]))
        self.assertContains(response, 'name="kitchen_region" value="Kuchnia gruzińska" checked')
        self.assertContains(response, '<span>Gruzińska</span>')
        response = self.client.post(reverse('cooking:edit-recipe', args=[recipe.pk]), recipe_post(
            title='Zupa', kitchen_region='Kuchnia gruzińska', preparation_time='50',
        ))
        self.assertRedirects(response, f"{reverse('cooking:recipe-list')}#przepis-{recipe.pk}", fetch_redirect_response=False)
        recipe.refresh_from_db()
        self.assertEqual(recipe.kitchen_region, 'Kuchnia gruzińska')
        self.assertEqual(recipe.preparation_time, 50)
        self.assertEqual(recipe.steps.count(), 2)

    def test_photo_replace_and_remove_delete_old_file(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.add_url, recipe_post(image=jpeg('pierwsze.jpg')))
        recipe = Recipe.objects.get()
        storage = recipe.image.storage
        first = recipe.image.name
        self.assertTrue(storage.exists(first))

        url = reverse('cooking:edit-recipe', args=[recipe.pk])
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, recipe_post(image=jpeg('drugie.jpg')))
        recipe.refresh_from_db()
        second = recipe.image.name
        self.assertNotEqual(first, second)
        self.assertFalse(storage.exists(first))

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, recipe_post(remove_image='1'))
        recipe.refresh_from_db()
        self.assertFalse(recipe.image)
        self.assertFalse(storage.exists(second))

    def test_other_user_cannot_edit(self):
        recipe = self.make_recipe()
        other = User.objects.create_user(username='gosc', password='pass12345')
        self.client.force_login(other)
        response = self.client.post(reverse('cooking:edit-recipe', args=[recipe.pk]), recipe_post())
        self.assertEqual(response.status_code, 404)

    def test_cook_page_rounds_pieces_up_and_handles_step_without_ingredients(self):
        recipe = self.make_recipe()
        RecipeStep.objects.create(recipe=recipe, order=2, title='Odstaw', instruction='Odstaw na 10 minut.')
        response = self.client.get(reverse('cooking:cook'), {'recipe': recipe.pk})
        self.assertContains(response, 'value="1" required')
        self.assertContains(response, 'W przepisie 0,5 szt.')
        self.assertContains(response, 'W tym kroku nic nie ubywa ze spiżarni.')
        self.assertContains(response, 'data-cook-row-template')


class RecipePantryTests(TestCase):
    """Etap 2: podpowiedzi ze spiżarni, automatyczna kategoria, grupy przy gotowaniu."""

    def setUp(self):
        self.user = User.objects.create_user(username='kucharz2', password='pass12345')
        self.client.force_login(self.user)
        make = lambda name, **extra: PantryProduct.objects.create(  # noqa: E731
            created_by=self.user, name=name, **{'category': 'Nabiał', 'unit': 'g', **extra},
        )
        self.onion = make('Cebula', unit='szt', category='Warzywa i owoce', current_quantity=Decimal('3'))
        self.group = ProductGroup.objects.create(name='Jogurt naturalny', category='Nabiał')
        self.pilos = make('Jogurt naturalny Pilos', group=self.group, current_quantity=Decimal('100'))
        self.bakoma = make('Jogurt naturalny Bakoma', group=self.group, current_quantity=Decimal('400'))

    def test_form_has_pantry_index_with_groups_and_rules(self):
        response = self.client.get(reverse('cooking:add-recipe'))
        index = response.context['pantry_index']
        names = [entry['name'] for entry in index]
        self.assertIn('Cebula', names)
        group = next(entry for entry in index if entry['name'] == 'Jogurt naturalny')
        self.assertEqual((group['kind'], group['unit'], group['members']), ('group', 'g', 2))
        self.assertContains(response, 'id="recipe-pantry-index"')
        self.assertContains(response, 'id="recipe-keyword-rules"')
        self.assertTrue(response.context['keyword_rules'])
        self.assertContains(response, 'recipe-ingredients.js')

    def test_missing_category_is_filled_on_save(self):
        self.client.post(reverse('cooking:add-recipe'), recipe_post(
            ingredient_step=['0', '0', '0'],
            ingredient_name=['Cebula', 'Jogurt naturalny', 'Karma dla kota'],
            ingredient_quantity=['1', '200', '1'],
            ingredient_unit=['szt', 'g', 'szt'],
            ingredient_category=['', '', ''],
        ))
        categories = dict(RecipeStepIngredient.objects.values_list('name', 'category'))
        self.assertEqual(categories['Cebula'], 'Warzywa i owoce')    # ze spiżarni
        self.assertEqual(categories['Jogurt naturalny'], 'Nabiał')   # z grupy
        self.assertEqual(categories['Karma dla kota'], 'Dla zwierząt')  # z reguł słów

    def test_cooking_group_name_uses_brand_with_most_stock(self):
        response = self.client.post(reverse('cooking:cook'), {
            'product_name': ['Jogurt naturalny'], 'quantity': ['150'], 'unit': ['g'], 'category': [''],
        })
        self.assertEqual(response.status_code, 302)
        self.bakoma.refresh_from_db()
        self.pilos.refresh_from_db()
        self.assertEqual(self.bakoma.current_quantity, Decimal('250.00'))
        self.assertEqual(self.pilos.current_quantity, Decimal('100.00'))
        self.assertFalse(PantryProduct.objects.filter(name='Jogurt naturalny').exists())
        self.assertEqual(PantryMovement.objects.get().product, self.bakoma)

    def test_cook_page_suggests_group_names(self):
        response = self.client.get(reverse('cooking:cook'))
        self.assertContains(response, '<option value="Jogurt naturalny"></option>', html=True)


class RecipeIngredientListTests(TestCase):
    """Etap 3: lista składników przepisu, krok opcjonalny, jednostki kuchenne."""

    def setUp(self):
        self.user = User.objects.create_user(username='kucharz3', password='pass12345')
        self.client.force_login(self.user)
        self.oil = PantryProduct.objects.create(created_by=self.user, name='Oliwa', unit='ml', category='Tłuszcze',
                                                current_quantity=Decimal('500'))
        self.flour = PantryProduct.objects.create(created_by=self.user, name='Mąka', unit='g', category='Produkty suche',
                                                  current_quantity=Decimal('1000'))

    def post(self, **overrides):
        data = recipe_post(
            ingredient_name=['Oliwa', 'Mąka', 'Sól', 'Pieprz', 'Cebula'],
            ingredient_quantity=['2', '3', '', '', '1'],
            ingredient_unit=['lyzka', 'lyzka', 'do_smaku', 'szczypta', 'szt'],
            ingredient_category=['', '', '', '', ''],
            ingredient_step=['0', '', '1', '', '0'],
        )
        data.update(overrides)
        return self.client.post(reverse('cooking:add-recipe'), data)

    def test_saves_list_with_optional_steps_and_kitchen_units(self):
        response = self.post()
        self.assertEqual(response.status_code, 302, response.context and response.context.get('problems'))
        recipe = Recipe.objects.get()
        first, second = recipe.steps.all()
        rows = [(i.name, str(i.quantity), i.unit, i.step_id) for i in recipe.ingredient_items.all()]
        self.assertEqual(rows, [
            ('Oliwa', '2.00', 'lyzka', first.pk),
            ('Mąka', '3.00', 'lyzka', None),
            ('Sól', '0.00', 'do_smaku', second.pk),
            ('Pieprz', '1.00', 'szczypta', None),  # pusta szczypta = 1
            ('Cebula', '1.00', 'szt', first.pk),
        ])
        self.assertTrue(first.mix_after)
        self.assertIn('2 łyżki - Oliwa', recipe.ingredients)
        self.assertIn('do smaku - Sól', recipe.ingredients)
        self.assertIn('1 szczypta - Pieprz', recipe.ingredients)

    def test_empty_step_is_dropped_and_assignments_follow(self):
        self.post(
            step_title=['', 'Smażenie'], step_duration_minutes=['', '10'], step_instruction=['', 'Smaż.'],
            step_mix_after=[], ingredient_step=['1', '', '', '', '1'],
        )
        recipe = Recipe.objects.get()
        step = recipe.steps.get()
        self.assertEqual(sorted(recipe.ingredient_items.filter(step=step).values_list('name', flat=True)), ['Cebula', 'Oliwa'])

    def test_edit_form_lists_ingredients_with_step_numbers(self):
        self.post()
        recipe = Recipe.objects.get()
        response = self.client.get(reverse('cooking:edit-recipe', args=[recipe.pk]))
        form = response.context['form']
        self.assertEqual([(i['name'], i['quantity'], i['unit'], i['step']) for i in form['ingredients']], [
            ('Oliwa', '2', 'lyzka', '0'), ('Mąka', '3', 'lyzka', ''), ('Sól', '', 'do_smaku', '1'),
            ('Pieprz', '1', 'szczypta', ''), ('Cebula', '1', 'szt', '0'),
        ])
        self.assertContains(response, '<optgroup label="Kuchenne">')
        self.assertContains(response, '<option value="1" selected>Krok 2</option>', html=True)

    def test_cook_page_converts_spoons_and_skips_to_taste(self):
        self.post()
        recipe = Recipe.objects.get()
        response = self.client.get(reverse('cooking:cook'), {'recipe': recipe.pk})
        sections = response.context['cook_sections']
        self.assertEqual([section['label'] for section in sections], ['Składniki', 'Krok 1', 'Krok 2'])
        prefill = {row['ingredient'].name: row['prefill'] for section in sections for row in section['rows']}
        self.assertEqual((prefill['Oliwa'].quantity, prefill['Oliwa'].unit, prefill['Oliwa'].optional), ('30', 'ml', False))
        self.assertEqual((prefill['Mąka'].quantity, prefill['Mąka'].unit, prefill['Mąka'].optional), ('', 'g', True))
        self.assertIn('zważ', prefill['Mąka'].hint)
        self.assertTrue(prefill['Sól'].optional and prefill['Pieprz'].optional)
        self.assertContains(response, 'Składniki bez kroku')
        self.assertContains(response, '2 łyżki – Oliwa')

    def test_cooking_skips_rows_left_empty(self):
        response = self.client.post(reverse('cooking:cook'), {
            'product_name': ['Oliwa', 'Sól'], 'quantity': ['30', ''], 'unit': ['ml', 'g'], 'category': ['', ''],
        })
        self.assertEqual(response.status_code, 302)
        self.oil.refresh_from_db()
        self.assertEqual(self.oil.current_quantity, Decimal('470.00'))
        self.assertFalse(PantryProduct.objects.filter(name='Sól').exists())

    def test_to_taste_needs_no_amount_but_grams_do(self):
        response = self.post(ingredient_quantity=['2', '', '', '', '1'], ingredient_unit=['lyzka', 'g', 'do_smaku', 'szczypta', 'szt'])
        self.assertContains(response, 'Składnik „Mąka”: podaj ilość')
        self.assertFalse(Recipe.objects.exists())


class RecipeIngredientMigrationTests(TransactionTestCase):
    """0023: składniki dostają przepis swojego kroku i wspólną numerację."""

    migrate_from = ('cooking', '0022_seed_pantry_categories')
    migrate_to = ('cooking', '0023_recipe_ingredient_list')

    def test_existing_ingredients_keep_steps_and_get_recipe_order(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old = executor.loader.project_state([self.migrate_from]).apps
        user = old.get_model('auth', 'User').objects.create(username='stary')
        recipe = old.get_model('cooking', 'Recipe').objects.create(user=user, title='Stary', ingredients='', instructions='')
        Step = old.get_model('cooking', 'RecipeStep')
        Ingredient = old.get_model('cooking', 'RecipeStepIngredient')
        second = Step.objects.create(recipe=recipe, order=2, instruction='Dwa')
        first = Step.objects.create(recipe=recipe, order=1, instruction='Jeden')
        Ingredient.objects.create(step=second, order=1, name='C', quantity=1, unit='g')
        Ingredient.objects.create(step=first, order=2, name='B', quantity=1, unit='g')
        Ingredient.objects.create(step=first, order=1, name='A', quantity=1, unit='g')

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([self.migrate_to])
        new = executor.loader.project_state([self.migrate_to]).apps
        rows = new.get_model('cooking', 'RecipeStepIngredient').objects.order_by('order')
        self.assertEqual([(row.name, row.order, row.recipe_id, row.step_id) for row in rows], [
            ('A', 1, recipe.pk, first.pk), ('B', 2, recipe.pk, first.pk), ('C', 3, recipe.pk, second.pk),
        ])
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
