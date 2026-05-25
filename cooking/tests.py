from decimal import Decimal
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import PantryMovement, PantryProduct, Recipe, RecipeStep, RecipeStepIngredient


User = get_user_model()


class RecipeViewsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='owner', password='pass12345')
        self.other = User.objects.create_user(username='other', password='pass12345')
        self.owner_recipe = Recipe.objects.create(
            user=self.owner,
            title='Owner pasta',
            ingredients='<strong>Makaron</strong><script>alert(1)</script>',
            instructions='<a href="javascript:alert(1)">klik</a><p>Gotuj 10 minut.</p>',
            portions=2,
            kcal=500,
            preparation_time=20,
        )
        self.other_recipe = Recipe.objects.create(
            user=self.other,
            title='Other soup',
            ingredients='Woda',
            instructions='Gotuj',
            portions=1,
            kcal=100,
            preparation_time=10,
        )

    def test_recipe_list_is_public_for_authenticated_users(self):
        self.client.login(username='owner', password='pass12345')

        response = self.client.get(reverse('cooking:recipe-list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Owner pasta')
        self.assertContains(response, 'Other soup')

    def test_recipe_html_is_sanitized_on_list(self):
        self.client.login(username='owner', password='pass12345')

        response = self.client.get(reverse('cooking:recipe-list'))

        self.assertNotContains(response, '<script>alert')
        self.assertNotContains(response, 'javascript:alert')
        self.assertContains(response, '<strong>Makaron</strong>', html=True)

    def test_non_owner_cannot_edit_or_delete_recipe(self):
        self.client.login(username='other', password='pass12345')

        edit_response = self.client.get(reverse('cooking:edit-recipe', args=[self.owner_recipe.id]))
        delete_response = self.client.post(reverse('cooking:delete-recipe', args=[self.owner_recipe.id]))

        self.assertEqual(edit_response.status_code, 404)
        self.assertEqual(delete_response.status_code, 404)
        self.assertTrue(Recipe.objects.filter(id=self.owner_recipe.id).exists())


class RecipeStructureTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='recipe-structure-user', password='pass12345')
        self.client.login(username='recipe-structure-user', password='pass12345')

    def test_add_recipe_saves_structured_steps_and_ingredients(self):
        response = self.client.post(reverse('cooking:add-recipe'), {
            'title': 'Owsianka',
            'description': 'Prosta owsianka',
            'portions': '1',
            'kcal': '300',
            'preparation_time': '10',
            'kitchen_region': 'Kuchnia polska',
            'meal_type': 'Śniadania',
            'type_of_dish': 'Gotowane',
            'step_title': ['Gotowanie płatków', 'Podanie'],
            'step_duration_minutes': ['5', '1'],
            'step_instruction': ['Dodaj płatki do mleka.', 'Przełóż do miski.'],
            'step_mix_after': ['0'],
            'ingredient_step': ['0', '0', '1'],
            'ingredient_name': ['Płatki owsiane', 'Mleko', 'Banan'],
            'ingredient_quantity': ['50', '200', '1'],
            'ingredient_unit': [PantryProduct.UNIT_GRAM, PantryProduct.UNIT_MILLILITER, PantryProduct.UNIT_PIECE],
            'ingredient_category': ['Produkty suche', 'Nabiał', 'Warzywa i owoce'],
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        recipe = Recipe.objects.get(user=self.user, title='Owsianka')
        self.assertEqual(recipe.steps.count(), 2)
        first_step = recipe.steps.first()
        self.assertTrue(first_step.mix_after)
        self.assertEqual(first_step.ingredients.count(), 2)
        self.assertIn('Płatki owsiane', recipe.ingredients)
        self.assertIn('Dodaj płatki do mleka.', recipe.instructions)

    def test_edit_recipe_replaces_structured_steps(self):
        recipe = Recipe.objects.create(
            user=self.user,
            title='Stary przepis',
            ingredients='Stare',
            instructions='Stare',
        )
        step = RecipeStep.objects.create(recipe=recipe, order=1, instruction='Stary krok')
        RecipeStepIngredient.objects.create(step=step, order=1, name='Stary składnik', quantity=1, unit=PantryProduct.UNIT_GRAM)

        response = self.client.post(reverse('cooking:edit-recipe', args=[recipe.id]), {
            'title': 'Nowy przepis',
            'description': '',
            'portions': '2',
            'kcal': '100',
            'preparation_time': '15',
            'kitchen_region': '',
            'meal_type': '',
            'type_of_dish': '',
            'step_title': ['Nowy etap'],
            'step_duration_minutes': ['3'],
            'step_instruction': ['Dodaj ryż i gotuj.'],
            'ingredient_step': ['0'],
            'ingredient_name': ['Ryż'],
            'ingredient_quantity': ['100'],
            'ingredient_unit': [PantryProduct.UNIT_GRAM],
            'ingredient_category': ['Produkty suche'],
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        recipe.refresh_from_db()
        self.assertEqual(recipe.title, 'Nowy przepis')
        self.assertEqual(recipe.steps.count(), 1)
        self.assertEqual(recipe.steps.first().ingredients.get().name, 'Ryż')


class PantryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='pantry-user', password='pass12345')
        self.other = User.objects.create_user(username='other-pantry-user', password='pass12345')
        self.client.login(username='pantry-user', password='pass12345')

    def test_add_pantry_product_creates_initial_purchase_movement(self):
        response = self.client.post(reverse('cooking:add-pantry-product'), {
            'name': 'Ryż',
            'category': 'Produkty suche',
            'unit': PantryProduct.UNIT_KILOGRAM,
            'current_quantity': '2.50',
            'minimum_quantity': '0.50',
            'restock_lead_days': '4',
            'notes': 'Basmati',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        product = PantryProduct.objects.get(user=self.user, name='Ryż')
        self.assertEqual(product.current_quantity, Decimal('2.50'))
        self.assertEqual(product.minimum_quantity, Decimal('0.50'))
        movement = product.movements.get()
        self.assertEqual(movement.movement_type, PantryMovement.PURCHASE)
        self.assertEqual(movement.quantity, Decimal('2.50'))

    def test_pantry_movement_updates_stock_and_prediction(self):
        product = PantryProduct.objects.create(
            user=self.user,
            name='Kawa',
            category='Napoje',
            unit=PantryProduct.UNIT_GRAM,
            current_quantity=Decimal('1000.00'),
            minimum_quantity=Decimal('200.00'),
            restock_lead_days=5,
        )
        PantryMovement.objects.create(
            product=product,
            movement_type=PantryMovement.CONSUME,
            quantity=Decimal('300.00'),
            occurred_on=timezone.localdate() - timedelta(days=6),
        )

        response = self.client.post(reverse('cooking:pantry-movement', args=[product.id]), {
            'movement_type': PantryMovement.CONSUME,
            'quantity': '100',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.current_quantity, Decimal('900.00'))
        self.assertEqual(product.movements.filter(movement_type=PantryMovement.CONSUME).count(), 2)

        average_daily = product.average_daily_consumption()
        self.assertEqual(average_daily, Decimal('13.33333333333333333333333333'))
        self.assertEqual(product.projected_depletion_date(), timezone.localdate() + timedelta(days=68))
        self.assertEqual(product.suggested_restock_date(), timezone.localdate() + timedelta(days=63))

    def test_piece_unit_rejects_fractional_product_quantities(self):
        response = self.client.post(reverse('cooking:add-pantry-product'), {
            'name': 'Jajka',
            'category': 'Nabiał',
            'unit': PantryProduct.UNIT_PIECE,
            'current_quantity': '6.5',
            'minimum_quantity': '2',
            'restock_lead_days': '2',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PantryProduct.objects.filter(user=self.user, name='Jajka').exists())
        self.assertContains(response, 'Dla jednostki &quot;szt.&quot; podaj liczbę całkowitą.')

    def test_piece_unit_rejects_fractional_movements(self):
        product = PantryProduct.objects.create(
            user=self.user,
            name='Jajka',
            unit=PantryProduct.UNIT_PIECE,
            current_quantity=Decimal('6.00'),
            minimum_quantity=Decimal('2.00'),
        )

        response = self.client.post(reverse('cooking:pantry-movement', args=[product.id]), {
            'movement_type': PantryMovement.CONSUME,
            'quantity': '0.5',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.current_quantity, Decimal('6.00'))
        self.assertFalse(product.movements.exists())
        self.assertContains(response, 'Dla jednostki &quot;szt.&quot; podaj liczbę całkowitą.')

    def test_piece_unit_accepts_integer_movements(self):
        product = PantryProduct.objects.create(
            user=self.user,
            name='Jajka',
            unit=PantryProduct.UNIT_PIECE,
            current_quantity=Decimal('6.00'),
            minimum_quantity=Decimal('2.00'),
        )

        response = self.client.post(reverse('cooking:pantry-movement', args=[product.id]), {
            'movement_type': PantryMovement.CONSUME,
            'quantity': '2',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.current_quantity, Decimal('4.00'))
        self.assertEqual(product.movements.get().quantity, Decimal('2.00'))

    def test_pantry_list_only_shows_logged_in_users_products(self):
        PantryProduct.objects.create(
            user=self.user,
            name='Makaron',
            category='Produkty suche',
            unit=PantryProduct.UNIT_PACKAGE,
            current_quantity=Decimal('3.00'),
        )
        PantryProduct.objects.create(
            user=self.user,
            name='Mleko',
            category='Nabiał',
            unit=PantryProduct.UNIT_LITER,
            current_quantity=Decimal('1.00'),
        )
        PantryProduct.objects.create(
            user=self.other,
            name='Cukier',
            unit=PantryProduct.UNIT_KILOGRAM,
            current_quantity=Decimal('1.00'),
        )

        response = self.client.get(reverse('cooking:pantry'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Makaron')
        self.assertContains(response, 'Mleko')
        self.assertNotContains(response, 'Cukier')
        self.assertEqual(
            [group['name'] for group in response.context['product_groups']],
            ['Produkty suche', 'Nabiał'],
        )


class CookModeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='cook-user', password='pass12345')
        self.client.login(username='cook-user', password='pass12345')

    def test_cook_view_consumes_existing_pantry_product(self):
        product = PantryProduct.objects.create(
            user=self.user,
            name='Mąka',
            category='Produkty suche',
            unit=PantryProduct.UNIT_GRAM,
            current_quantity=Decimal('1000.00'),
        )

        response = self.client.post(reverse('cooking:cook'), {
            'product_name': ['Mąka'],
            'quantity': ['250'],
            'unit': [PantryProduct.UNIT_GRAM],
            'category': ['Produkty suche'],
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.current_quantity, Decimal('750.00'))
        movement = product.movements.get()
        self.assertEqual(movement.movement_type, PantryMovement.CONSUME)
        self.assertEqual(movement.quantity, Decimal('250.00'))
        self.assertEqual(movement.note, 'Gotowanie')

    def test_cook_view_creates_missing_consumed_product(self):
        response = self.client.post(reverse('cooking:cook'), {
            'product_name': ['Bazylia'],
            'quantity': ['10'],
            'unit': [PantryProduct.UNIT_GRAM],
            'category': ['Przyprawy'],
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        product = PantryProduct.objects.get(user=self.user, name='Bazylia')
        self.assertEqual(product.category, 'Przyprawy')
        self.assertEqual(product.unit, PantryProduct.UNIT_GRAM)
        self.assertEqual(product.current_quantity, Decimal('0.00'))
        movement = product.movements.get()
        self.assertEqual(movement.movement_type, PantryMovement.CONSUME)
        self.assertEqual(movement.quantity, Decimal('10.00'))

    def test_cook_view_converts_weight_to_product_unit(self):
        product = PantryProduct.objects.create(
            user=self.user,
            name='Cukier',
            unit=PantryProduct.UNIT_KILOGRAM,
            current_quantity=Decimal('1.00'),
        )

        response = self.client.post(reverse('cooking:cook'), {
            'product_name': ['Cukier'],
            'quantity': ['250'],
            'unit': [PantryProduct.UNIT_GRAM],
            'category': ['Produkty suche'],
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.current_quantity, Decimal('0.75'))
        self.assertEqual(product.movements.get().quantity, Decimal('0.25'))

    def test_cook_view_rejects_fractional_piece_product(self):
        product = PantryProduct.objects.create(
            user=self.user,
            name='Jajka',
            unit=PantryProduct.UNIT_PIECE,
            current_quantity=Decimal('6.00'),
        )

        response = self.client.post(reverse('cooking:cook'), {
            'product_name': ['Jajka'],
            'quantity': ['0.5'],
            'unit': [PantryProduct.UNIT_PIECE],
            'category': ['Nabiał'],
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.current_quantity, Decimal('6.00'))
        self.assertFalse(product.movements.exists())
        self.assertContains(response, 'Dla jednostki &quot;szt.&quot; podaj liczbę całkowitą.')

    def test_cook_view_prefills_structured_recipe_steps(self):
        recipe = Recipe.objects.create(
            user=self.user,
            title='Naleśniki',
            ingredients='',
            instructions='',
        )
        step = RecipeStep.objects.create(recipe=recipe, order=1, title='Ciasto', instruction='Wymieszaj składniki.', mix_after=True)
        RecipeStepIngredient.objects.create(
            step=step,
            order=1,
            name='Mąka',
            quantity=Decimal('120.00'),
            unit=PantryProduct.UNIT_GRAM,
            category='Produkty suche',
        )

        response = self.client.get(reverse('cooking:cook'), {'recipe': recipe.id})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Ciasto')
        self.assertContains(response, 'Mąka')
        self.assertContains(response, '120.00')
        self.assertContains(response, 'Po dodaniu wymieszaj')
