"""Etap 2 audytu: powrót w to samo miejsce, „Cofnij” i wspólne potwierdzenia."""
import time
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from cars.models import CarFuelConsumption, Cars, CarService, CarServicePart
from cooking.models import PantryProduct, ProductGroup, ShoppingList, ShoppingListItem
from finance.models import (
    BrokerageAccount,
    BrokerageCashOperation,
    Daily,
    Income,
    InvestmentFunding,
    Monthly,
)
from utils.navigation import safe_next
from utils.undo import SESSION_KEY

User = get_user_model()


class SafeNextTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def post(self, value):
        return self.factory.post('/x/', {'next': value}, HTTP_HOST='testserver')

    def test_accepts_local_path_with_query_and_drops_fragment(self):
        request = self.post('/cooking/pantry/?widok=grupy&q=Jogurt#product-3')
        self.assertEqual(safe_next(request, '/fallback/'), '/cooking/pantry/?widok=grupy&q=Jogurt')

    def test_rejects_other_sites(self):
        for value in ['https://evil.example/', '//evil.example/x', '/\\evil.example', 'javascript:alert(1)', '']:
            with self.subTest(value=value):
                self.assertEqual(safe_next(self.post(value), '/fallback/'), '/fallback/')

    def test_prefix_limits_where_an_action_may_return(self):
        request = self.post('/finance/expenses/')
        self.assertEqual(safe_next(request, '/cooking/pantry/', prefix='/cooking/pantry/'), '/cooking/pantry/')


class UndoTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='dom', password='pass123')
        self.client.login(username='dom', password='pass123')

    def undo_url(self, response_or_client=None):
        client = response_or_client or self.client
        entries = client.session.get(SESSION_KEY, [])
        self.assertTrue(entries, 'Brak wpisu w koszu po usunięciu.')
        return reverse('undo', args=[entries[-1]['token']])


class ExpenseUndoTests(UndoTestCase):
    def setUp(self):
        super().setUp()
        self.account = self.user.owned_finance_accounts.get(account_type='personal')
        self.month = Monthly.objects.create(
            user=self.user, account=self.account, date=date(2026, 9, 1), total_income=0, total_expense=0,
        )
        self.expense = Daily.objects.create(
            user=self.user, account=self.account, date=date(2026, 9, 12), title='Zakupy',
            category='Jedzenie', store='Lidl', cost=Decimal('84.20'), month=self.month,
        )
        self.month.total_expense = Decimal('84.20')
        self.month.save()

    def test_delete_is_immediate_and_offers_undo_back_on_the_same_list(self):
        list_url = reverse('finance:expense_list') + '?month=2026-09&page=1'
        response = self.client.post(
            reverse('finance:delete_expense', args=[self.expense.id]), {'next': list_url},
        )

        self.assertRedirects(response, list_url, fetch_redirect_response=False)
        self.assertFalse(Daily.objects.filter(pk=self.expense.pk).exists())
        self.month.refresh_from_db()
        self.assertEqual(self.month.total_expense, Decimal('0.00'))

        page = self.client.get(list_url)
        self.assertContains(page, 'Usunięto wydatek „Zakupy”.')
        self.assertContains(page, 'Cofnij')
        self.assertContains(page, '/cofnij/')
        self.assertNotContains(page, 'deleteExpenseModal')

    def test_undo_restores_the_same_record_and_month_total(self):
        created_at_fields = Daily.objects.values().get(pk=self.expense.pk)
        self.client.post(reverse('finance:delete_expense', args=[self.expense.id]))

        response = self.client.post(self.undo_url(), {'next': reverse('finance:expense_list')})

        self.assertRedirects(response, reverse('finance:expense_list'), fetch_redirect_response=False)
        restored = Daily.objects.values().get(pk=self.expense.pk)
        self.assertEqual(restored, created_at_fields)
        self.month.refresh_from_db()
        self.assertEqual(self.month.total_expense, Decimal('84.20'))
        self.assertIn('Przywrócono wydatek „Zakupy”.', [str(m) for m in get_messages(response.wsgi_request)])

    def test_undo_token_works_once(self):
        self.client.post(reverse('finance:delete_expense', args=[self.expense.id]))
        url = self.undo_url()
        self.client.post(url)
        response = self.client.post(url, follow=True)

        self.assertEqual(Daily.objects.filter(pk=self.expense.pk).count(), 1)
        self.assertContains(response, 'Tego nie da się już cofnąć')

    def test_undo_expires(self):
        self.client.post(reverse('finance:delete_expense', args=[self.expense.id]))
        url = self.undo_url()
        session = self.client.session
        entries = session[SESSION_KEY]
        entries[-1]['at'] = time.time() - 11 * 60
        session[SESSION_KEY] = entries
        session.save()

        self.client.post(url)

        self.assertFalse(Daily.objects.filter(pk=self.expense.pk).exists())

    def test_other_user_cannot_use_the_token(self):
        self.client.post(reverse('finance:delete_expense', args=[self.expense.id]))
        url = self.undo_url()

        User.objects.create_user(username='gosc', password='pass123')
        self.client.logout()
        self.client.login(username='gosc', password='pass123')
        self.client.post(url)

        self.assertFalse(Daily.objects.filter(pk=self.expense.pk).exists())


