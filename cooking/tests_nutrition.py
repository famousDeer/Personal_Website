"""Kalorie i makro: produkty, Open Food Facts, przepisy i dziennik posiłków."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    Food,
    MealEntry,
    NutritionGoal,
    PantryProduct,
    ProductCatalogEntry,
    Recipe,
    RecipeStepIngredient,
)
from .services.foods import load_builtin_foods
from .services.nutrition import (
    REASON_NO_DATA,
    REASON_UNIT,
    REASON_UNKNOWN,
    NutritionIndex,
    recipe_nutrition,
    recipe_portion,
)
from .services.product_catalog import CatalogProductNotFound, CatalogUnavailable, _normalize_product


class NutritionBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='ola', password='pass12345')
        self.other = User.objects.create_user(username='kuba', password='pass12345')
        self.client.force_login(self.user)
        self.rice = PantryProduct.objects.create(
            name='Ryż', unit='g', kcal_100=Decimal('350'), protein_100=Decimal('7'),
            fat_100=Decimal('1'), carbs_100=Decimal('78'),
        )
        self.oil = PantryProduct.objects.create(
            name='Olej', unit='ml', kcal_100=Decimal('900'), fat_100=Decimal('100'),
        )
        self.egg = PantryProduct.objects.create(
            name='Jajko', unit='szt', kcal_100=Decimal('140'), protein_100=Decimal('12.5'),
            fat_100=Decimal('10'), unit_weight=Decimal('60'),
        )

    def recipe(self, ingredients, portions=2, kcal=0):
        recipe = Recipe.objects.create(
            user=self.user, title='Ryż z jajkiem', ingredients='', instructions='',
            portions=portions, kcal=kcal,
        )
        for order, (name, quantity, unit) in enumerate(ingredients, start=1):
            RecipeStepIngredient.objects.create(
                recipe=recipe, order=order, name=name, quantity=Decimal(quantity), unit=unit,
            )
        return recipe


class RecipeNutritionTests(NutritionBase):
    def test_sums_ingredients_and_divides_by_portions(self):
        recipe = self.recipe([
            ('ryz', '200', 'g'),          # 700 kcal; nazwa bez ogonków
            ('Olej', '1', 'lyzka'),       # 15 ml -> 135 kcal
            ('Jajko', '2', 'szt'),        # 120 g -> 168 kcal
            ('Sól', '0', 'do_smaku'),     # pomijana, nie jest brakiem
        ])
        result = recipe_nutrition(recipe, NutritionIndex())
        self.assertTrue(result.complete)
        self.assertEqual(result.total.kcal, Decimal('1003.0'))
        self.assertEqual(result.portion.kcal, Decimal('501.5'))
        self.assertEqual(result.portion.protein, Decimal('14.5'))   # (14 + 15) / 2
        self.assertEqual(result.portion.carbs, Decimal('78.0'))

    def test_missing_ingredients_are_listed_with_reason(self):
        PantryProduct.objects.create(name='Mąka', unit='g')
        self.egg.unit_weight = None
        self.egg.save()
        recipe = self.recipe([
            ('Ryż', '100', 'g'),
            ('Bazylia', '10', 'g'),
            ('Mąka', '100', 'g'),
            ('Ryż', '1', 'szklanka'),     # szklanka produktu liczonego wagą
            ('Jajko', '1', 'szt'),        # sztuka bez wagi
        ])
        result = recipe_nutrition(recipe, NutritionIndex())
        self.assertEqual(result.counted, 1)
        self.assertEqual(result.missing, [
            ('Bazylia', REASON_UNKNOWN), ('Mąka', REASON_NO_DATA),
            ('Ryż', REASON_UNIT), ('Jajko', REASON_UNIT),
        ])

    def test_open_food_facts_fills_in_for_product_without_own_values(self):
        PantryProduct.objects.create(name='Jogurt', unit='g', barcode='5900000000111')
        ProductCatalogEntry.objects.create(
            source=ProductCatalogEntry.SOURCE_OPEN_FOOD_FACTS, lookup_barcode='5900000000111',
            status=ProductCatalogEntry.STATUS_FOUND, kcal_100=Decimal('60'), protein_100=Decimal('4'),
        )
        result = recipe_nutrition(self.recipe([('Jogurt', '200', 'g')], portions=1), NutritionIndex())
        self.assertEqual((result.total.kcal, result.total.protein), (Decimal('120.0'), Decimal('8.0')))

    def test_manual_kcal_wins_but_macros_stay_computed(self):
        recipe = self.recipe([('Ryż', '100', 'g')], portions=1, kcal=999)
        portion, _ = recipe_portion(recipe, NutritionIndex())
        self.assertEqual((portion.kcal, portion.carbs), (Decimal('999'), Decimal('78.0')))

    def test_recipe_list_shows_computed_kcal_and_missing(self):
        self.recipe([('Ryż', '200', 'g'), ('Bazylia', '10', 'g')])
        response = self.client.get(reverse('cooking:recipe-list'))
        self.assertContains(response, '350 kcal')
        self.assertContains(response, 'Kalorie bez: Bazylia.')
        self.assertContains(response, 'Zjadłem porcję')


class CatalogNutritionTests(TestCase):
    def test_nutriments_are_read_from_open_food_facts(self):
        data = _normalize_product({
            'code': '5900000000111', 'product_name': 'Jogurt',
            'nutriments': {
                'energy-kcal_100g': 61.4, 'proteins_100g': '4.3', 'fat_100g': 3,
                'carbohydrates_100g': 'x',
            },
        }, '5900000000111')
        self.assertEqual(data['kcal_100'], Decimal('61.4'))
        self.assertEqual(data['protein_100'], Decimal('4.3'))
        self.assertEqual(data['fat_100'], Decimal('3.0'))
        self.assertIsNone(data['carbs_100'])

    def test_nutrition_block_of_api_v3_is_read_when_nutriments_are_empty(self):
        data = _normalize_product({'code': '1', 'nutriments': {}, 'nutrition': {'aggregated_set': {
            'per': '100g', 'nutrients': {
                'energy-kcal': {'unit': 'kcal', 'value': 60}, 'proteins': {'unit': 'g', 'value': 11},
                'fat': {'unit': 'g', 'value': 0.2}, 'carbohydrates': {'unit': 'g', 'value': 4},
            },
        }}}, '1')
        self.assertEqual(
            (data['kcal_100'], data['protein_100'], data['fat_100'], data['carbs_100']),
            (Decimal('60.0'), Decimal('11.0'), Decimal('0.2'), Decimal('4.0')),
        )
        per_serving = {'code': '1', 'nutrition': {'aggregated_set': {
            'per': 'serving', 'nutrients': {'energy-kcal': {'value': 90}},
        }}}
        self.assertIsNone(_normalize_product(per_serving, '1')['kcal_100'])

    def test_kilojoules_are_converted_when_kcal_missing(self):
        data = _normalize_product({'code': '1', 'nutriments': {'energy-kj_100g': 418.4}}, '1')
        self.assertEqual(data['kcal_100'], Decimal('100.0'))
        self.assertIsNone(_normalize_product({'code': '1'}, '1')['kcal_100'])


class PantryNutritionEditTests(NutritionBase):
    def test_edit_saves_and_clears_nutrition(self):
        url = reverse('cooking:edit-pantry-product', args=[self.rice.pk])
        response = self.client.post(url, {
            'name': 'Ryż', 'kcal_100': '344,5', 'protein_100': '', 'fat_100': '0.7',
            'carbs_100': '77', 'unit_weight': '100',
        })
        self.assertEqual(response.status_code, 302)
        self.rice.refresh_from_db()
        self.assertEqual(self.rice.kcal_100, Decimal('344.5'))
        self.assertIsNone(self.rice.protein_100)
        self.assertEqual(self.rice.unit_weight, Decimal('100.00'))

        response = self.client.post(url, {'name': 'Ryż', 'kcal_100': 'dużo'})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'Kcal na 100 g: podaj liczbę.', status_code=400)


class DiaryTests(NutritionBase):
    def add(self, **data):
        return self.client.post(reverse('cooking:diary-add'), data, follow=True)

    def test_product_entry_is_computed_from_pantry(self):
        self.add(name='Ryż', quantity='150', unit='g', meal='obiad')
        entry = MealEntry.objects.get()
        self.assertEqual((entry.user, entry.date, entry.meal), (self.user, timezone.localdate(), 'obiad'))
        self.assertEqual((entry.kcal, entry.carbs, entry.product), (Decimal('525.0'), Decimal('117.0'), self.rice))

    def test_pieces_use_unit_weight(self):
        self.add(name='Jajko', quantity='2', unit='szt')
        self.assertEqual(MealEntry.objects.get().kcal, Decimal('168.0'))

    def test_manual_kcal_for_unknown_food(self):
        self.add(name='Pizza na mieście', kcal='800', protein='30', meal='kolacja')
        entry = MealEntry.objects.get()
        self.assertEqual((entry.kcal, entry.protein, entry.quantity, entry.product), (Decimal('800.0'), Decimal('30.0'), None, None))

    def test_unknown_or_incomplete_food_is_rejected_with_hint(self):
        response = self.add(name='Pizza na mieście', quantity='1', unit='szt')
        self.assertContains(response, 'Nie znam produktu „Pizza na mieście”.')
        self.egg.unit_weight = None
        self.egg.save()
        response = self.add(name='Jajko', quantity='2', unit='szt')
        self.assertContains(response, 'uzupełnij wagę sztuki')
        self.assertFalse(MealEntry.objects.exists())

    def test_recipe_portion_by_button_and_by_name(self):
        recipe = self.recipe([('Ryż', '200', 'g')])     # 350 kcal na porcję
        self.add(recipe=recipe.pk)
        self.add(name='ryż z jajkiem', quantity='1,5', unit='porcja')
        kcals = list(MealEntry.objects.order_by('id').values_list('kcal', 'recipe_id', 'unit'))
        self.assertEqual(kcals, [(Decimal('350.0'), recipe.pk, 'porcja'), (Decimal('525.0'), recipe.pk, 'porcja')])

    def test_recipe_without_any_calories_is_rejected(self):
        recipe = self.recipe([('Bazylia', '10', 'g')])
        response = self.add(recipe=recipe.pk)
        self.assertContains(response, 'nie ma kalorii')
        self.assertFalse(MealEntry.objects.exists())

    def test_day_page_totals_goal_and_privacy(self):
        today = timezone.localdate()
        MealEntry.objects.create(user=self.user, date=today, meal='obiad', name='Zupa', kcal=Decimal('400'))
        MealEntry.objects.create(user=self.user, date=today, meal='kolacja', name='Kanapka', kcal=Decimal('350'))
        MealEntry.objects.create(user=self.user, date=today - timedelta(days=1), name='Wczorajsze', kcal=Decimal('111'))
        MealEntry.objects.create(user=self.other, date=today, name='Cudze', kcal=Decimal('999'))
        self.client.post(reverse('cooking:diary-goal'), {'kcal': '2000'})
        self.assertEqual(NutritionGoal.objects.get(user=self.user).kcal, 2000)

        response = self.client.get(reverse('cooking:diary'))
        self.assertEqual(response.context['total'].kcal, Decimal('750'))
        self.assertEqual(response.context['remaining'], Decimal('1250'))
        self.assertContains(response, 'Zostało 1\xa0250 kcal')   # separator tysięcy (pl)
        self.assertNotContains(response, 'Cudze')
        # Wczorajsza pozycja jest tylko w „Ostatnio i najczęściej”, nie w posiłkach dnia.
        self.assertEqual([entry.name for meal in response.context['meals'] for entry in meal['entries']], ['Zupa', 'Kanapka'])
        self.assertEqual([entry.name for entry in response.context['recent']], ['Wczorajsze', 'Kanapka', 'Zupa'])

        response = self.client.get(reverse('cooking:diary'), {'date': (today - timedelta(days=1)).isoformat()})
        self.assertContains(response, 'Wczorajsze')

    def test_delete_own_entry_with_undo_and_not_someone_elses(self):
        mine = MealEntry.objects.create(user=self.user, date=timezone.localdate(), name='Zupa', kcal=Decimal('400'))
        theirs = MealEntry.objects.create(user=self.other, date=timezone.localdate(), name='Cudze', kcal=Decimal('1'))
        response = self.client.post(reverse('cooking:diary-delete', args=[mine.pk]), follow=True)
        self.assertContains(response, 'Usunięto z dziennika: Zupa.')
        self.assertFalse(MealEntry.objects.filter(pk=mine.pk).exists())
        self.assertEqual(self.client.post(reverse('cooking:diary-delete', args=[theirs.pk])).status_code, 404)

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('cooking:diary')).status_code, 302)
        self.client.post(reverse('cooking:diary-add'), {'name': 'Ryż', 'quantity': '100', 'unit': 'g'})
        self.assertFalse(MealEntry.objects.exists())


class FoodBaseTests(NutritionBase):
    """Baza produktów spożywczych: lista wbudowana, przepisy, wyszukiwarka dziennika."""

    def setUp(self):
        super().setUp()
        load_builtin_foods()

    def add(self, **data):
        return self.client.post(reverse('cooking:diary-add'), data, follow=True)

    def search(self, query):
        return self.client.get(reverse('cooking:diary-search'), {'q': query}).json()['results']

    def test_builtin_list_loads_once_and_keeps_own_foods(self):
        count = Food.objects.count()
        self.assertGreater(count, 200)
        chicken = Food.objects.get(name='Pierś z kurczaka')
        self.assertEqual((chicken.kcal_100, chicken.protein_100, chicken.source), (Decimal('120.0'), Decimal('22.5'), 'builtin'))
        Food.objects.filter(name='Banan').update(source=Food.SOURCE_OWN, kcal_100=Decimal('1'))
        Food.objects.filter(name='Jabłko').update(kcal_100=Decimal('1'))
        load_builtin_foods()
        self.assertEqual(Food.objects.count(), count)
        self.assertEqual(Food.objects.get(name='Banan').kcal_100, Decimal('1.0'))      # własny zostaje
        self.assertEqual(Food.objects.get(name='Jabłko').kcal_100, Decimal('52.0'))    # wbudowany wraca

    def test_recipe_counts_ingredients_missing_from_pantry_and_household_measures(self):
        butter = PantryProduct.objects.create(name='Masło', unit='g', kcal_100=Decimal('700'))
        recipe = self.recipe([
            ('pierś z kurczaka', '500', 'g'),   # tylko w bazie: 600 kcal
            ('Banan', '2', 'szt'),              # 2 × 120 g × 89 kcal = 213,6
            ('Masło', '1', 'lyzka'),            # kcal ze spiżarni (700), waga łyżki z bazy (14 g) = 98
        ], portions=1)
        result = recipe_nutrition(recipe, NutritionIndex())
        self.assertEqual(result.missing, [])
        self.assertEqual(result.total.kcal, Decimal('911.6'))
        self.assertEqual(butter.kcal_100, Decimal('700'))

    def test_search_merges_pantry_recipes_and_food_base(self):
        self.recipe([('Ryż', '200', 'g')])                       # „Ryż z jajkiem”
        results = self.search('ryz')
        kinds = [(item['kind'], item['name']) for item in results]
        self.assertEqual(kinds[:2], [('product', 'Ryż'), ('recipe', 'Ryż z jajkiem')])
        self.assertIn(('food', 'Ryż biały'), kinds)
        self.assertEqual(results[1]['units'], [{'value': 'porcja', 'label': 'porcja', 'grams': 100.0}])

        chicken = self.search('piers kurcz')[0]
        self.assertEqual((chicken['kind'], chicken['name'], chicken['per_100']['kcal']), ('food', 'Pierś z kurczaka', 120.0))
        self.assertEqual([unit['value'] for unit in chicken['units']], ['szt', 'g'])
        self.assertEqual(chicken['quantity'], 1)
        self.assertEqual(self.search(''), [])
        # Produkt spiżarni o nazwie z bazy zastępuje pozycję bazy.
        self.assertEqual([item['kind'] for item in self.search('jajko') if item['name'] == 'Jajko'], ['product'])

    def test_add_picked_food_product_and_plain_name(self):
        chicken = Food.objects.get(name='Pierś z kurczaka')
        self.add(food=chicken.pk, name='Pierś z kurczaka', quantity='150', unit='g')
        self.add(product=self.egg.pk, name='Jajko', quantity='2', unit='szt')
        self.add(name='banan', quantity='1', unit='szt')
        rows = list(MealEntry.objects.order_by('id').values_list('name', 'kcal', 'food_id', 'product_id'))
        self.assertEqual(rows, [
            ('Pierś z kurczaka', Decimal('180.0'), chicken.pk, None),
            ('Jajko', Decimal('168.0'), None, self.egg.pk),
            ('Banan', Decimal('106.8'), Food.objects.get(name='Banan').pk, None),
        ])
        response = self.add(food=chicken.pk, quantity='1', unit='szklanka')
        self.assertContains(response, 'Podaj ilość w gramach.')

    def test_repeat_copies_own_entry_only(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        mine = MealEntry.objects.create(
            user=self.user, date=yesterday, meal='obiad', name='Zupa', quantity=Decimal('300'), unit='g',
            kcal=Decimal('210'), protein=Decimal('9'),
        )
        theirs = MealEntry.objects.create(user=self.other, date=yesterday, name='Cudze', kcal=Decimal('1'))
        response = self.client.get(reverse('cooking:diary'))
        self.assertContains(response, 'name="repeat" value="%d"' % mine.pk)
        self.assertNotContains(response, 'Cudze')

        self.add(repeat=mine.pk, meal='kolacja')
        copy = MealEntry.objects.get(date=timezone.localdate())
        self.assertEqual((copy.name, copy.kcal, copy.protein, copy.meal, copy.user), ('Zupa', Decimal('210.0'), Decimal('9.0'), 'kolacja', self.user))
        self.assertEqual(self.client.post(reverse('cooking:diary-add'), {'repeat': theirs.pk}).status_code, 404)

    def test_own_food_is_added_and_found(self):
        url = reverse('cooking:food-add')
        self.assertContains(self.client.get(url, {'name': 'Pierogi babci'}), 'value="Pierogi babci"')
        response = self.client.post(url, {'name': 'Pierogi babci', 'kcal_100': '210,5', 'protein_100': '6', 'piece_weight': '40'})
        self.assertRedirects(response, reverse('cooking:diary'), fetch_redirect_response=False)
        food = Food.objects.get(name='Pierogi babci')
        self.assertEqual((food.kcal_100, food.fat_100, food.piece_weight, food.source, food.created_by),
                         (Decimal('210.5'), Decimal('0'), Decimal('40.0'), 'own', self.user))
        self.assertEqual(self.search('pierogi')[0]['name'], 'Pierogi babci')

        for data, text in [
            ({'name': 'pierogi BABCI', 'kcal_100': '1'}, 'W bazie jest już'),
            ({'name': 'Nowe', 'kcal_100': ''}, 'Podaj kcal na 100 g.'),
        ]:
            self.assertContains(self.client.post(url, data), text, status_code=400)
        self.assertEqual(Food.objects.filter(source='own').count(), 1)


@override_settings(OPEN_FOOD_FACTS_ENABLED=True, OPEN_FOOD_FACTS_RATE_LIMIT=12)
class DiaryLabelledProductTests(NutritionBase):
    """Skaner i szukanie po nazwie w Open Food Facts (sieć mockowana)."""

    SKYR = {
        'code': '5902409703887', 'product_name_pl': 'Skyr naturalny', 'brands': 'Fruvita, Biedronka',
        'quantity': '150 g', 'product_quantity': 150, 'product_quantity_unit': 'g',
        'nutriments': {'energy-kcal_100g': 66, 'proteins_100g': 12, 'fat_100g': 0, 'carbohydrates_100g': 4.1},
    }

    def scan(self, barcode):
        return self.client.get(reverse('cooking:diary-barcode'), {'barcode': barcode}).json()

    @patch('cooking.services.product_catalog._request_product')
    def test_scan_saves_open_food_facts_product_in_food_base(self, request_product):
        request_product.return_value = self.SKYR
        data = self.scan('5902409703887')
        food = Food.objects.get(barcode='5902409703887')
        self.assertEqual((food.name, food.kcal_100, food.piece_weight, food.source),
                         ('Skyr naturalny (Fruvita)', Decimal('66.0'), Decimal('150.0'), 'off'))
        self.assertEqual((data['found'], data['result']['kind'], data['result']['id']), (True, 'food', food.pk))
        self.assertEqual([unit['value'] for unit in data['result']['units']], ['szt', 'g'])

        self.scan('5902409703887')                      # drugi skan: z bazy, bez sieci
        self.assertEqual((request_product.call_count, Food.objects.filter(source='off').count()), (1, 1))
        self.client.post(reverse('cooking:diary-add'), {'food': food.pk, 'quantity': '1', 'unit': 'szt'})
        self.assertEqual(MealEntry.objects.get().kcal, Decimal('99.0'))

    @patch('cooking.services.product_catalog._request_product')
    def test_scan_prefers_pantry_product_and_reports_unknown_code(self, request_product):
        self.rice.barcode = '5900000000222'
        self.rice.save()
        data = self.scan('5900000000222')
        self.assertEqual((data['result']['kind'], data['result']['id']), ('product', self.rice.pk))
        request_product.assert_not_called()

        request_product.side_effect = CatalogProductNotFound()
        data = self.scan('5900000000338')
        self.assertEqual((data['found'], data['barcode']), (False, '5900000000338'))
        self.assertIn('Nie znam kodu', data['message'])
        self.assertEqual(self.client.get(reverse('cooking:diary-barcode'), {'barcode': ''}).status_code, 400)

    @patch('cooking.services.product_catalog._request_search')
    def test_name_search_lists_only_products_with_calories(self, request_search):
        request_search.return_value = [
            self.SKYR,
            {'code': '5900000000345', 'product_name': 'Bez kalorii', 'nutriments': {}},
            {'code': 'x', 'product_name': 'Zły kod', 'nutriments': {'energy-kcal_100g': 10}},
        ]
        data = self.client.get(reverse('cooking:diary-search-off'), {'q': 'skyr'}).json()
        self.assertEqual(data['results'], [
            {'barcode': '5902409703887', 'name': 'Skyr naturalny (Fruvita)', 'note': '150 g · 66 kcal / 100 g'},
        ])
        self.assertEqual(request_search.call_args.args[0], 'skyr')
        self.assertFalse(Food.objects.exists())          # zapis dopiero po wyborze

    @patch('cooking.services.product_catalog._request_search')
    def test_name_search_respects_switch_and_errors(self, request_search):
        request_search.side_effect = CatalogUnavailable()
        url = reverse('cooking:diary-search-off')
        self.assertIn('nie odpowiada', self.client.get(url, {'q': 'skyr'}).json()['error'])
        with override_settings(OPEN_FOOD_FACTS_ENABLED=False):
            request_search.reset_mock()
            self.assertEqual(self.client.get(url, {'q': 'skyr'}).json()['results'], [])
            request_search.assert_not_called()
            self.assertNotContains(self.client.get(reverse('cooking:diary')), 'data-off-search-url')
        self.assertContains(self.client.get(reverse('cooking:diary')), 'data-off-search-url')

    def test_own_food_keeps_scanned_barcode(self):
        self.client.post(reverse('cooking:food-add'), {'name': 'Baton', 'kcal_100': '450', 'barcode': '5900000000338'})
        self.assertEqual(self.scan('5900000000338')['result']['name'], 'Baton')
