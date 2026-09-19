import json
from io import BytesIO, StringIO
import tempfile
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.signals import request_finished
from django.db import close_old_connections
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image as PILImage

from .models import (
    PantryMovement,
    PantryProduct,
    ProductCatalogEntry,
    ProductCatalogQuota,
    Recipe,
    RecipeStep,
    RecipeStepIngredient,
    ShoppingList,
    ShoppingListItem,
)
from django.core.management import call_command
from django.template import Context, Template
from django.test import SimpleTestCase

from .constants import PANTRY_CATEGORIES, PANTRY_CATEGORY_GROUPS
from .services.product_catalog import (
    CatalogProductNotFound,
    CatalogUnavailable,
    _pick_product_name,
    _suggest_category,
    _text_has_keyword,
)
from .services.pantry_forecast import (
    _intermittent_days_range,
    forecast_pantry_product,
    forecast_pantry_products,
    infer_typical_shopping_weekday,
)


User = get_user_model()


def make_test_image(name='product.png', color=(42, 132, 92)):
    buffer = BytesIO()
    PILImage.new('RGB', (24, 24), color=color).save(buffer, format='PNG')
    return SimpleUploadedFile(name, buffer.getvalue(), content_type='image/png')


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

    def test_non_owner_can_open_recipe_in_cook_mode(self):
        self.client.login(username='other', password='pass12345')

        response = self.client.get(reverse('cooking:cook'), {'recipe': self.owner_recipe.id})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Owner pasta')

    def test_add_recipe_rejects_invalid_image_upload(self):
        self.client.login(username='owner', password='pass12345')
        bad_image = SimpleUploadedFile(
            'bad.jpg',
            b'not an image',
            content_type='image/jpeg',
        )

        response = self.client.post(reverse('cooking:add-recipe'), {
            'title': 'Bad image recipe',
            'description': '',
            'portions': '1',
            'kcal': '0',
            'preparation_time': '5',
            'step_title': ['Krok'],
            'step_duration_minutes': ['1'],
            'step_instruction': ['Gotuj.'],
            'ingredient_step': ['0'],
            'ingredient_name': ['Ryż'],
            'ingredient_quantity': ['100'],
            'ingredient_unit': [PantryProduct.UNIT_GRAM],
            'ingredient_category': ['Produkty suche'],
            'image': bad_image,
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Nie udało się odczytać zdjęcia')
        self.assertFalse(Recipe.objects.filter(user=self.owner, title='Bad image recipe').exists())


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
            'current_package_count': '3',
            'minimum_quantity': '0.50',
            'restock_lead_days': '4',
            'notes': 'Basmati',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        product = PantryProduct.objects.get(user=self.user, name='Ryż')
        self.assertEqual(product.current_quantity, Decimal('2.50'))
        self.assertEqual(product.current_package_count, 3)
        self.assertEqual(product.minimum_quantity, Decimal('0.50'))
        movement = product.movements.get()
        self.assertEqual(movement.movement_type, PantryMovement.PURCHASE)
        self.assertEqual(movement.quantity, Decimal('2.50'))
        self.assertEqual(movement.package_count, 3)

    def test_add_pantry_product_rejects_inconsistent_package_total(self):
        response = self.client.post(reverse('cooking:add-pantry-product'), {
            'name': 'Niespójny jogurt',
            'barcode': '5900000000998',
            'unit': PantryProduct.UNIT_MILLILITER,
            'quantity_per_scan': '200',
            'current_quantity': '1000',
            'current_package_count': '3',
            'minimum_quantity': '0',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PantryProduct.objects.filter(name='Niespójny jogurt').exists())
        self.assertContains(response, 'Liczba opakowań nie odpowiada ilości łącznej')

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
        self.assertEqual(product.current_package_count, 0)
        self.assertIsNone(product.movements.order_by('-created_at').first().package_count)
        self.assertEqual(product.movements.filter(movement_type=PantryMovement.CONSUME).count(), 2)

        forecast = forecast_pantry_product(product)
        average_daily = product.average_daily_consumption()
        self.assertEqual(average_daily, forecast.rate)
        self.assertGreater(average_daily, Decimal('13.33'))
        self.assertEqual(product.projected_depletion_date(), forecast.minimum_date_to)
        self.assertEqual(product.suggested_restock_date(), forecast.buy_date)

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

    def test_manual_package_movement_updates_count_and_total_quantity(self):
        product = PantryProduct.objects.create(
            user=self.user,
            name='Jogurt',
            unit=PantryProduct.UNIT_MILLILITER,
            quantity_per_scan=Decimal('200.00'),
            current_quantity=Decimal('1000.00'),
            current_package_count=5,
        )

        response = self.client.post(reverse('cooking:pantry-movement', args=[product.id]), {
            'movement_type': PantryMovement.CONSUME,
            'package_count': '2',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.current_package_count, 3)
        self.assertEqual(product.current_quantity, Decimal('600.00'))
        movement = product.movements.get()
        self.assertEqual(movement.package_count, 2)
        self.assertEqual(movement.quantity, Decimal('400.00'))

    def test_manual_overconsumption_records_only_fulfilled_quantity(self):
        product = PantryProduct.objects.create(
            user=self.user,
            name='Mały jogurt',
            barcode='5900000000097',
            unit=PantryProduct.UNIT_MILLILITER,
            quantity_per_scan=Decimal('200.00'),
            current_quantity=Decimal('200.00'),
            current_package_count=1,
        )

        response = self.client.post(reverse('cooking:pantry-movement', args=[product.id]), {
            'movement_type': PantryMovement.CONSUME,
            'package_count': '5',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        movement = product.movements.get()
        self.assertEqual(product.current_quantity, Decimal('0.00'))
        self.assertEqual(movement.quantity, Decimal('200.00'))
        self.assertEqual(movement.requested_quantity, Decimal('1000.00'))
        self.assertEqual(movement.package_count, 1)
        self.assertEqual(movement.requested_package_count, 5)
        self.assertTrue(movement.stock_was_insufficient)

    def test_manual_movement_operation_id_is_idempotent(self):
        product = PantryProduct.objects.create(
            user=self.user,
            name='Mleko idempotentne',
            barcode='5900000000905',
            unit=PantryProduct.UNIT_MILLILITER,
            quantity_per_scan=Decimal('500.00'),
            current_quantity=Decimal('1000.00'),
            current_package_count=2,
        )
        operation_id = str(uuid4())
        payload = {
            'movement_type': PantryMovement.CONSUME,
            'package_count': '1',
            'operation_id': operation_id,
        }

        self.client.post(reverse('cooking:pantry-movement', args=[product.id]), payload)
        self.client.post(reverse('cooking:pantry-movement', args=[product.id]), payload)

        product.refresh_from_db()
        self.assertEqual(product.current_quantity, Decimal('500.00'))
        self.assertEqual(product.current_package_count, 1)
        self.assertEqual(product.movements.count(), 1)
        self.assertEqual(str(product.movements.get().scan_id), operation_id)

    def test_manual_form_uses_base_quantity_for_unpacked_product(self):
        product = PantryProduct.objects.create(
            user=self.user,
            name='Ryż luzem',
            unit=PantryProduct.UNIT_KILOGRAM,
            current_quantity=Decimal('2.50'),
        )

        response = self.client.get(reverse('cooking:pantry'))

        self.assertContains(
            response,
            f'<input id="consume-{product.id}" type="number" name="quantity" '
            'step="0.01" min="0.01" max="99999999.99" '
            'class="form-control" inputmode="decimal" placeholder="Ilość" required>',
            html=True,
        )
        self.assertContains(response, '<span class="input-group-text">kg</span>', html=True)

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
            # Kolejność grup idzie za PANTRY_CATEGORY_GROUPS.
            ['Nabiał', 'Produkty suche'],
        )

    def test_empty_product_without_history_shows_immediate_restock_alert(self):
        PantryProduct.objects.create(
            user=self.user,
            name='Pusty produkt',
            category='Inne',
            unit=PantryProduct.UNIT_PACKAGE,
            current_quantity=Decimal('0.00'),
            minimum_quantity=Decimal('0.00'),
        )

        response = self.client.get(reverse('cooking:pantry'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Zapas osiągnął ustawione minimum')
        self.assertContains(response, 'Uzupełnij teraz')

    def test_due_forecast_is_included_in_restock_filter_even_above_minimum(self):
        product = PantryProduct.objects.create(
            user=self.user,
            name='Produkt prognozowany',
            unit=PantryProduct.UNIT_PIECE,
            current_quantity=Decimal('3.00'),
            minimum_quantity=Decimal('2.00'),
            restock_lead_days=3,
        )
        for days_ago in range(30):
            PantryMovement.objects.create(
                product=product,
                movement_type=PantryMovement.CONSUME,
                quantity=Decimal('1.00'),
                occurred_on=timezone.localdate() - timedelta(days=days_ago),
            )

        response = self.client.get(reverse('cooking:pantry'), {'status': 'low'})

        self.assertContains(response, 'Produkt prognozowany')
        self.assertEqual(response.context['product_count'], 1)

    def test_active_filters_show_no_results_state_instead_of_first_scan_state(self):
        PantryProduct.objects.create(
            user=self.user,
            name='Makaron',
            unit=PantryProduct.UNIT_PACKAGE,
            current_quantity=Decimal('1.00'),
        )

        response = self.client.get(reverse('cooking:pantry'), {'q': 'nie istnieje'})

        self.assertContains(response, 'Brak produktów spełniających filtry')
        self.assertNotContains(response, 'Spiżarnia czeka na pierwszy skan')
        self.assertEqual(response.context['product_count'], 0)


class PantryForecastTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='forecast-user', password='pass12345')
        self.today = timezone.localdate()

    def product(self, **overrides):
        values = {
            'user': self.user,
            'name': f'Produkt {PantryProduct.objects.count() + 1}',
            'unit': PantryProduct.UNIT_PIECE,
            'current_quantity': Decimal('10.00'),
            'minimum_quantity': Decimal('2.00'),
            'quantity_per_scan': Decimal('1.00'),
            'restock_lead_days': 2,
        }
        values.update(overrides)
        return PantryProduct.objects.create(**values)

    def consume(self, product, quantity, days_ago, package_count=None):
        return PantryMovement.objects.create(
            product=product,
            movement_type=PantryMovement.CONSUME,
            quantity=Decimal(str(quantity)),
            package_count=package_count,
            occurred_on=self.today - timedelta(days=days_ago),
        )

    def test_no_history_does_not_invent_purchase_date(self):
        product = self.product()

        forecast = forecast_pantry_product(product, today=self.today)

        self.assertEqual(forecast.status, 'no_history')
        self.assertEqual(forecast.model, 'none')
        self.assertEqual(forecast.rate, Decimal('0.00'))
        self.assertIsNone(forecast.buy_date)
        self.assertFalse(forecast.is_due)

    def test_no_history_at_minimum_recommends_one_full_package(self):
        product = self.product(
            barcode='5900000000011',
            unit=PantryProduct.UNIT_MILLILITER,
            current_quantity=Decimal('0.00'),
            minimum_quantity=Decimal('0.00'),
            quantity_per_scan=Decimal('200.00'),
        )

        forecast = forecast_pantry_product(product, today=self.today)

        self.assertTrue(forecast.is_due)
        self.assertEqual(forecast.buy_date, self.today)
        self.assertEqual(forecast.suggested_packages, 1)
        self.assertEqual(forecast.suggested_quantity, Decimal('200.00'))

    def test_short_history_uses_actual_exposure_instead_of_fixed_thirty_days(self):
        product = self.product(
            unit=PantryProduct.UNIT_GRAM,
            current_quantity=Decimal('1000.00'),
            minimum_quantity=Decimal('200.00'),
        )
        self.consume(product, 100, days_ago=6)

        forecast = forecast_pantry_product(product, today=self.today)

        self.assertEqual(forecast.status, 'cold_start')
        self.assertEqual(forecast.history_days, 7)
        self.assertGreater(forecast.rate, Decimal('10.00'))
        self.assertLess(forecast.rate, Decimal('20.00'))
        self.assertIsNone(forecast.buy_date)
        self.assertFalse(forecast.is_due)

    def test_regular_consumption_produces_ready_forecast_and_minimum_range(self):
        product = self.product(current_quantity=Decimal('12.00'), minimum_quantity=Decimal('2.00'))
        for days_ago in range(30):
            self.consume(product, 1, days_ago=days_ago, package_count=1)

        forecast = forecast_pantry_product(product, today=self.today)

        self.assertEqual(forecast.status, 'ready')
        self.assertEqual(forecast.model, 'regular')
        self.assertEqual(forecast.rate, Decimal('1.00'))
        self.assertIsNotNone(forecast.minimum_date_from)
        self.assertIsNotNone(forecast.minimum_date_to)
        self.assertLessEqual(forecast.minimum_date_from, forecast.minimum_date_to)
        self.assertLess(forecast.buy_date, forecast.minimum_date_from)

    def test_intermittent_consumption_uses_separate_model(self):
        product = self.product(current_quantity=Decimal('8.00'), minimum_quantity=Decimal('1.00'))
        for days_ago in [0, 10, 20, 30, 40, 50]:
            self.consume(product, 2, days_ago=days_ago)

        forecast = forecast_pantry_product(product, today=self.today)

        self.assertEqual(forecast.status, 'ready')
        self.assertEqual(forecast.model, 'intermittent')
        self.assertGreater(forecast.rate, Decimal('0.10'))
        self.assertLess(forecast.rate, Decimal('0.40'))
        self.assertGreater(
            forecast.days_to_minimum_to,
            forecast.days_to_minimum_from,
        )

    def test_intermittent_pattern_has_wider_range_than_regular_pattern_at_similar_rate(self):
        regular = self.product(name='Regularny', current_quantity=Decimal('12.00'), minimum_quantity=Decimal('2.00'))
        intermittent = self.product(name='Sporadyczny', current_quantity=Decimal('12.00'), minimum_quantity=Decimal('2.00'))
        for days_ago in range(30):
            self.consume(regular, 1, days_ago=days_ago)
        for days_ago in range(0, 30, 5):
            self.consume(intermittent, 5, days_ago=days_ago)

        regular_forecast = forecast_pantry_product(regular, today=self.today)
        intermittent_forecast = forecast_pantry_product(intermittent, today=self.today)

        regular_width = regular_forecast.days_to_minimum_to - regular_forecast.days_to_minimum_from
        intermittent_width = intermittent_forecast.days_to_minimum_to - intermittent_forecast.days_to_minimum_from
        self.assertEqual(regular_forecast.model, 'regular')
        self.assertEqual(intermittent_forecast.model, 'intermittent')
        self.assertGreater(intermittent_width, regular_width)

    def test_rare_single_event_uses_exact_geometric_quantiles(self):
        days_from, days_to = _intermittent_days_range(
            usable_stock=1.0,
            event_probability=0.02,
            event_size=5.0,
            event_values=[5.0],
            confidence_score=80,
        )

        self.assertEqual(days_from, 6)
        self.assertEqual(days_to, 114)

    def test_single_outlier_is_robustly_capped(self):
        product = self.product(current_quantity=Decimal('40.00'), minimum_quantity=Decimal('2.00'))
        for days_ago in range(30):
            self.consume(product, 101 if days_ago == 3 else 1, days_ago=days_ago)

        forecast = forecast_pantry_product(product, today=self.today)

        self.assertEqual(forecast.model, 'regular')
        self.assertLess(forecast.rate, Decimal('1.25'))

    def test_days_after_stockout_are_censored_instead_of_zero_demand(self):
        product = self.product(current_quantity=Decimal('0.00'), minimum_quantity=Decimal('0.00'))
        self.consume(product, 1, days_ago=20, package_count=1)

        forecast = forecast_pantry_product(product, today=self.today)

        self.assertEqual(forecast.history_days, 1)
        self.assertEqual(forecast.rate, Decimal('1.00'))

    def test_purchase_quantity_rounds_up_to_complete_barcode_packages(self):
        product = self.product(
            barcode='5900000000028',
            unit=PantryProduct.UNIT_MILLILITER,
            current_quantity=Decimal('200.00'),
            minimum_quantity=Decimal('200.00'),
            quantity_per_scan=Decimal('200.00'),
        )
        for days_ago in range(0, 28, 4):
            self.consume(product, 200, days_ago=days_ago, package_count=1)

        forecast = forecast_pantry_product(product, today=self.today)

        self.assertTrue(forecast.is_due)
        self.assertGreaterEqual(forecast.suggested_packages, 1)
        self.assertEqual(
            forecast.suggested_quantity,
            Decimal(forecast.suggested_packages) * product.quantity_per_scan,
        )

    def test_unpacked_product_without_history_uses_minimum_as_purchase_fallback(self):
        product = self.product(
            unit=PantryProduct.UNIT_GRAM,
            current_quantity=Decimal('200.00'),
            minimum_quantity=Decimal('200.00'),
        )

        forecast = forecast_pantry_product(product, today=self.today)

        self.assertEqual(forecast.suggested_packages, 0)
        self.assertEqual(forecast.suggested_quantity, Decimal('200.00'))

    def test_aligned_shopping_day_marks_today_as_due(self):
        product = self.product(
            current_quantity=Decimal('4.00'),
            minimum_quantity=Decimal('2.00'),
            restock_lead_days=0,
        )
        for days_ago in range(30):
            self.consume(product, 1, days_ago=days_ago)
        expected = forecast_pantry_product(product, today=self.today)
        shopping_weekday = (self.today.weekday() - 1) % 7

        aligned = forecast_pantry_product(
            product,
            today=self.today,
            shopping_weekday=shopping_weekday,
        )

        self.assertGreater(expected.buy_date, self.today)
        self.assertEqual(aligned.buy_date, self.today)
        self.assertTrue(aligned.is_due)

    def test_typical_shopping_weekday_requires_a_clear_pattern(self):
        saturdays = [self.today - timedelta(days=(self.today.weekday() - 5) % 7 + 7 * offset) for offset in range(4)]
        self.assertEqual(infer_typical_shopping_weekday(saturdays), 5)
        self.assertIsNone(infer_typical_shopping_weekday(saturdays[:2]))

    def test_new_product_uses_local_category_prior_normalized_to_its_package(self):
        learned = self.product(
            name='Jogurt uczony',
            category='Nabiał',
            barcode='5900000000103',
            unit=PantryProduct.UNIT_MILLILITER,
            quantity_per_scan=Decimal('200.00'),
            current_quantity=Decimal('2000.00'),
            current_package_count=10,
        )
        new_product = self.product(
            name='Jogurt nowy',
            category='Nabiał',
            barcode='5900000000110',
            unit=PantryProduct.UNIT_MILLILITER,
            quantity_per_scan=Decimal('100.00'),
            current_quantity=Decimal('500.00'),
            current_package_count=5,
        )
        for days_ago in range(30):
            self.consume(learned, 200, days_ago=days_ago, package_count=1)

        forecasts = forecast_pantry_products([learned, new_product], today=self.today)

        self.assertEqual(forecasts[learned.pk].rate, Decimal('200.00'))
        self.assertEqual(forecasts[new_product.pk].status, 'no_history')
        self.assertEqual(forecasts[new_product.pk].rate, Decimal('100.00'))
        self.assertIsNone(forecasts[new_product.pk].buy_date)


class PantryBarcodeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='scanner-user', password='pass12345')
        self.other = User.objects.create_user(username='other-scanner-user', password='pass12345')
        self.client.login(username='scanner-user', password='pass12345')
        self.product = PantryProduct.objects.create(
            user=self.user,
            name='Batoniki',
            barcode='5901234123457',
            quantity_per_scan=Decimal('2.00'),
            category='Produkty suche',
            unit=PantryProduct.UNIT_PIECE,
            current_quantity=Decimal('6.00'),
            current_package_count=3,
        )

    def post_json(self, url_name, payload):
        return self.client.post(
            reverse(url_name),
            data=json.dumps(payload),
            content_type='application/json',
        )

    def test_pantry_page_contains_scanner_flow(self):
        response = self.client.get(reverse('cooking:pantry'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Uruchom skaner')
        self.assertContains(response, 'Wybierz akcję ręcznie')
        self.assertContains(response, 'Nic nie zostanie zapisane automatycznie')
        self.assertContains(response, 'Dodaj wiele')
        self.assertContains(response, 'data-known-count')
        self.assertContains(response, 'capture="environment"')
        self.assertContains(response, 'data-product-photo-camera')
        self.assertContains(response, 'data-product-photo-gallery')
        self.assertContains(response, 'Wybierz z galerii')
        self.assertContains(response, 'data-scanner-panel="photo-camera"')
        self.assertContains(response, 'data-photo-camera-video')
        self.assertContains(response, 'data-photo-camera-capture')
        self.assertContains(response, 'data-scanner-panel="crop"')
        self.assertContains(response, 'data-crop-viewport')
        self.assertContains(response, 'data-countdown-manual="known"')
        self.assertContains(response, 'data-countdown-manual="unknown"')
        self.assertContains(response, reverse('cooking:pantry-barcode-lookup'))
        page_html = response.content.decode()
        self.assertIn('data-countdown="known" hidden', page_html)
        self.assertIn('data-countdown="unknown" hidden', page_html)
        camera_input = page_html.split('data-product-photo-camera', 1)[0].rsplit('<input', 1)[-1]
        gallery_input = page_html.split('data-product-photo-gallery', 1)[0].rsplit('<input', 1)[-1]
        self.assertIn('capture="environment"', camera_input)
        self.assertNotIn('capture=', gallery_input)

    def test_lookup_returns_known_and_unknown_without_mutating_stock(self):
        known = self.client.get(reverse('cooking:pantry-barcode-lookup'), {
            'barcode': self.product.barcode,
        })
        unknown = self.client.get(reverse('cooking:pantry-barcode-lookup'), {
            'barcode': '0000000000001',
        })

        self.assertEqual(known.status_code, 200)
        self.assertEqual(known.json()['status'], 'known')
        self.assertEqual(known.json()['product']['quantity_per_scan'], '2.00')
        self.assertEqual(known.json()['product']['current_package_count'], 3)
        self.assertFalse(known.json()['auto_action_enabled'])
        self.assertIsNone(known.json()['default_action'])
        self.assertEqual(known.json()['timeout_ms'], 5000)
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(unknown.json()['status'], 'unknown')
        self.assertFalse(unknown.json()['auto_action_enabled'])
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_quantity, Decimal('6.00'))
        self.assertFalse(self.product.movements.exists())

    @patch('cooking.views.PANTRY_SCANNER_AUTO_ACTION_TIMEOUT_MS', 7000)
    @patch('cooking.views.PANTRY_SCANNER_AUTO_ACTION_ENABLED', True)
    def test_lookup_can_expose_enabled_automatic_action(self):
        response = self.client.get(reverse('cooking:pantry-barcode-lookup'), {
            'barcode': self.product.barcode,
        })
        page = self.client.get(reverse('cooking:pantry'))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['auto_action_enabled'])
        self.assertEqual(response.json()['default_action'], PantryMovement.CONSUME)
        self.assertEqual(response.json()['timeout_ms'], 7000)
        self.assertContains(page, 'domyślna akcja po 7 s')
        self.assertIn(
            'data-countdown-manual="known" hidden',
            page.content.decode(),
        )

    def test_lookup_does_not_expose_another_users_product(self):
        PantryProduct.objects.create(
            user=self.other,
            name='Cudza kawa',
            barcode='1111111111111',
        )

        response = self.client.get(reverse('cooking:pantry-barcode-lookup'), {
            'barcode': '1111111111111',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'unknown')

    def test_same_barcode_can_be_used_by_different_users(self):
        other_product = PantryProduct.objects.create(
            user=self.other,
            name='Inne batoniki',
            barcode=self.product.barcode,
        )

        self.assertEqual(other_product.barcode, self.product.barcode)

    def test_known_scan_consumes_quantity_per_scan_and_is_idempotent(self):
        scan_id = str(uuid4())
        payload = {
            'barcode': self.product.barcode,
            'scan_id': scan_id,
            'action': PantryMovement.CONSUME,
            'count': 1,
        }

        first = self.post_json('cooking:pantry-barcode-action', payload)
        second = self.post_json('cooking:pantry-barcode-action', payload)

        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()['idempotent_replay'])
        self.assertEqual(first.json()['quantity'], '2.00')
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()['idempotent_replay'])
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_quantity, Decimal('4.00'))
        self.assertEqual(self.product.current_package_count, 2)
        self.assertEqual(self.product.movements.count(), 1)
        self.assertEqual(self.product.movements.get().package_count, 1)

    def test_reusing_scan_id_with_different_payload_is_rejected(self):
        scan_id = str(uuid4())
        first = self.post_json('cooking:pantry-barcode-action', {
            'barcode': self.product.barcode,
            'scan_id': scan_id,
            'action': PantryMovement.PURCHASE,
            'count': 1,
        })
        conflict = self.post_json('cooking:pantry-barcode-action', {
            'barcode': self.product.barcode,
            'scan_id': scan_id,
            'action': PantryMovement.PURCHASE,
            'count': 2,
        })

        self.assertEqual(first.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_quantity, Decimal('8.00'))
        self.assertEqual(self.product.current_package_count, 4)
        self.assertEqual(self.product.movements.count(), 1)

    def test_known_scan_can_add_multiple_packages(self):
        response = self.post_json('cooking:pantry-barcode-action', {
            'barcode': self.product.barcode,
            'scan_id': str(uuid4()),
            'action': PantryMovement.PURCHASE,
            'count': 3,
        })

        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_quantity, Decimal('12.00'))
        self.assertEqual(self.product.current_package_count, 6)
        self.assertEqual(self.product.movements.get().quantity, Decimal('6.00'))
        self.assertEqual(self.product.movements.get().package_count, 3)

    def test_consumption_never_makes_stock_negative(self):
        self.product.current_quantity = Decimal('1.00')
        self.product.current_package_count = 1
        self.product.save(update_fields=['current_quantity', 'current_package_count'])

        response = self.post_json('cooking:pantry-barcode-action', {
            'barcode': self.product.barcode,
            'scan_id': str(uuid4()),
            'action': PantryMovement.CONSUME,
            'count': 1,
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['stock_was_insufficient'])
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_quantity, Decimal('0.00'))
        self.assertEqual(self.product.current_package_count, 0)
        movement = self.product.movements.get()
        self.assertEqual(movement.quantity, Decimal('1.00'))
        self.assertEqual(movement.requested_quantity, Decimal('2.00'))
        self.assertEqual(movement.package_count, 1)
        self.assertEqual(movement.requested_package_count, 1)
        self.assertTrue(movement.stock_was_insufficient)

        replay = self.post_json('cooking:pantry-barcode-action', {
            'barcode': self.product.barcode,
            'scan_id': str(movement.scan_id),
            'action': PantryMovement.CONSUME,
            'count': 1,
        })
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()['idempotent_replay'])
        self.assertEqual(self.product.movements.count(), 1)

    def test_unknown_code_cannot_be_used_as_known_action(self):
        response = self.post_json('cooking:pantry-barcode-action', {
            'barcode': '9999999999999',
            'scan_id': str(uuid4()),
            'action': PantryMovement.CONSUME,
            'count': 1,
        })

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['ok'], False)
        self.assertEqual(PantryMovement.objects.count(), 0)

    def test_register_unknown_code_adds_many_in_one_movement(self):
        response = self.post_json('cooking:pantry-barcode-register', {
            'barcode': '0123456789012',
            'scan_id': str(uuid4()),
            'name': 'Mleko',
            'quantity_per_scan': '1.50',
            'unit': PantryProduct.UNIT_LITER,
            'count': 4,
            'category': 'Nabiał',
        })

        self.assertEqual(response.status_code, 201)
        product = PantryProduct.objects.get(user=self.user, name='Mleko')
        self.assertEqual(product.barcode, '0123456789012')
        self.assertEqual(product.quantity_per_scan, Decimal('1.50'))
        self.assertEqual(product.current_quantity, Decimal('6.00'))
        self.assertEqual(product.current_package_count, 4)
        movement = product.movements.get()
        self.assertEqual(movement.movement_type, PantryMovement.PURCHASE)
        self.assertEqual(movement.quantity, Decimal('6.00'))
        self.assertEqual(movement.package_count, 4)

    def test_register_can_attach_barcode_to_existing_product(self):
        milk = PantryProduct.objects.create(
            user=self.user,
            name='Mleko',
            unit=PantryProduct.UNIT_MILLILITER,
            current_quantity=Decimal('500.00'),
        )
        ProductCatalogEntry.objects.create(
            lookup_barcode='1234567890123',
            status=ProductCatalogEntry.STATUS_FOUND,
            product_type='food',
            product_name='Mleko',
            external_category='en:dairies, en:milks',
            suggested_category='Nabiał',
        )

        response = self.post_json('cooking:pantry-barcode-register', {
            'barcode': '1234567890123',
            'scan_id': str(uuid4()),
            'name': 'mleko',
            'quantity_per_scan': '1',
            'unit': PantryProduct.UNIT_LITER,
            'count': 2,
        })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['created'])
        milk.refresh_from_db()
        self.assertEqual(milk.barcode, '1234567890123')
        self.assertEqual(milk.quantity_per_scan, Decimal('1000.00'))
        self.assertEqual(milk.current_quantity, Decimal('2500.00'))
        self.assertEqual(milk.current_package_count, 3)
        self.assertEqual(milk.category, 'Nabiał')

    def test_register_does_not_overwrite_an_existing_category(self):
        product = PantryProduct.objects.create(
            user=self.user,
            name='Napój własny',
            category='Produkty suche',
            unit=PantryProduct.UNIT_LITER,
        )

        response = self.post_json('cooking:pantry-barcode-register', {
            'barcode': '2345678901234',
            'scan_id': str(uuid4()),
            'name': product.name,
            'quantity_per_scan': '1',
            'unit': PantryProduct.UNIT_LITER,
            'count': 1,
            'category': 'Napoje',
        })

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.category, 'Produkty suche')

    def test_register_is_idempotent(self):
        payload = {
            'barcode': '7654321098765',
            'scan_id': str(uuid4()),
            'name': 'Sok',
            'quantity_per_scan': '1',
            'unit': PantryProduct.UNIT_LITER,
            'count': 2,
        }

        first = self.post_json('cooking:pantry-barcode-register', payload)
        second = self.post_json('cooking:pantry-barcode-register', payload)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()['idempotent_replay'])
        product = PantryProduct.objects.get(user=self.user, name='Sok')
        self.assertEqual(product.current_quantity, Decimal('2.00'))
        self.assertEqual(product.current_package_count, 2)
        # Bez wybranej kategorii podpowiedź bierze się z nazwy.
        self.assertEqual(product.category, 'Napoje')
        self.assertEqual(product.movements.count(), 1)

    def test_invalid_scanner_payloads_do_not_change_data(self):
        invalid_count = self.post_json('cooking:pantry-barcode-action', {
            'barcode': self.product.barcode,
            'scan_id': str(uuid4()),
            'action': PantryMovement.PURCHASE,
            'count': 0,
        })
        invalid_barcode = self.client.get(reverse('cooking:pantry-barcode-lookup'), {
            'barcode': '<script>',
        })
        invalid_json = self.client.post(
            reverse('cooking:pantry-barcode-register'),
            data='{',
            content_type='application/json',
        )
        fractional_count = self.post_json('cooking:pantry-barcode-action', {
            'barcode': self.product.barcode,
            'scan_id': str(uuid4()),
            'action': PantryMovement.PURCHASE,
            'count': 1.5,
        })
        oversized = self.post_json('cooking:pantry-barcode-register', {
            'barcode': '8888888888888',
            'scan_id': str(uuid4()),
            'name': 'Za dużo',
            'quantity_per_scan': '99999999.99',
            'unit': PantryProduct.UNIT_KILOGRAM,
            'count': 2,
        })
        invalid_category = self.post_json('cooking:pantry-barcode-register', {
            'barcode': '7777777777777',
            'scan_id': str(uuid4()),
            'name': 'Nieprawidłowa kategoria',
            'quantity_per_scan': '1',
            'unit': PantryProduct.UNIT_PACKAGE,
            'count': 1,
            'category': '<script>',
        })

        self.assertEqual(invalid_count.status_code, 400)
        self.assertEqual(invalid_barcode.status_code, 400)
        self.assertEqual(invalid_json.status_code, 400)
        self.assertEqual(fractional_count.status_code, 400)
        self.assertEqual(oversized.status_code, 400)
        self.assertEqual(invalid_category.status_code, 400)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_quantity, Decimal('6.00'))
        self.assertFalse(self.product.movements.exists())

    def test_scanner_api_requires_login_and_csrf(self):
        anonymous = Client()
        login_response = anonymous.get(reverse('cooking:pantry-barcode-lookup'), {
            'barcode': self.product.barcode,
        })

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.login(username='scanner-user', password='pass12345')
        csrf_client.get(reverse('cooking:pantry'))
        payload = json.dumps({
            'barcode': self.product.barcode,
            'scan_id': str(uuid4()),
            'action': PantryMovement.PURCHASE,
            'count': 1,
        })
        forbidden = csrf_client.post(
            reverse('cooking:pantry-barcode-action'),
            data=payload,
            content_type='application/json',
        )
        token = csrf_client.cookies['csrftoken'].value
        allowed = csrf_client.post(
            reverse('cooking:pantry-barcode-action'),
            data=payload,
            content_type='application/json',
            HTTP_X_CSRFTOKEN=token,
        )

        self.assertEqual(login_response.status_code, 302)
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(allowed.status_code, 200)

    def test_register_can_save_a_product_photo_and_serve_it_only_to_owner(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(PRIVATE_MEDIA_ROOT=media_root):
            response = self.client.post(reverse('cooking:pantry-barcode-register'), {
                'barcode': '2222222222222',
                'scan_id': str(uuid4()),
                'name': 'Jogurt waniliowy',
                'quantity_per_scan': '200',
                'unit': PantryProduct.UNIT_MILLILITER,
                'count': '5',
                'category': 'Nabiał',
                'image': make_test_image(),
            })

            self.assertEqual(response.status_code, 201)
            product = PantryProduct.objects.get(user=self.user, barcode='2222222222222')
            self.assertTrue(product.image.name.startswith('pantry_product_images/'))
            with self.assertRaises(ValueError):
                _ = product.image.url
            self.assertEqual(product.current_package_count, 5)
            self.assertEqual(product.current_quantity, Decimal('1000.00'))
            self.assertEqual(response.json()['product']['current_package_count'], 5)
            self.assertTrue(response.json()['product']['image_url'])
            self.assertTrue(response.json()['product']['image_upload_url'])

            image_response = self.client.get(reverse('cooking:pantry-product-image', args=[product.id]))
            self.assertEqual(image_response.status_code, 200)
            self.assertEqual(image_response['Content-Type'], 'image/png')

            self.client.logout()
            self.client.login(username='other-scanner-user', password='pass12345')
            hidden_response = self.client.get(reverse('cooking:pantry-product-image', args=[product.id]))
            self.assertEqual(hidden_response.status_code, 404)

    def test_known_product_photo_can_be_added_and_invalid_image_is_rejected(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(PRIVATE_MEDIA_ROOT=media_root):
            invalid = self.client.post(
                reverse('cooking:pantry-product-image-upload', args=[self.product.id]),
                {'image': SimpleUploadedFile('fake.jpg', b'not-an-image', content_type='image/jpeg')},
            )
            self.assertEqual(invalid.status_code, 400)
            self.product.refresh_from_db()
            self.assertFalse(self.product.image)

            uploaded = self.client.post(
                reverse('cooking:pantry-product-image-upload', args=[self.product.id]),
                {'image': make_test_image('batoniki.png')},
            )
            self.assertEqual(uploaded.status_code, 200)
            self.product.refresh_from_db()
            self.assertTrue(self.product.image)
            self.assertEqual(uploaded.json()['product']['image_url'], reverse(
                'cooking:pantry-product-image', args=[self.product.id],
            ))

    def test_photo_upload_endpoint_does_not_allow_editing_another_users_product(self):
        self.client.logout()
        self.client.login(username='other-scanner-user', password='pass12345')

        response = self.client.post(
            reverse('cooking:pantry-product-image-upload', args=[self.product.id]),
            {'image': make_test_image()},
        )

        self.assertEqual(response.status_code, 404)
        self.product.refresh_from_db()
        self.assertFalse(self.product.image)


@override_settings(OPEN_FOOD_FACTS_ENABLED=True, OPEN_FOOD_FACTS_RATE_LIMIT=12)
class ProductCatalogLookupTests(TestCase):
    barcode = '3017624010701'

    def setUp(self):
        self.user = User.objects.create_user(username='catalog-user', password='pass12345')
        self.other = User.objects.create_user(username='catalog-other', password='pass12345')
        self.client.login(username='catalog-user', password='pass12345')

    def lookup(self, barcode=None):
        return self.client.get(reverse('cooking:pantry-barcode-lookup'), {
            'barcode': barcode or self.barcode,
        })

    def catalog_product(self, **overrides):
        product = {
            'code': self.barcode,
            'lang': 'de',
            'product_type': 'food',
            'product_name': 'Nutella',
            'generic_name': 'Krem z orzechami laskowymi i kakao',
            'brands': 'Ferrero',
            'quantity': '400 g',
            'product_quantity': 400,
            'product_quantity_unit': 'g',
            'categories': 'Spreads, Chocolate spreads',
            'categories_tags': ['en:spreads', 'en:chocolate-spreads'],
            'ingredients_text': 'Sugar, hazelnuts, cocoa.',
            'image_front_small_url': (
                'https://images.openfoodfacts.org/images/products/301/762/401/0701/front_de.200.jpg'
            ),
            'last_updated_t': 1785948506,
        }
        product.update(overrides)
        return product

    @patch('cooking.services.product_catalog._cache_catalog_image')
    @patch('cooking.services.product_catalog._request_product')
    def test_unknown_product_is_suggested_and_saved_in_local_catalog(self, request_product, cache_image):
        request_product.return_value = self.catalog_product()

        response = self.lookup()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['status'], 'unknown')
        self.assertEqual(payload['catalog']['status'], 'found')
        self.assertEqual(payload['catalog']['name'], 'Nutella')
        self.assertEqual(payload['catalog']['brand'], 'Ferrero')
        self.assertEqual(payload['catalog']['suggested_quantity_per_scan'], '400.00')
        self.assertEqual(payload['catalog']['suggested_unit'], PantryProduct.UNIT_GRAM)
        # Wpis jest niemiecki (lang=de), a polskiej nazwy brak - taki produkt
        # nie trafia do spiżarni jednym dotknięciem, tylko przez formularz.
        self.assertEqual(payload['catalog']['name_language'], 'de')
        self.assertFalse(payload['catalog']['name_is_polish'])
        self.assertFalse(payload['catalog']['can_auto_register'])
        self.assertFalse(payload['catalog']['remembered'])
        self.assertEqual(response['Cache-Control'], 'private, no-store')

        entry = ProductCatalogEntry.objects.get(lookup_barcode=self.barcode)
        self.assertEqual(entry.product_name, 'Nutella')
        self.assertEqual(entry.description, 'Krem z orzechami laskowymi i kakao')
        self.assertEqual(entry.suggested_category, 'Słodycze i przekąski')
        self.assertEqual(entry.name_language, 'de')
        self.assertEqual(entry.status, ProductCatalogEntry.STATUS_FOUND)
        self.assertTrue(entry.valid_until > timezone.now())
        self.assertEqual(ProductCatalogQuota.objects.get().request_count, 1)
        self.assertFalse(PantryProduct.objects.exists())
        self.assertFalse(PantryMovement.objects.exists())
        cache_image.assert_called_once()

    @patch('cooking.services.product_catalog._cache_catalog_image')
    @patch('cooking.services.product_catalog._request_product')
    def test_canonical_category_tags_use_specific_priority(self, request_product, cache_image):
        scenarios = [
            (
                '5901234567890',
                self.catalog_product(
                    code='5901234567890',
                    product_name='Lody waniliowe',
                    categories='Dairy desserts',
                    categories_tags=[
                        *(f'en:unmapped-category-{index:02d}-with-a-long-name' for index in range(8)),
                        'en:dairies',
                        'en:ice-creams-and-sorbets',
                    ],
                ),
                'Mrożonki',
            ),
            (
                '4006381333931',
                self.catalog_product(
                    code='4006381333931',
                    product_name='Sok jabłkowy',
                    categories='Fruit products',
                    categories_tags=['en:fruits', 'en:beverages', 'en:fruit-juices'],
                ),
                'Napoje',
            ),
            (
                '5012345678900',
                self.catalog_product(
                    code='5012345678900',
                    product_name='Tuńczyk',
                    categories='Fish',
                    categories_tags=['en:fishes', 'en:canned-foods', 'en:canned-fishes'],
                ),
                'Konserwy',
            ),
            (
                '8712345678906',
                self.catalog_product(
                    code='8712345678906',
                    product_type='product',
                    product_name='Płyn do naczyń',
                    categories='Household products',
                    categories_tags=['en:cleaning-products', 'en:dishwashing-products'],
                ),
                'Chemia domowa',
            ),
            (
                '7612345678901',
                self.catalog_product(
                    code='7612345678901',
                    product_type='petfood',
                    product_name='Karma dla kota',
                    categories='Pet food',
                    categories_tags=['en:pet-foods'],
                ),
                'Dla zwierząt',
            ),
            (
                '3212345678908',
                self.catalog_product(
                    code='3212345678908',
                    product_name='Produkt regionalny',
                    categories='Mrożonki',
                    categories_tags=['pl:mrozonki'],
                ),
                'Mrożonki',
            ),
            (
                '2912345678904',
                self.catalog_product(
                    code='2912345678904',
                    product_type='product',
                    product_name='Produkt wielobranżowy',
                    categories='Beverages',
                    categories_tags=['en:beverages'],
                ),
                'Inne',
            ),
        ]
        request_product.side_effect = [product for _, product, _ in scenarios]

        for barcode, _, expected_category in scenarios:
            with self.subTest(barcode=barcode):
                response = self.lookup(barcode)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json()['catalog']['suggested_category'],
                    expected_category,
                )
                entry = ProductCatalogEntry.objects.get(lookup_barcode=barcode)
                self.assertEqual(entry.suggested_category, expected_category)
                self.assertTrue(entry.external_category)
                if expected_category == 'Mrożonki':
                    self.assertTrue(
                        'en:ice-creams-and-sorbets' in entry.external_category
                        or 'Mrożonki' in entry.external_category
                    )
                cached_response = self.lookup(barcode)
                self.assertEqual(cached_response.json()['catalog']['cache_state'], 'hit')
                self.assertEqual(
                    cached_response.json()['catalog']['suggested_category'],
                    expected_category,
                )

        self.assertEqual(request_product.call_count, len(scenarios))
        self.assertEqual(cache_image.call_count, len(scenarios))

    @patch('cooking.services.product_catalog._cache_catalog_image')
    @patch('cooking.services.product_catalog._request_product')
    def test_catalog_cache_is_shared_between_users_without_second_request(self, request_product, cache_image):
        request_product.return_value = self.catalog_product()

        first = self.lookup()
        self.client.logout()
        self.client.login(username='catalog-other', password='pass12345')
        second = self.lookup()

        self.assertEqual(first.json()['catalog']['cache_state'], 'refreshed')
        self.assertEqual(second.json()['catalog']['cache_state'], 'hit')
        self.assertEqual(second.json()['catalog']['name'], 'Nutella')
        request_product.assert_called_once_with(self.barcode)
        cache_image.assert_called_once()

    @patch('cooking.services.product_catalog._request_product')
    def test_not_found_response_is_cached_without_mutating_pantry(self, request_product):
        request_product.side_effect = CatalogProductNotFound()

        first = self.lookup()
        second = self.lookup()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()['catalog']['status'], 'not_found')
        self.assertEqual(second.json()['catalog']['status'], 'not_found')
        request_product.assert_called_once_with(self.barcode)
        self.assertEqual(
            ProductCatalogEntry.objects.get(lookup_barcode=self.barcode).status,
            ProductCatalogEntry.STATUS_NOT_FOUND,
        )
        self.assertFalse(PantryProduct.objects.exists())

    @patch('cooking.services.product_catalog._request_product')
    def test_catalog_timeout_keeps_manual_flow_available(self, request_product):
        request_product.side_effect = CatalogUnavailable()

        response = self.lookup()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'unknown')
        self.assertEqual(response.json()['catalog']['status'], 'unavailable')
        entry = ProductCatalogEntry.objects.get(lookup_barcode=self.barcode)
        self.assertIsNotNone(entry.retry_after)
        self.assertFalse(PantryProduct.objects.exists())

    @patch('cooking.services.product_catalog._request_product')
    def test_alphanumeric_code_skips_external_catalog(self, request_product):
        response = self.lookup('LOCAL-CODE-42')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['catalog']['status'], 'unsupported')
        request_product.assert_not_called()
        self.assertFalse(ProductCatalogEntry.objects.exists())

    @patch('cooking.services.product_catalog._cache_catalog_image')
    @patch('cooking.services.product_catalog._request_product')
    def test_known_local_product_is_enriched_without_mutating_stock(self, request_product, cache_image):
        request_product.return_value = self.catalog_product()
        PantryProduct.objects.create(
            user=self.user,
            name='Produkt lokalny',
            barcode=self.barcode,
            current_quantity=Decimal('3.00'),
        )

        response = self.lookup()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'known')
        self.assertEqual(response.json()['catalog']['status'], 'found')
        self.assertEqual(response.json()['catalog']['name'], 'Nutella')
        self.assertEqual(response.json()['product']['category'], 'Słodycze i przekąski')
        request_product.assert_called_once_with(self.barcode)
        product = PantryProduct.objects.get()
        self.assertEqual(product.current_quantity, Decimal('3.00'))
        self.assertEqual(product.category, 'Słodycze i przekąski')
        self.assertFalse(PantryMovement.objects.exists())

    @patch('cooking.services.product_catalog._cache_catalog_image')
    @patch('cooking.services.product_catalog._request_product')
    def test_known_product_keeps_category_chosen_by_user(self, request_product, cache_image):
        request_product.return_value = self.catalog_product()
        product = PantryProduct.objects.create(
            user=self.user,
            name='Produkt lokalny',
            barcode=self.barcode,
            category='Nabiał',
            current_quantity=Decimal('3.00'),
        )

        response = self.lookup()

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.category, 'Nabiał')
        self.assertEqual(response.json()['product']['category'], 'Nabiał')
        self.assertEqual(product.current_quantity, Decimal('3.00'))
        self.assertFalse(PantryMovement.objects.exists())

    def test_cached_category_is_recomputed_with_current_rules(self):
        ProductCatalogEntry.objects.create(
            lookup_barcode=self.barcode,
            canonical_barcode=self.barcode,
            status=ProductCatalogEntry.STATUS_FOUND,
            product_type='food',
            product_name='Sok jabłkowy',
            external_category='en:fruits, en:beverages, en:fruit-juices',
            suggested_category='Warzywa i owoce',
            fetched_at=timezone.now(),
            valid_until=timezone.now() + timedelta(days=7),
        )

        response = self.lookup()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['catalog']['cache_state'], 'hit')
        self.assertEqual(response.json()['catalog']['suggested_category'], 'Napoje')
        self.assertEqual(
            ProductCatalogEntry.objects.get(lookup_barcode=self.barcode).suggested_category,
            'Napoje',
        )

    @patch('cooking.services.product_catalog._cache_catalog_image')
    @patch('cooking.services.product_catalog._request_product')
    def test_untrusted_image_and_ambiguous_quantity_use_safe_fallbacks(self, request_product, cache_image):
        request_product.return_value = self.catalog_product(
            product_quantity=6,
            product_quantity_unit='pieces',
            image_front_small_url='http://attacker.example/product.jpg',
        )

        response = self.lookup()
        catalog = response.json()['catalog']

        self.assertEqual(catalog['status'], 'found')
        self.assertEqual(catalog['suggested_quantity_per_scan'], '1.00')
        self.assertEqual(catalog['suggested_unit'], PantryProduct.UNIT_PACKAGE)
        self.assertEqual(catalog['image_url'], '')
        cache_image.assert_called_once()

    @override_settings(OPEN_FOOD_FACTS_RATE_LIMIT=1)
    @patch('cooking.services.product_catalog._cache_catalog_image')
    @patch('cooking.services.product_catalog._request_product')
    def test_shared_rate_limit_prevents_excess_upstream_requests(self, request_product, cache_image):
        request_product.return_value = self.catalog_product()

        first = self.lookup()
        second = self.lookup('5901234567890')

        self.assertEqual(first.json()['catalog']['status'], 'found')
        self.assertEqual(second.json()['catalog']['status'], 'unavailable')
        self.assertEqual(second.json()['catalog']['cache_state'], 'rate_limited')
        self.assertEqual(request_product.call_count, 1)

    @patch('cooking.services.product_catalog._request_product')
    def test_stale_found_entry_is_used_when_refresh_fails(self, request_product):
        ProductCatalogEntry.objects.create(
            lookup_barcode=self.barcode,
            canonical_barcode=self.barcode,
            status=ProductCatalogEntry.STATUS_FOUND,
            product_name='Produkt z cache',
            suggested_quantity_per_scan=Decimal('1.00'),
            suggested_unit=PantryProduct.UNIT_PACKAGE,
            fetched_at=timezone.now() - timedelta(days=31),
            valid_until=timezone.now() - timedelta(days=1),
        )
        request_product.side_effect = CatalogUnavailable()

        response = self.lookup()

        self.assertEqual(response.json()['catalog']['status'], 'found')
        self.assertEqual(response.json()['catalog']['cache_state'], 'stale')
        self.assertEqual(response.json()['catalog']['name'], 'Produkt z cache')

    def test_cached_image_is_served_only_to_authenticated_users(self):
        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            entry = ProductCatalogEntry.objects.create(
                lookup_barcode=self.barcode,
                status=ProductCatalogEntry.STATUS_FOUND,
                product_name='Produkt ze zdjęciem',
                image=SimpleUploadedFile('product.jpg', b'local-image', content_type='image/jpeg'),
            )
            url = reverse('cooking:pantry-catalog-image', args=[entry.id])

            response = self.client.get(url)
            anonymous = Client().get(url)

            self.assertEqual(response.status_code, 200)
            # Exhausting a FileResponse emits request_finished. During a
            # PostgreSQL TestCase transaction its regular connection closer
            # would close the class-wide atomic connection between tests.
            request_finished.disconnect(close_old_connections)
            try:
                self.assertEqual(b''.join(response.streaming_content), b'local-image')
            finally:
                request_finished.connect(close_old_connections)
            self.assertEqual(anonymous.status_code, 302)


class ShoppingListTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='shopping-user', password='pass12345')
        self.other = User.objects.create_user(username='other-shopping-user', password='pass12345')
        self.client.login(username='shopping-user', password='pass12345')

    def test_generate_shopping_list_uses_low_pantry_stock(self):
        low_product = PantryProduct.objects.create(
            user=self.user,
            name='Ryż',
            category='Produkty suche',
            unit=PantryProduct.UNIT_KILOGRAM,
            current_quantity=Decimal('0.20'),
            minimum_quantity=Decimal('1.00'),
        )
        PantryProduct.objects.create(
            user=self.user,
            name='Makaron',
            category='Produkty suche',
            unit=PantryProduct.UNIT_PACKAGE,
            current_quantity=Decimal('3.00'),
            minimum_quantity=Decimal('1.00'),
        )
        PantryProduct.objects.create(
            user=self.other,
            name='Cukier',
            unit=PantryProduct.UNIT_KILOGRAM,
            current_quantity=Decimal('0.00'),
            minimum_quantity=Decimal('1.00'),
        )

        response = self.client.post(reverse('cooking:generate-shopping-list'), follow=True)

        self.assertEqual(response.status_code, 200)
        shopping_list = ShoppingList.objects.get(user=self.user)
        self.assertEqual(shopping_list.source, ShoppingList.AUTOMATIC)
        item = shopping_list.items.get()
        self.assertEqual(item.pantry_product, low_product)
        self.assertEqual(item.name, 'Ryż')
        self.assertEqual(item.quantity, Decimal('0.80'))
        self.assertFalse(shopping_list.items.filter(name='Makaron').exists())
        self.assertFalse(shopping_list.items.filter(name='Cukier').exists())

    def test_create_manual_shopping_list_with_multiple_items(self):
        response = self.client.post(reverse('cooking:create-shopping-list'), {
            'title': 'Weekend',
            'item_name': ['Jajka', 'Mleko'],
            'item_quantity': ['6', '1.5'],
            'item_unit': [PantryProduct.UNIT_PIECE, PantryProduct.UNIT_LITER],
            'item_category': ['Nabiał', 'Nabiał'],
            'item_note': ['duże', ''],
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        shopping_list = ShoppingList.objects.get(user=self.user, title='Weekend')
        self.assertEqual(shopping_list.source, ShoppingList.MANUAL)
        self.assertEqual(shopping_list.items.count(), 2)
        self.assertEqual(shopping_list.items.get(name='Jajka').quantity, Decimal('6.00'))

    def test_complete_shopping_list_adds_purchased_items_to_pantry(self):
        product = PantryProduct.objects.create(
            user=self.user,
            name='Mleko',
            category='Nabiał',
            unit=PantryProduct.UNIT_LITER,
            current_quantity=Decimal('1.00'),
        )
        shopping_list = ShoppingList.objects.create(user=self.user, title='Po pracy')
        ShoppingListItem.objects.create(
            shopping_list=shopping_list,
            pantry_product=product,
            name='Mleko',
            quantity=Decimal('2.00'),
            unit=PantryProduct.UNIT_LITER,
            category='Nabiał',
            is_purchased=True,
        )
        ShoppingListItem.objects.create(
            shopping_list=shopping_list,
            name='Chleb',
            quantity=Decimal('1.00'),
            unit=PantryProduct.UNIT_PIECE,
            category='Produkty suche',
            is_purchased=True,
        )

        response = self.client.post(reverse('cooking:complete-shopping-list', args=[shopping_list.id]), follow=True)

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        shopping_list.refresh_from_db()
        bread = PantryProduct.objects.get(user=self.user, name='Chleb')
        self.assertEqual(product.current_quantity, Decimal('3.00'))
        self.assertEqual(product.current_package_count, 0)
        self.assertEqual(bread.current_quantity, Decimal('1.00'))
        self.assertEqual(bread.current_package_count, 1)
        self.assertEqual(shopping_list.status, ShoppingList.COMPLETED)
        self.assertEqual(product.movements.get().movement_type, PantryMovement.PURCHASE)
        self.assertIsNone(product.movements.get().package_count)
        self.assertEqual(bread.movements.get().package_count, 1)
        self.assertEqual(bread.movements.get().note, 'Lista zakupów: Po pracy')

    def test_edit_shopping_list_title(self):
        shopping_list = ShoppingList.objects.create(user=self.user, title='Stara nazwa')

        response = self.client.post(reverse('cooking:edit-shopping-list', args=[shopping_list.id]), {
            'title': 'Nowa nazwa',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        shopping_list.refresh_from_db()
        self.assertEqual(shopping_list.title, 'Nowa nazwa')
        self.assertContains(response, 'Nowa nazwa')

    def test_delete_shopping_list_removes_items(self):
        shopping_list = ShoppingList.objects.create(user=self.user, title='Do usunięcia')
        item = ShoppingListItem.objects.create(
            shopping_list=shopping_list,
            name='Mleko',
            quantity=Decimal('1.00'),
            unit=PantryProduct.UNIT_LITER,
        )

        response = self.client.post(reverse('cooking:delete-shopping-list', args=[shopping_list.id]), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ShoppingList.objects.filter(id=shopping_list.id).exists())
        self.assertFalse(ShoppingListItem.objects.filter(id=item.id).exists())

    def test_other_user_cannot_open_shopping_list(self):
        shopping_list = ShoppingList.objects.create(user=self.other, title='Cudza lista')

        response = self.client.get(reverse('cooking:shopping-list-detail', args=[shopping_list.id]))

        self.assertEqual(response.status_code, 404)

    def test_other_user_cannot_delete_shopping_list(self):
        shopping_list = ShoppingList.objects.create(user=self.other, title='Cudza lista')

        response = self.client.post(reverse('cooking:delete-shopping-list', args=[shopping_list.id]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(ShoppingList.objects.filter(id=shopping_list.id).exists())


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
        self.assertIsNone(movement.package_count)
        self.assertEqual(product.current_package_count, 0)

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
        self.assertEqual(movement.quantity, Decimal('0.00'))
        self.assertEqual(movement.requested_quantity, Decimal('10.00'))
        self.assertTrue(movement.stock_was_insufficient)

    def test_cook_view_records_only_fulfilled_quantity_when_stock_is_insufficient(self):
        product = PantryProduct.objects.create(
            user=self.user,
            name='Masło',
            unit=PantryProduct.UNIT_GRAM,
            current_quantity=Decimal('100.00'),
        )

        response = self.client.post(reverse('cooking:cook'), {
            'product_name': ['Masło'],
            'quantity': ['250'],
            'unit': [PantryProduct.UNIT_GRAM],
            'category': ['Nabiał'],
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        movement = product.movements.get()
        self.assertEqual(product.current_quantity, Decimal('0.00'))
        self.assertEqual(movement.quantity, Decimal('100.00'))
        self.assertEqual(movement.requested_quantity, Decimal('250.00'))
        self.assertTrue(movement.stock_was_insufficient)

    def test_cook_view_records_actual_package_count_change(self):
        product = PantryProduct.objects.create(
            user=self.user,
            name='Jogurt do gotowania',
            barcode='5900000000912',
            unit=PantryProduct.UNIT_MILLILITER,
            quantity_per_scan=Decimal('200.00'),
            current_quantity=Decimal('1000.00'),
            current_package_count=5,
        )

        response = self.client.post(reverse('cooking:cook'), {
            'product_name': ['Jogurt do gotowania'],
            'quantity': ['250'],
            'unit': [PantryProduct.UNIT_MILLILITER],
            'category': ['Nabiał'],
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        movement = product.movements.get()
        self.assertEqual(product.current_quantity, Decimal('750.00'))
        self.assertEqual(product.current_package_count, 4)
        self.assertEqual(movement.package_count, 1)
        self.assertEqual(movement.requested_package_count, 2)

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



class PantryCategoryListTests(SimpleTestCase):
    def test_categories_are_grouped_and_other_is_last(self):
        grouped = [category for _, categories in PANTRY_CATEGORY_GROUPS for category in categories]
        self.assertEqual(list(PANTRY_CATEGORIES), [*grouped, 'Inne'])
        self.assertEqual(len(set(PANTRY_CATEGORIES)), len(PANTRY_CATEGORIES))
        # Stare nazwy muszą zostać - są zapisane jako tekst w produktach.
        for legacy in ['Produkty suche', 'Nabiał', 'Warzywa i owoce', 'Mięso i ryby', 'Mrożonki',
                       'Przyprawy', 'Konserwy', 'Napoje', 'Chemia domowa', 'Inne']:
            self.assertIn(legacy, PANTRY_CATEGORIES)

    def test_options_tag_renders_groups_and_marks_selection(self):
        html = Template('{% load pantry_extras %}{% pantry_category_options value %}').render(
            Context({'value': 'Kosmetyki i higiena'})
        )
        self.assertIn('<optgroup label="Spożywcze">', html)
        self.assertIn('<optgroup label="Dom">', html)
        self.assertIn('<option value="Kosmetyki i higiena" selected>', html)
        self.assertEqual(html.count(' selected'), 1)
        self.assertTrue(html.endswith('<option value="Inne">Inne</option>'))

    def test_options_tag_escapes_selected_value(self):
        html = Template('{% load pantry_extras %}{% pantry_category_options value %}').render(
            Context({'value': '"><script>'})
        )
        self.assertNotIn('<script>', html)
        self.assertNotIn(' selected', html)


class PantryCategoryMappingTests(SimpleTestCase):
    """Mapowanie na łańcuchach tagów takich, jakie zwraca API: tag i wszyscy
    jego przodkowie z taksonomii openfoodfacts-server."""

    TAG_CASES = [
        ('food', ['en:flatbreads', 'en:breads', 'en:cereals-and-potatoes', 'en:cereals-and-their-products',
                  'en:plant-based-foods', 'en:plant-based-foods-and-beverages'], 'Pieczywo'),
        ('food', ['en:bread-crumbs', 'en:breads', 'en:cereals-and-potatoes', 'en:cereals-and-their-products',
                  'en:plant-based-foods', 'en:plant-based-foods-and-beverages'], 'Produkty suche'),
        ('food', ['en:coffee-capsules', 'en:beverage-preparations', 'en:beverages',
                  'en:beverages-and-beverages-preparations', 'en:capsules', 'en:coffees', 'en:hot-beverages',
                  'en:plant-based-foods', 'en:plant-based-foods-and-beverages'], 'Napoje'),
        ('food', ['en:plant-based-milk-alternatives', 'en:beverages', 'en:beverages-and-beverages-preparations',
                  'en:dairy-substitutes', 'en:milk-substitutes', 'en:plant-based-beverages',
                  'en:plant-based-foods-and-beverages'], 'Nabiał'),
        ('food', ['en:frozen-vegetables', 'en:frozen-foods', 'en:frozen-plant-based-foods',
                  'en:fruits-and-vegetables-based-foods', 'en:plant-based-foods',
                  'en:plant-based-foods-and-beverages', 'en:vegetable-based-foods-and-beverages',
                  'en:vegetables-based-foods'], 'Mrożonki'),
        ('food', ['en:dietary-supplements'], 'Leki i apteczka'),
        ('product', ['en:toilet-papers', 'en:home-garden', 'en:household-paper-products',
                     'en:household-supplies'], 'Artykuły papierowe'),
        ('product', ['en:laundry-detergent', 'en:home-garden', 'en:household-supplies',
                     'en:laundry-supplies'], 'Chemia domowa'),
        ('product', ['en:diapers', 'en:baby-toddler', 'en:diapering'], 'Kosmetyki i higiena'),
        ('product', ['en:cat-litter', 'en:animals-pet-supplies', 'en:cat-supplies', 'en:pet-supplies'],
         'Dla zwierząt'),
    ]

    def test_real_taxonomy_chains(self):
        for product_type, tags, expected in self.TAG_CASES:
            with self.subTest(tag=tags[0]):
                self.assertEqual(
                    _suggest_category('', '', '', category_tags=tags, product_type=product_type),
                    expected,
                )

    def test_product_type_defaults(self):
        self.assertEqual(_suggest_category('Coś', '', '', category_tags=[], product_type='beauty'),
                         'Kosmetyki i higiena')
        self.assertEqual(_suggest_category('Coś', '', '', category_tags=[], product_type='petfood'),
                         'Dla zwierząt')
        self.assertEqual(_suggest_category('Ładowarka', '', '', category_tags=[], product_type='product'),
                         'Inne')

    def test_non_food_products_never_get_a_food_category(self):
        self.assertEqual(
            _suggest_category('Masło do ciała', '', '', category_tags=[], product_type='product'),
            'Kosmetyki i higiena',
        )
        self.assertEqual(
            _suggest_category('Ser', '', '', category_tags=['en:cheeses'], product_type='beauty'),
            'Kosmetyki i higiena',
        )

    def test_polish_name_traps(self):
        cases = [
            ('Herbatniki maślane', 'Słodycze i przekąski'),
            ('Herbata czarna', 'Napoje'),
            ('Tabletki do zmywarki', 'Chemia domowa'),
            ('Kapsułki do prania', 'Chemia domowa'),
            ('Masło do ciała', 'Kosmetyki i higiena'),
            ('Mleczko czyszczące', 'Chemia domowa'),
            ('Sos pomidorowy', 'Przyprawy'),
            ('Bułka tarta', 'Produkty suche'),
            ('Serwetki papierowe', 'Artykuły papierowe'),
            ('Ocet balsamiczny', 'Przyprawy'),
            ('Syrop na kaszel', 'Leki i apteczka'),
            ('Syrop malinowy', 'Napoje'),
            ('Chipsy tortilla', 'Słodycze i przekąski'),
            ('Milka czekolada', 'Słodycze i przekąski'),
            ('Karma dla kota z łososiem', 'Dla zwierząt'),
            ('Toilettenpapier', 'Artykuły papierowe'),
            ('Prací gel', 'Chemia domowa'),
        ]
        for name, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    _suggest_category(name, '', '', category_tags=[], product_type='food'),
                    expected,
                )

    def test_keyword_matching_respects_word_boundaries(self):
        self.assertTrue(_text_has_keyword('ser żółty', 'ser'))
        self.assertFalse(_text_has_keyword('serwetki', 'ser'))
        self.assertTrue(_text_has_keyword('mleczko czyszczące', 'czyszcząc*'))
        self.assertFalse(_text_has_keyword('żel oczyszczający', 'czyszcząc*'))
        self.assertTrue(_text_has_keyword('soft paper towels', 'paper towel*'))

    def test_name_prefers_polish_then_english_then_original(self):
        cases = [
            ({'lang': 'de', 'product_name': 'Vollmilch', 'product_name_pl': 'Mleko pełne'}, ('Mleko pełne', 'pl')),
            ({'lang': 'pl', 'product_name': 'Mleko Łaciate'}, ('Mleko Łaciate', 'pl')),
            ({'lang': 'de', 'product_name': 'Spülmittel', 'generic_name_pl': 'Płyn do naczyń'},
             ('Płyn do naczyń', 'pl')),
            ({'lang': 'cs', 'product_name': 'Prací gel', 'product_name_en': 'Laundry gel'}, ('Laundry gel', 'en')),
            ({'lang': 'de', 'product_name': 'Spülmittel'}, ('Spülmittel', 'de')),
            ({}, ('', '')),
        ]
        for product, expected in cases:
            with self.subTest(product=product):
                self.assertEqual(_pick_product_name(product), expected)


@override_settings(OPEN_FOOD_FACTS_ENABLED=True, OPEN_FOOD_FACTS_RATE_LIMIT=12)
class HouseholdCatalogTests(TestCase):
    barcode = '5900498028133'

    def setUp(self):
        self.user = User.objects.create_user(username='dom-a', password='pass12345')
        self.other = User.objects.create_user(username='dom-b', password='pass12345')
        self.client.login(username='dom-a', password='pass12345')

    def register(self, client=None, **overrides):
        payload = {
            'barcode': self.barcode,
            'scan_id': str(uuid4()),
            'name': 'Płyn do naczyń miętowy',
            'category': 'Chemia domowa',
            'quantity_per_scan': '900',
            'unit': PantryProduct.UNIT_MILLILITER,
            'count': 1,
        }
        payload.update(overrides)
        return (client or self.client).post(
            reverse('cooking:pantry-barcode-register'),
            data=json.dumps(payload),
            content_type='application/json',
        )

    def lookup(self, client=None):
        return (client or self.client).get(
            reverse('cooking:pantry-barcode-lookup'), {'barcode': self.barcode},
        )

    def other_client(self):
        client = Client()
        client.login(username='dom-b', password='pass12345')
        return client

    def off_entry(self, **overrides):
        values = {
            'source': ProductCatalogEntry.SOURCE_OPEN_FOOD_FACTS,
            'lookup_barcode': self.barcode,
            'status': ProductCatalogEntry.STATUS_FOUND,
            'product_name': 'Spülmittel Minze',
            'name_language': 'de',
            'brand': 'Ludwik',
            'product_type': 'product',
            'external_category': 'en:dish-detergent-soap',
            'suggested_category': 'Chemia domowa',
            'attribution_url': 'https://world.openproductsfacts.org/product/5900498028133',
            'valid_until': timezone.now() + timedelta(days=30),
            'fetched_at': timezone.now(),
        }
        values.update(overrides)
        return ProductCatalogEntry.objects.create(**values)

    def test_registration_is_remembered_for_the_whole_household(self):
        self.assertEqual(self.register().status_code, 201)

        entry = ProductCatalogEntry.objects.get(
            source=ProductCatalogEntry.SOURCE_HOUSEHOLD, lookup_barcode=self.barcode,
        )
        self.assertEqual(entry.product_name, 'Płyn do naczyń miętowy')
        self.assertEqual(entry.suggested_category, 'Chemia domowa')
        self.assertEqual(entry.suggested_unit, PantryProduct.UNIT_MILLILITER)
        self.assertEqual(entry.suggested_quantity_per_scan, Decimal('900.00'))
        self.assertIsNone(entry.valid_until)

    @patch('cooking.services.product_catalog._request_product')
    def test_remembered_product_wins_over_open_food_facts_without_network(self, request_product):
        self.off_entry(valid_until=timezone.now() - timedelta(days=1))
        self.register()

        response = self.lookup(self.other_client())

        request_product.assert_not_called()
        catalog = response.json()['catalog']
        self.assertEqual(response.json()['status'], 'unknown')  # drugi domownik nie ma go w spiżarni
        self.assertEqual(catalog['name'], 'Płyn do naczyń miętowy')
        self.assertEqual(catalog['suggested_category'], 'Chemia domowa')
        self.assertEqual(catalog['source'], 'household')
        self.assertEqual(catalog['source_label'], 'Zapamiętane w domu')
        self.assertTrue(catalog['remembered'])
        self.assertTrue(catalog['name_is_polish'])
        self.assertTrue(catalog['can_auto_register'])
        # marka i atrybucja nadal z Open Food Facts
        self.assertEqual(catalog['brand'], 'Ludwik')
        self.assertTrue(catalog['attribution_url'].startswith('https://world.openproductsfacts.org/'))

    @patch('cooking.services.product_catalog._request_product')
    def test_memory_survives_deleting_the_product(self, request_product):
        self.register()
        PantryProduct.objects.filter(user=self.user, barcode=self.barcode).delete()

        catalog = self.lookup().json()['catalog']

        request_product.assert_not_called()
        self.assertEqual(catalog['name'], 'Płyn do naczyń miętowy')

    def test_remembered_category_is_not_overridden_by_rules(self):
        self.register(name='Coś nietypowego', category='Napoje')
        entry = ProductCatalogEntry.objects.get(source=ProductCatalogEntry.SOURCE_HOUSEHOLD)
        entry.external_category = 'en:dairies'
        entry.save()

        self.assertEqual(self.lookup().json()['catalog']['suggested_category'], 'Napoje')

    def test_foreign_open_food_facts_name_requires_the_form(self):
        self.off_entry()

        catalog = self.lookup().json()['catalog']

        self.assertEqual(catalog['name'], 'Spülmittel Minze')
        self.assertEqual(catalog['name_language'], 'de')
        self.assertFalse(catalog['name_is_polish'])
        self.assertFalse(catalog['can_auto_register'])
        self.assertFalse(catalog['remembered'])

    def test_manual_add_with_barcode_is_remembered_and_category_suggested(self):
        response = self.client.post(reverse('cooking:add-pantry-product'), {
            'name': 'Papier toaletowy Velvet',
            'barcode': '5901478007780',
            'quantity_per_scan': '1',
            'unit': PantryProduct.UNIT_PACKAGE,
            'current_quantity': '0',
            'minimum_quantity': '0',
            'restock_lead_days': '3',
        })

        self.assertEqual(response.status_code, 302)
        product = PantryProduct.objects.get(user=self.user, barcode='5901478007780')
        self.assertEqual(product.category, 'Artykuły papierowe')
        entry = ProductCatalogEntry.objects.get(
            source=ProductCatalogEntry.SOURCE_HOUSEHOLD, lookup_barcode='5901478007780',
        )
        self.assertEqual(entry.product_name, 'Papier toaletowy Velvet')

    def test_known_product_in_other_category_is_upgraded_on_scan(self):
        self.off_entry()
        product = PantryProduct.objects.create(
            user=self.user, name='Spülmittel', barcode=self.barcode, category='Inne',
        )

        self.lookup()

        product.refresh_from_db()
        self.assertEqual(product.category, 'Chemia domowa')


class EditPantryProductTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='edytor', password='pass12345')
        self.other = User.objects.create_user(username='sasiad', password='pass12345')
        self.client.login(username='edytor', password='pass12345')
        self.product = PantryProduct.objects.create(
            user=self.user,
            name='Spülmittel',
            barcode='4001234567890',
            category='Inne',
            unit=PantryProduct.UNIT_MILLILITER,
            quantity_per_scan=Decimal('500.00'),
            current_quantity=Decimal('1000.00'),
            current_package_count=2,
        )
        self.url = reverse('cooking:edit-pantry-product', args=[self.product.pk])

    def post(self, **overrides):
        data = {
            'name': 'Płyn do naczyń',
            'category': 'Chemia domowa',
            'minimum_quantity': '500',
            'restock_lead_days': '5',
            'notes': 'Kupować większe opakowanie',
        }
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_form_renders_current_values_with_grouped_categories(self):
        self.product.minimum_quantity = Decimal('1.50')
        self.product.save()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="Spülmittel"')
        self.assertContains(response, '<optgroup label="Dom">')
        self.assertContains(response, '<option value="Inne" selected>')
        # Decimal w polu liczbowym bez polskiego przecinka ("1,50" pole odrzuca)
        self.assertContains(response, 'name="minimum_quantity" value="1.50"')
        self.assertNotContains(response, 'value="1,50"')

    def test_edit_updates_product_and_household_memory_without_touching_stock(self):
        response = self.post()

        self.assertRedirects(response, reverse('cooking:pantry'))
        self.product.refresh_from_db()
        self.assertEqual(self.product.name, 'Płyn do naczyń')
        self.assertEqual(self.product.category, 'Chemia domowa')
        self.assertEqual(self.product.minimum_quantity, Decimal('500.00'))
        self.assertEqual(self.product.restock_lead_days, 5)
        self.assertEqual(self.product.current_quantity, Decimal('1000.00'))
        self.assertEqual(self.product.current_package_count, 2)
        self.assertEqual(self.product.unit, PantryProduct.UNIT_MILLILITER)
        entry = ProductCatalogEntry.objects.get(
            source=ProductCatalogEntry.SOURCE_HOUSEHOLD, lookup_barcode='4001234567890',
        )
        self.assertEqual(entry.product_name, 'Płyn do naczyń')
        self.assertEqual(entry.suggested_category, 'Chemia domowa')

    def test_edit_without_barcode_does_not_create_memory(self):
        self.product.barcode = ''
        self.product.save()

        self.post()

        self.assertFalse(ProductCatalogEntry.objects.exists())

    def test_duplicate_name_and_bad_category_are_rejected(self):
        PantryProduct.objects.create(user=self.user, name='Płyn do naczyń')

        self.assertEqual(self.post().status_code, 400)
        self.assertEqual(self.post(name='Inna nazwa', category='Wymyślona').status_code, 400)
        self.product.refresh_from_db()
        self.assertEqual(self.product.name, 'Spülmittel')

    def test_cannot_edit_someone_elses_product(self):
        other_product = PantryProduct.objects.create(user=self.other, name='Cudzy')

        response = self.client.post(
            reverse('cooking:edit-pantry-product', args=[other_product.pk]),
            {'name': 'Przejęty', 'minimum_quantity': '0', 'restock_lead_days': '3'},
        )

        self.assertEqual(response.status_code, 404)
        other_product.refresh_from_db()
        self.assertEqual(other_product.name, 'Cudzy')

    def test_pantry_card_links_to_edit_form(self):
        response = self.client.get(reverse('cooking:pantry'))

        self.assertContains(response, self.url)


class LearnPantryCatalogCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='uczen', password='pass12345')
        self.partner = User.objects.create_user(username='partner', password='pass12345')
        ProductCatalogEntry.objects.create(
            source=ProductCatalogEntry.SOURCE_OPEN_FOOD_FACTS,
            lookup_barcode='5900000000001',
            status=ProductCatalogEntry.STATUS_FOUND,
            product_name='Shampoo',
            product_type='beauty',
            suggested_category='Inne',  # stara reguła wrzucała kosmetyki do "Inne"
        )
        self.shampoo = PantryProduct.objects.create(
            user=self.user, name='Szampon', barcode='5900000000001', category='Inne',
        )
        self.paper = PantryProduct.objects.create(
            user=self.user, name='Papier toaletowy', category='Inne',
        )
        self.chosen = PantryProduct.objects.create(
            user=self.user, name='Mleko', barcode='5900000000002', category='Napoje',
        )

    def run_command(self, *args):
        out = StringIO()
        call_command('learn_pantry_catalog', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        output = self.run_command('--dry-run')

        self.assertIn('Nic nie zostało zapisane', output)
        self.shampoo.refresh_from_db()
        self.assertEqual(self.shampoo.category, 'Inne')
        self.assertFalse(
            ProductCatalogEntry.objects.filter(source=ProductCatalogEntry.SOURCE_HOUSEHOLD).exists()
        )
        self.assertEqual(
            ProductCatalogEntry.objects.get(lookup_barcode='5900000000001').suggested_category, 'Inne',
        )

    def test_run_recategorizes_other_and_remembers_barcodes(self):
        self.run_command()

        self.shampoo.refresh_from_db()
        self.paper.refresh_from_db()
        self.chosen.refresh_from_db()
        self.assertEqual(self.shampoo.category, 'Kosmetyki i higiena')
        self.assertEqual(self.paper.category, 'Artykuły papierowe')
        self.assertEqual(self.chosen.category, 'Napoje')  # świadomy wybór zostaje
        remembered = {
            entry.lookup_barcode: entry
            for entry in ProductCatalogEntry.objects.filter(source=ProductCatalogEntry.SOURCE_HOUSEHOLD)
        }
        self.assertEqual(set(remembered), {'5900000000001', '5900000000002'})
        self.assertEqual(remembered['5900000000001'].suggested_category, 'Kosmetyki i higiena')
        self.assertEqual(remembered['5900000000002'].product_name, 'Mleko')

    def test_latest_product_wins_and_existing_memory_is_kept(self):
        PantryProduct.objects.create(
            user=self.partner, name='Mleko 2%', barcode='5900000000002', category='Nabiał',
        )
        ProductCatalogEntry.objects.create(
            source=ProductCatalogEntry.SOURCE_HOUSEHOLD,
            lookup_barcode='5900000000001',
            status=ProductCatalogEntry.STATUS_FOUND,
            product_name='Szampon pokrzywowy',
            suggested_category='Kosmetyki i higiena',
        )

        self.run_command()

        self.assertEqual(
            ProductCatalogEntry.objects.get(
                source=ProductCatalogEntry.SOURCE_HOUSEHOLD, lookup_barcode='5900000000002',
            ).product_name,
            'Mleko 2%',
        )
        self.assertEqual(
            ProductCatalogEntry.objects.get(
                source=ProductCatalogEntry.SOURCE_HOUSEHOLD, lookup_barcode='5900000000001',
            ).product_name,
            'Szampon pokrzywowy',
        )