class InvestmentExpenseUndoTests(UndoTestCase):
    """Wydatek-inwestycja ciągnie za sobą zasilenie konta maklerskiego."""

    def test_undo_restores_the_funding_and_its_match(self):
        brokerage = BrokerageAccount.objects.create(
            user=self.user, name='XTB PLN', broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD, currency='PLN', external_account_id='1',
        )
        operation = BrokerageCashOperation.objects.create(
            account=brokerage, operation_type=BrokerageCashOperation.DEPOSIT,
            occurred_at=timezone.make_aware(datetime(2026, 8, 8, 12, 0)), amount='1000.00',
            currency='PLN', external_id='cash-1', import_source='xtb',
        )
        self.client.post(reverse('finance:add_expense'), {
            'date': '2026-08-08', 'title': 'Wpłata XTB', 'category': 'Inwestycje', 'store': 'XTB',
            'cost': '1000.00', 'brokerage_account': str(brokerage.id),
        })
        expense = Daily.objects.get(title='Wpłata XTB')
        funding = InvestmentFunding.objects.get(expense=expense)
        self.assertEqual(funding.cash_operation_id, operation.id)

        self.client.post(reverse('finance:delete_expense', args=[expense.id]))
        self.assertFalse(InvestmentFunding.objects.exists())

        self.client.post(self.undo_url())

        restored = InvestmentFunding.objects.get(expense_id=expense.id)
        self.assertEqual(restored.pk, funding.pk)
        self.assertEqual(restored.cash_operation_id, operation.id)


class IncomeUndoTests(UndoTestCase):
    def test_delete_and_undo_income(self):
        account = self.user.owned_finance_accounts.get(account_type='personal')
        month = Monthly.objects.create(user=self.user, account=account, date=date(2026, 9, 1),
                                       total_income=Decimal('5000.00'), total_expense=0)
        income = Income.objects.create(user=self.user, account=account, date=date(2026, 9, 10),
                                       title='Pensja', amount=Decimal('5000.00'), source='Praca', month=month)

        self.client.post(reverse('finance:delete_income', args=[income.id]))
        month.refresh_from_db()
        self.assertEqual(month.total_income, Decimal('0.00'))

        self.client.post(self.undo_url())

        self.assertTrue(Income.objects.filter(pk=income.pk, title='Pensja').exists())
        month.refresh_from_db()
        self.assertEqual(month.total_income, Decimal('5000.00'))


