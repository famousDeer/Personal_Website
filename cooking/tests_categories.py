"""Kategorie spiżarni i reguły automatycznego wyboru edytowane w aplikacji."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import (
    PantryCategory,
    PantryCategoryRule,
    PantryProduct,
    ProductCatalogEntry,
    ProductGroup,
    Recipe,
    RecipeStep,
    RecipeStepIngredient,
    ShopLayout,
    ShoppingList,
    ShoppingListItem,
)
from .services import categories as service
from .services.product_catalog import _suggest_category, suggest_category_from_name
from .services.shopping_sync import shopping_snapshot

User = get_user_model()


def keyword_rule(category_name):
    return PantryCategoryRule.objects.get(
        kind=PantryCategoryRule.KIND_KEYWORDS, category__name=category_name,
    )


class SeededDataTests(TestCase):
    def test_migration_seeds_current_categories_and_rules(self):
        self.assertEqual(PantryCategory.objects.count(), 15)
        self.assertEqual(PantryCategoryRule.objects.filter(kind='tags').count(), 16)
        self.assertEqual(PantryCategoryRule.objects.filter(kind='keywords').count(), 16)
        self.assertEqual(service.category_names()[-1], 'Inne')
        self.assertEqual([label for label, _ in service.category_groups()], ['Spożywcze', 'Dom'])
        # Produkty suche mają dwie reguły słów: frazy przed Pieczywem i resztę na końcu.
        suche = PantryCategoryRule.objects.filter(kind='keywords', category__name='Produkty suche')
        self.assertEqual(suche.count(), 2)

    def test_empty_tables_fall_back_to_defaults(self):
        PantryCategory.objects.all().delete()
        self.assertIn('Nabiał', service.category_names())
        self.assertEqual(suggest_category_from_name('Mleko 2%'), 'Nabiał')


class KeywordParsingTests(TestCase):
    def test_separators_case_duplicates_and_punctuation(self):
        self.assertEqual(
            service.parse_keywords('Kawa, kawy;\nCOFFEE\nkawa,  Coca-Cola ,jogurt*'),
            ['kawa', 'kawy', 'coffee', 'coca cola', 'jogurt*'],
        )

    def test_rejects_star_inside_and_too_short_words(self):
        with self.assertRaisesMessage(service.CategoryError, 'gwiazdka może stać tylko na końcu'):
            service.parse_keywords('ka*wa')
        with self.assertRaisesMessage(service.CategoryError, 'za krótkie'):
            service.parse_keywords('a*')
        with self.assertRaisesMessage(service.CategoryError, 'co najmniej jedno'):
            service.parse_keywords(' , ')

    def test_tags_drop_language_prefix(self):
        self.assertEqual(service.parse_tags('en:Frozen foods, frozen-foods\npl:mrożonki'), ['frozen-foods', 'mro-onki'])

    def test_keyword_with_hyphen_matches_name_with_space(self):
        self.assertTrue(service.text_has_keyword(service.searchable_text('Coca Cola Zero'), 'coca-cola'))
        self.assertTrue(service.text_has_keyword(service.searchable_text('Coca-Cola'), 'coca cola'))


class CategoryEditingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='kat', password='pass123')

    def test_create_category_is_usable_everywhere(self):
        category = service.create_category('Kawa i herbata', 'food')
        self.assertIn('Kawa i herbata', service.category_names())
        self.assertEqual(service.category_names()[-1], 'Inne')
        food = dict(service.category_groups())['Spożywcze']
        self.assertEqual(food[-1], 'Kawa i herbata')
        self.assertEqual(category.position, 10)

    def test_names_are_unique_case_insensitive_and_reserved(self):
        with self.assertRaisesMessage(service.CategoryError, 'już istnieje'):
            service.create_category('nabiał', 'food')
        for reserved in ('Inne', 'bez kategorii'):
            with self.assertRaisesMessage(service.CategoryError, 'zarezerwowana'):
                service.create_category(reserved, 'home')
        with self.assertRaisesMessage(service.CategoryError, 'grupę'):
            service.create_category('Coś', 'garaz')

    def _usage_fixtures(self, name):
        product = PantryProduct.objects.create(created_by=self.user, name='Mleko', category=name)
        group = ProductGroup.objects.create(name='Mleko 2%', category=name)
        shopping_list = ShoppingList.objects.create(title='Sobota', created_by=self.user)
        item = ShoppingListItem.objects.create(shopping_list=shopping_list, name='Mleko', category=name)
        recipe = Recipe.objects.create(user=self.user, title='Naleśniki', ingredients='-', instructions='-')
        step = RecipeStep.objects.create(recipe=recipe, instruction='Wymieszaj')
        ingredient = RecipeStepIngredient.objects.create(step=step, name='Mleko', category=name)
        entry = ProductCatalogEntry.objects.create(
            lookup_barcode='5900000000001', source=ProductCatalogEntry.SOURCE_HOUSEHOLD,
            status=ProductCatalogEntry.STATUS_FOUND, suggested_category=name,
        )
        shop = ShopLayout.objects.create(name='Lidl', category_order=['Pieczywo', name, 'Napoje'])
        return product, group, item, ingredient, entry, shop

    def test_rename_rewrites_every_stored_name(self):
        fixtures = self._usage_fixtures('Nabiał')
        category = PantryCategory.objects.get(name='Nabiał')
        service.update_category(category, 'Nabiał i jaja', 'food')
        product, group, item, ingredient, entry, shop = fixtures
        for obj in (product, group, item, ingredient):
            obj.refresh_from_db()
            self.assertEqual(obj.category, 'Nabiał i jaja')
        entry.refresh_from_db()
        shop.refresh_from_db()
        self.assertEqual(entry.suggested_category, 'Nabiał i jaja')
        self.assertEqual(shop.category_order, ['Pieczywo', 'Nabiał i jaja', 'Napoje'])
        # Reguły idą za kategorią (klucz obcy), więc podpowiedź ma nową nazwę.
        self.assertEqual(suggest_category_from_name('Mleko 2%'), 'Nabiał i jaja')
        self.assertNotIn('Nabiał', service.category_names())

    def test_delete_moves_usage_and_removes_rules(self):
        product, group, item, ingredient, entry, shop = self._usage_fixtures('Nabiał')
        category = PantryCategory.objects.get(name='Nabiał')
        moved = service.delete_category(category, move_to='Inne')
        self.assertEqual(moved, 1)
        for obj in (product, group, item, ingredient):
            obj.refresh_from_db()
            self.assertEqual(obj.category, 'Inne')
        shop.refresh_from_db()
        # "Inne" zajmuje w sklepie miejsce usuniętej kategorii.
        self.assertEqual(shop.category_order, ['Pieczywo', 'Inne', 'Napoje'])
        self.assertFalse(PantryCategoryRule.objects.filter(category_id=category.pk).exists())
        self.assertNotEqual(suggest_category_from_name('Mleko 2%'), 'Nabiał')

    def test_delete_into_existing_category_and_into_none(self):
        product = PantryProduct.objects.create(created_by=self.user, name='Lody', category='Mrożonki')
        shop = ShopLayout.objects.create(name='Biedronka', category_order=['Mrożonki', 'Słodycze i przekąski'])
        service.delete_category(PantryCategory.objects.get(name='Mrożonki'), move_to='Słodycze i przekąski')
        product.refresh_from_db()
        shop.refresh_from_db()
        self.assertEqual(product.category, 'Słodycze i przekąski')
        self.assertEqual(shop.category_order, ['Słodycze i przekąski'])

        other = PantryProduct.objects.create(created_by=self.user, name='Puszka', category='Konserwy')
        service.delete_category(PantryCategory.objects.get(name='Konserwy'), move_to='')
        other.refresh_from_db()
        self.assertEqual(other.category, '')

    def test_delete_rejects_unknown_target_and_last_category(self):
        category = PantryCategory.objects.get(name='Napoje')
        with self.assertRaises(service.CategoryError):
            service.delete_category(category, move_to='Nie ma takiej')
        with self.assertRaises(service.CategoryError):
            service.delete_category(category, move_to='Napoje')
        PantryCategory.objects.exclude(pk=category.pk).delete()
        with self.assertRaisesMessage(service.CategoryError, 'co najmniej jedna'):
            service.delete_category(category)

    def test_move_category_changes_display_order(self):
        category = PantryCategory.objects.get(name='Nabiał')
        service.move_category(category, -1)
        food = dict(service.category_groups())['Spożywcze']
        self.assertEqual(food[:2], ('Nabiał', 'Pieczywo'))
        self.assertFalse(service.move_category(PantryCategory.objects.get(name='Nabiał'), -1))

    def test_changing_group_controls_non_food_matching(self):
        # Produkt z bazy produktów ogólnych dostaje tylko kategorie z grupy Dom.
        self.assertEqual(_suggest_category('Płyn do naczyń', '', '', product_type='product'), 'Chemia domowa')
        service.update_category(PantryCategory.objects.get(name='Chemia domowa'), 'Chemia domowa', 'food')
        self.assertEqual(_suggest_category('Płyn do naczyń', '', '', product_type='product'), 'Inne')


class RuleEditingTests(TestCase):
    def test_new_category_with_keywords_suggests_itself(self):
        category = service.create_category('Kawa i herbata', 'food')
        self.assertEqual(suggest_category_from_name('Kawa ziarnista'), 'Napoje')
        rule = service.save_rule(category, 'keywords', 'kawa, kawy, coffee, kaffee, káva')
        # Na końcu listy przegrywa z Napojami, które mają słowo "kawa" wyżej.
        self.assertEqual(rule.position, 16)
        self.assertEqual(suggest_category_from_name('Kawa ziarnista'), 'Napoje')
        service.save_rule(category, 'keywords', 'kawa, kawy, coffee, kaffee, káva', position=1, rule=rule)
        self.assertEqual(suggest_category_from_name('Kawa ziarnista'), 'Kawa i herbata')
        self.assertEqual(suggest_category_from_name('Kaffee Crema'), 'Kawa i herbata')
        positions = list(
            PantryCategoryRule.objects.filter(kind='keywords').order_by('position').values_list('position', flat=True)
        )
        self.assertEqual(positions, list(range(17)))

    def test_editing_keywords_of_existing_rule(self):
        self.assertEqual(suggest_category_from_name('Hummus'), '')
        rule = keyword_rule('Konserwy')
        service.save_rule(rule.category, 'keywords', rule.patterns + '\nhummus', rule=rule)
        self.assertEqual(suggest_category_from_name('Hummus klasyczny'), 'Konserwy')
        rule.refresh_from_db()
        self.assertEqual(rule.position, 6)  # miejsce bez zmian

    def test_move_and_delete_rule_reindex_positions(self):
        rule = keyword_rule('Napoje')
        self.assertEqual(suggest_category_from_name('Herbatniki'), 'Słodycze i przekąski')
        for _ in range(3):
            service.move_rule(rule, -1)
        # Napoje nad Słodyczami: "herbat*" łapie teraz "Herbatniki".
        self.assertEqual(suggest_category_from_name('Herbatniki'), 'Napoje')
        service.delete_rule(rule)
        positions = list(
            PantryCategoryRule.objects.filter(kind='keywords').order_by('position').values_list('position', flat=True)
        )
        self.assertEqual(positions, list(range(15)))
        self.assertEqual(suggest_category_from_name('Sok jabłkowy'), 'Warzywa i owoce')

    def test_tag_rules_from_database(self):
        tags = ['en:plant-based-foods', 'en:coffees']
        self.assertEqual(_suggest_category('Lavazza', '', '', category_tags=tags), 'Napoje')
        category = service.create_category('Kawa', 'food')
        service.save_rule(category, 'tags', 'en:coffees', position=1)
        self.assertEqual(_suggest_category('Lavazza', '', '', category_tags=tags), 'Kawa')

    def test_conflicts_report_other_rules(self):
        conflicts = service.keyword_conflicts(['kawa', 'nowe-słowo'])
        self.assertEqual(list(conflicts), ['kawa'])
        self.assertEqual(conflicts['kawa'][0][1], 'Napoje')

    def test_restore_defaults_after_rename_and_delete(self):
        napoje = PantryCategory.objects.get(name='Napoje')
        service.update_category(napoje, 'Picie', 'food')
        service.delete_category(PantryCategory.objects.get(name='Mrożonki'))
        rule = keyword_rule('Picie')
        service.save_rule(rule.category, 'keywords', 'coś', rule=rule)

        created = service.restore_default_rules()

        self.assertEqual(created, 1)  # Mrożonki wróciły
        self.assertEqual(suggest_category_from_name('Sok pomarańczowy'), 'Picie')
        self.assertEqual(suggest_category_from_name('Lody waniliowe'), 'Mrożonki')
        self.assertEqual(PantryCategoryRule.objects.filter(kind='keywords').count(), 16)


@override_settings(PANTRY_CATEGORIES_RECHECK_SECONDS=60)
class TaxonomyCacheTests(TestCase):
    def setUp(self):
        service.invalidate()

    def tearDown(self):
        service.invalidate()

    def test_cached_between_calls_and_refreshed_after_change(self):
        service.taxonomy()
        with self.assertNumQueries(0):
            service.category_names()
            suggest_category_from_name('Mleko')
        service.create_category('Kawa', 'food')
        self.assertIn('Kawa', service.category_names())

    def test_other_worker_change_is_seen_after_recheck_window(self):
        first = service.taxonomy()
        # Inny worker zmienił bazę i wersję w cache - bez sygnału w tym procesie.
        PantryCategory.objects.filter(name='Napoje').update(name='Picie')
        cache.set(service.VERSION_CACHE_KEY, 'inna-wersja', None)
        self.assertIs(service.taxonomy(), first)
        with mock.patch('cooking.services.categories.time.monotonic', return_value=10**9):
            self.assertIn('Picie', service.category_names())


class SnapshotTests(TestCase):
    def test_offline_app_gets_current_categories(self):
        service.create_category('Kawa', 'food')
        groups = shopping_snapshot()['categoryGroups']
        self.assertEqual(groups[0]['label'], 'Spożywcze')
        self.assertIn('Kawa', groups[0]['categories'])
        self.assertEqual(groups[-1], {'label': '', 'categories': ['Inne']})


class CategoryPagesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='domownik', password='pass123')
        self.client.force_login(self.user)

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(reverse('cooking:pantry-categories'))
        self.assertEqual(response.status_code, 302)

    def test_list_and_rules_tab(self):
        PantryProduct.objects.create(created_by=self.user, name='Mleko', category='Nabiał')
        response = self.client.get(reverse('cooking:pantry-categories'))
        self.assertContains(response, 'Nabiał')
        self.assertContains(response, '1 produkt')
        self.assertContains(response, 'Nowa kategoria')
        response = self.client.get(reverse('cooking:pantry-categories'), {'tab': 'reguly', 'sprawdz': 'Herbatniki maślane'})
        self.assertContains(response, 'jak zapisać słowa kluczowe')
        self.assertContains(response, 'mléko')  # przykład innych języków
        self.assertEqual(response.context['test_result']['match'].category, 'Słodycze i przekąski')
        self.assertContains(response, 'pc-rule is-match')

    def test_pantry_page_links_to_categories(self):
        response = self.client.get(reverse('cooking:pantry'))
        self.assertContains(response, reverse('cooking:pantry-categories'))

    def test_create_edit_delete_flow(self):
        response = self.client.post(reverse('cooking:pantry-category-add'), {'name': 'Kawa i herbata', 'group': 'food'}, follow=True)
        self.assertContains(response, 'Dodano kategorię „Kawa i herbata”')
        category = PantryCategory.objects.get(name='Kawa i herbata')

        response = self.client.post(reverse('cooking:pantry-category-add'), {'name': 'kawa i herbata', 'group': 'food'}, follow=True)
        self.assertContains(response, 'już istnieje')

        response = self.client.post(
            reverse('cooking:pantry-category-rule-add'),
            {'rodzaj': 'slowa', 'category': category.pk, 'patterns': 'kawa, herbata', 'position': '1'},
            follow=True,
        )
        self.assertContains(response, 'Dodano regułę')
        self.assertContains(response, 'także w regule')  # "kawa" jest też w Napojach
        self.assertEqual(suggest_category_from_name('Herbata zielona'), 'Kawa i herbata')

        response = self.client.post(
            reverse('cooking:pantry-category-edit', args=[category.pk]), {'name': 'Kawa', 'group': 'food'}, follow=True,
        )
        self.assertContains(response, 'Zmieniono nazwę na „Kawa”')

        PantryProduct.objects.create(created_by=self.user, name='Lavazza', category='Kawa')
        response = self.client.get(reverse('cooking:pantry-category-delete', args=[category.pk]))
        self.assertContains(response, '1 produkt w spiżarni')
        response = self.client.post(
            reverse('cooking:pantry-category-delete', args=[category.pk]), {'move_to': 'Napoje'}, follow=True,
        )
        self.assertContains(response, 'Usunięto kategorię „Kawa”')
        self.assertTrue(PantryProduct.objects.filter(name='Lavazza', category='Napoje').exists())

    def test_invalid_rule_shows_error_and_keeps_input(self):
        category = PantryCategory.objects.get(name='Napoje')
        response = self.client.post(
            reverse('cooking:pantry-category-rule-add'),
            {'rodzaj': 'slowa', 'category': category.pk, 'patterns': 'ka*wa', 'position': '1'},
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'gwiazdka może stać tylko na końcu', status_code=400)
        self.assertContains(response, 'ka*wa', status_code=400)

    def test_rule_move_and_restore(self):
        rule = keyword_rule('Napoje')
        self.client.post(reverse('cooking:pantry-category-rule-move', args=[rule.pk]), {'direction': 'up'})
        rule.refresh_from_db()
        self.assertEqual(rule.position, 11)
        response = self.client.post(reverse('cooking:pantry-category-rules-restore'), follow=True)
        self.assertContains(response, 'Przywrócono domyślne reguły')
        self.assertEqual(keyword_rule('Napoje').position, 12)

    def test_new_category_accepted_by_product_form(self):
        service.create_category('Kawa', 'food')
        response = self.client.get(reverse('cooking:add-pantry-product'))
        self.assertContains(response, '<option value="Kawa">Kawa</option>', html=True)
        response = self.client.post(reverse('cooking:add-pantry-product'), {
            'name': 'Lavazza', 'category': 'Kawa', 'unit': 'opak', 'quantity_per_scan': '1',
            'current_package_count': '1', 'current_quantity': '1', 'minimum_quantity': '0',
            'restock_lead_days': '0',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(PantryProduct.objects.filter(name='Lavazza', category='Kawa').exists())
