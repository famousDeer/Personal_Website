"""Wybór sugestii ze spiżarni: tylko zaznaczone, na nową albo istniejącą listę."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import PantryProduct, ProductGroup, ShoppingList, ShoppingListItem

User = get_user_model()


class ShoppingSuggestionPickerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='wybor', password='pass12345')
        self.client.force_login(self.user)
        make = lambda name, **extra: PantryProduct.objects.create(  # noqa: E731
            created_by=self.user, name=name, category='Nabiał', unit=PantryProduct.UNIT_PIECE,
            current_quantity=Decimal('0.00'), **extra,
        )
        self.milk = make('Mleko')
        self.eggs = make('Jajka', minimum_quantity=Decimal('6.00'))
        self.butter = make('Masło')
        self.gift = make('Wino z prezentu', one_off=True)  # nie kupuję ponownie
        self.group = ProductGroup.objects.create(name='Jogurt naturalny', category='Nabiał')
        make('Jogurt Pilos', group=self.group)
        self.url = reverse('cooking:add-shopping-suggestions')

    def keys(self):
        page = self.client.get(reverse('cooking:shopping-list'))
        return {
            suggestion['name']: suggestion
            for group in page.context['suggestion_groups'] for suggestion in group['items']
        }

    def test_page_offers_every_suggestion_checked_and_grouped(self):
        response = self.client.get(reverse('cooking:shopping-list'))
        names = set(self.keys())
        self.assertEqual(names, {'Mleko', 'Jajka', 'Masło', 'Jogurt naturalny'})
        self.assertContains(response, 'name="pick" value="p-%d" checked' % self.milk.pk)
        self.assertContains(response, 'value="g-%d"' % self.group.pk)
        self.assertEqual(response.context['suggestion_groups'][0]['category'], 'Nabiał')
        self.assertContains(response, 'Utwórz ze wszystkich')

    def test_only_picked_go_to_new_list_with_edited_quantity(self):
        response = self.client.post(self.url, {
            'pick': [f'p-{self.milk.pk}', f'g-{self.group.pk}'],
            f'qty_p-{self.milk.pk}': '3',
            'target': 'new',
            'title': 'Wtorek',
        })
        shopping_list = ShoppingList.objects.get(title='Wtorek')
        self.assertRedirects(response, reverse('cooking:shopping-list-detail', args=[shopping_list.pk]))
        items = {item.name: item for item in shopping_list.items.all()}
        self.assertEqual(set(items), {'Mleko', 'Jogurt naturalny'})
        self.assertEqual(items['Mleko'].quantity, Decimal('3.00'))
        self.assertEqual(items['Mleko'].pantry_product, self.milk)
        self.assertEqual(items['Jogurt naturalny'].pantry_group, self.group)
        self.assertEqual(shopping_list.source, ShoppingList.AUTOMATIC)

    def test_add_to_existing_list_skips_what_is_already_there(self):
        shopping_list = ShoppingList.objects.create(created_by=self.user, title='Sobota')
        ShoppingListItem.objects.create(shopping_list=shopping_list, name='mleko', unit='szt')
        response = self.client.post(self.url, {
            'pick': [f'p-{self.milk.pk}', f'p-{self.eggs.pk}'],
            'target': 'existing', 'target_list': str(shopping_list.pk),
        }, follow=True)
        self.assertEqual(sorted(shopping_list.items.values_list('name', flat=True)), ['Jajka', 'mleko'])
        self.assertContains(response, 'Dodano do listy „Sobota”: 1 pozycję.')
        self.assertContains(response, 'Pominięte, bo już są na liście: Mleko.')

    def test_page_marks_suggestions_already_on_each_list(self):
        shopping_list = ShoppingList.objects.create(created_by=self.user, title='Sobota')
        ShoppingListItem.objects.create(shopping_list=shopping_list, name='Masło', pantry_product=self.butter, unit='szt')
        response = self.client.get(reverse('cooking:shopping-list'), {'lista': shopping_list.pk})
        self.assertEqual(response.context['suggestion_target'], str(shopping_list.pk))
        self.assertEqual(self.keys()['Masło']['on_lists'], [shopping_list.pk])
        self.assertEqual(self.keys()['Mleko']['on_lists'], [])

    def test_invalid_quantity_falls_back_to_suggestion(self):
        self.client.post(self.url, {
            'pick': [f'p-{self.milk.pk}'], f'qty_p-{self.milk.pk}': '1.5', 'target': 'new',
        })
        item = ShoppingListItem.objects.get(name='Mleko')
        self.assertEqual(item.quantity, Decimal('1.00'))  # szt. muszą być całe

    def test_nothing_picked_or_stale_key(self):
        response = self.client.post(self.url, {'target': 'new'}, follow=True)
        self.assertContains(response, 'Zaznacz co najmniej jeden produkt.')
        response = self.client.post(self.url, {'pick': [f'p-{self.gift.pk}', 'p-999999'], 'target': 'new'}, follow=True)
        self.assertContains(response, 'nie są już potrzebne')
        self.assertFalse(ShoppingList.objects.exists())

    def test_completed_list_is_refused(self):
        shopping_list = ShoppingList.objects.create(created_by=self.user, title='Stara', status=ShoppingList.COMPLETED)
        response = self.client.post(self.url, {
            'pick': [f'p-{self.milk.pk}'], 'target': 'existing', 'target_list': str(shopping_list.pk),
        }, follow=True)
        self.assertContains(response, 'została zakończona')
        self.assertFalse(shopping_list.items.exists())

    def test_detail_page_links_to_picker_for_this_list(self):
        shopping_list = ShoppingList.objects.create(created_by=self.user, title='Sobota')
        response = self.client.get(reverse('cooking:shopping-list-detail', args=[shopping_list.pk]))
        self.assertContains(response, f'{reverse("cooking:shopping-list")}?lista={shopping_list.pk}#sugestie')