class ShoppingItemUndoTests(UndoTestCase):
    def setUp(self):
        super().setUp()
        self.product = PantryProduct.objects.create(
            created_by=self.user, name='Mleko', unit=PantryProduct.UNIT_PIECE,
            current_quantity=Decimal('1.00'), minimum_quantity=Decimal('2.00'),
        )
        self.shopping_list = ShoppingList.objects.create(created_by=self.user, title='Sobota')
        self.item = ShoppingListItem.objects.create(
            shopping_list=self.shopping_list, pantry_product=self.product, name='Mleko',
            quantity=Decimal('2.00'), unit=PantryProduct.UNIT_PIECE, note='bez laktozy',
        )
        self.detail_url = reverse('cooking:shopping-list-detail', args=[self.shopping_list.id])

    def test_delete_without_question_and_undo_restores_the_same_item(self):
        before = ShoppingListItem.objects.values().get(pk=self.item.pk)
        response = self.client.post(
            reverse('cooking:delete-shopping-item', args=[self.item.id]), {'next': self.detail_url},
        )
        self.assertRedirects(response, self.detail_url, fetch_redirect_response=False)
        self.assertFalse(ShoppingListItem.objects.filter(pk=self.item.pk).exists())

        stamp = ShoppingList.objects.get(pk=self.shopping_list.pk).updated_at
        response = self.client.post(self.undo_url(), {'next': self.detail_url})

        self.assertRedirects(response, f'{self.detail_url}#shopping-item-{self.item.pk}',
                             fetch_redirect_response=False)
        self.assertEqual(ShoppingListItem.objects.values().get(pk=self.item.pk), before)
        self.assertGreater(ShoppingList.objects.get(pk=self.shopping_list.pk).updated_at, stamp)

    def test_toggle_returns_to_the_item_not_the_top(self):
        response = self.client.post(
            reverse('cooking:toggle-shopping-item', args=[self.item.id]), {'next': self.detail_url},
        )
        self.assertRedirects(response, f'{self.detail_url}#shopping-item-{self.item.pk}',
                             fetch_redirect_response=False)

    def test_detail_page_marks_regions_for_background_updates(self):
        page = self.client.get(self.detail_url)
        self.assertContains(page, 'data-partial-default="#shopping-summary, #shopping-complete, #shopping-items, #shopping-pantry-rows"')
        self.assertContains(page, f'id="shopping-item-{self.item.pk}"')
        self.assertContains(page, 'data-optimistic="shopping-toggle"')
        self.assertContains(page, 'data-confirm="Usunąć listę „Sobota”?"')
        self.assertNotContains(page, 'return confirm(')


class ProductGroupUndoTests(UndoTestCase):
    def test_undo_brings_back_the_group_and_its_brands(self):
        group = ProductGroup.objects.create(name='Jogurt naturalny', minimum_packages=2, created_by=self.user)
        brands = [
            PantryProduct.objects.create(created_by=self.user, name=name, unit=PantryProduct.UNIT_PIECE, group=group)
            for name in ['Jogurt Pilos', 'Jogurt Bakoma']
        ]
        shopping_list = ShoppingList.objects.create(created_by=self.user, title='Lista')
        item = ShoppingListItem.objects.create(shopping_list=shopping_list, pantry_group=group, name='Jogurt naturalny')

        self.client.post(reverse('cooking:delete-product-group', args=[group.id]))
        self.assertFalse(PantryProduct.objects.filter(group__isnull=False).exists())

        self.client.post(self.undo_url())

        self.assertTrue(ProductGroup.objects.filter(pk=group.pk, name='Jogurt naturalny').exists())
        for brand in brands:
            brand.refresh_from_db()
            self.assertEqual(brand.group_id, group.pk)
        item.refresh_from_db()
        self.assertEqual(item.pantry_group_id, group.pk)


class PantryMovementReturnTests(UndoTestCase):
    def setUp(self):
        super().setUp()
        self.product = PantryProduct.objects.create(
            created_by=self.user, name='Jajka', unit=PantryProduct.UNIT_PIECE,
            current_quantity=Decimal('6.00'), minimum_quantity=Decimal('2.00'),
        )
        self.url = reverse('cooking:pantry-movement', args=[self.product.id])

    def consume(self, next_url):
        return self.client.post(self.url, {'movement_type': 'consume', 'quantity': '1', 'next': next_url})

    def test_returns_to_the_same_view_filters_and_product(self):
        response = self.consume('/cooking/pantry/?widok=grupy&q=Jaj')
        self.assertRedirects(response, f'/cooking/pantry/?widok=grupy&q=Jaj#product-{self.product.pk}',
                             fetch_redirect_response=False)

    def test_foreign_or_unrelated_next_falls_back_to_pantry(self):
        for value in ['https://evil.example/', '/finance/expenses/']:
            with self.subTest(value=value):
                response = self.consume(value)
                self.assertRedirects(response, f'/cooking/pantry/#product-{self.product.pk}',
                                     fetch_redirect_response=False)

    def test_pantry_card_has_anchor_and_background_form(self):
        page = self.client.get(reverse('cooking:pantry'))
        self.assertContains(page, f'id="product-{self.product.pk}"')
        self.assertContains(page, f'id="manual-{self.product.pk}"')
        self.assertContains(page, 'data-optimistic="pantry-movement"')
        self.assertContains(page, 'id="pantry-metrics"')

    def test_product_delete_uses_the_shared_confirmation(self):
        page = self.client.get(reverse('cooking:edit-pantry-product', args=[self.product.pk]))
        self.assertContains(page, 'data-confirm="Usunąć „Jajka” ze spiżarni?"')
        self.assertContains(page, 'data-confirm-flag')
        # Bez potwierdzenia serwer nadal odmawia.
        self.client.post(reverse('cooking:delete-pantry-product', args=[self.product.pk]), {'confirm': ''})
        self.assertTrue(PantryProduct.objects.filter(pk=self.product.pk).exists())


class CarUndoTests(UndoTestCase):
    def setUp(self):
        super().setUp()
        self.car = Cars.objects.create(user=self.user, brand='Toyota', model='Corolla', year=2020,
                                       odometer=85000, fuel_type='Benzyna', price='72000.00')
        self.first = CarFuelConsumption.objects.create(car=self.car, date=date(2026, 4, 1), fuel_station='Orlen',
                                                       liters='35', price='245', odometer=85000)
        self.second = CarFuelConsumption.objects.create(car=self.car, date=date(2026, 4, 10), fuel_station='BP',
                                                        liters='30', price='210', odometer=85500)
        self.third = CarFuelConsumption.objects.create(car=self.car, date=date(2026, 4, 20), fuel_station='BP',
                                                       liters='32', price='220', odometer=86000)
        from cars.views import recalculate_fuel_consumptions
        recalculate_fuel_consumptions(self.car)
        self.third.refresh_from_db()
        self.consumption = self.third.consumption

    def test_fuel_undo_recalculates_consumption_and_keeps_the_tab(self):
        dashboard = reverse('cars:dashboard', args=[self.car.id])
        response = self.client.post(reverse('cars:delete_fuel', args=[self.car.id, self.second.id]),
                                    {'next': dashboard})
        self.assertRedirects(response, f'{dashboard}#tab=fuel', fetch_redirect_response=False)
        self.third.refresh_from_db()
        self.assertNotEqual(self.third.consumption, self.consumption)

        self.client.post(self.undo_url(), {'next': dashboard})

        self.assertTrue(CarFuelConsumption.objects.filter(pk=self.second.pk).exists())
        self.third.refresh_from_db()
        self.assertEqual(self.third.consumption, self.consumption)

    def test_service_undo_restores_parts(self):
        service = CarService.objects.create(car=self.car, date=date(2026, 5, 1), service_type='Olej',
                                            description='Wymiana', cost='300')
        CarServicePart.objects.create(service=service, name='Filtr', price='40')

        self.client.post(reverse('cars:delete_service', args=[self.car.id, service.id]))
        self.assertFalse(CarServicePart.objects.exists())
        self.client.post(self.undo_url())

        self.assertEqual(list(CarService.objects.get(pk=service.pk).parts.values_list('name', flat=True)), ['Filtr'])

    def test_car_delete_menu_asks_only_for_the_car(self):
        page = self.client.get(reverse('cars:dashboard', args=[self.car.id]))
        self.assertContains(page, 'data-delete-confirm="Usunąć Toyota Corolla?"')
        self.assertContains(page, 'id="dropdownDeleteForm"')
        self.assertNotContains(page, 'confirmDeletePopover')


class NoNativeConfirmTests(TestCase):
    """Wszystkie potwierdzenia idą przez jeden arkusz, nie przez confirm()."""

    def test_pages_do_not_use_browser_confirm(self):
        user = User.objects.create_user(username='dom', password='pass123')
        self.client.login(username='dom', password='pass123')
        ShoppingList.objects.create(created_by=user, title='Lista')
        for url in [reverse('cooking:shopping-list'), reverse('finance:brokerage'),
                    reverse('cooking:recipe-list'), reverse('finance:travels')]:
            with self.subTest(url=url):
                page = self.client.get(url)
                self.assertEqual(page.status_code, 200)
                self.assertNotContains(page, 'return confirm(')
                self.assertContains(page, 'id="app-confirm"')
