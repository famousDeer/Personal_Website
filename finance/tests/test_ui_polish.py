"""Etap 3 audytu: kontrakt kafelka, zmiana względem poprzedniego okresu,
dane do wykresu i zwinięta sekcja "Dane i uzgodnienia" w maklerze."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.templatetags.ui import zl
from finance.models import BrokerageAccount, Daily, Income, Monthly
from finance.views import _period_change, _previous_month_totals

User = get_user_model()


class ZlFilterTests(TestCase):
    def test_whole_zloty_with_polish_grouping(self):
        self.assertEqual(zl(Decimal('54377.40')), '54 377 zł')
        self.assertEqual(zl(5432), '5432 zł')          # 4 cyfry bez odstępu
        self.assertEqual(zl(1234567.5), '1 234 568 zł')
        self.assertEqual(zl(0), '0 zł')

    def test_negative_uses_minus_sign(self):
        self.assertEqual(zl(-120), '−120 zł')

    def test_non_numeric_passes_through(self):
        self.assertEqual(zl('brak'), 'brak')


class PeriodChangeTests(TestCase):
    def test_no_comparison_without_previous_value(self):
        self.assertIsNone(_period_change(100, 0, higher_is_better=True))
        self.assertIsNone(_period_change(100, None, higher_is_better=True))

    def test_direction_and_judgement(self):
        spending_up = _period_change(150, 100, higher_is_better=False)
        self.assertEqual(spending_up['direction'], 'up')
        self.assertFalse(spending_up['good'])
        self.assertEqual(spending_up['percent'], Decimal('50'))

        income_down = _period_change(80, 100, higher_is_better=True)
        self.assertEqual(income_down['direction'], 'down')
        self.assertFalse(income_down['good'])

        spending_down = _period_change(80, 100, higher_is_better=False)
        self.assertTrue(spending_down['good'])

    def test_tiny_change_is_flat_and_neutral(self):
        flat = _period_change(Decimal('100.3'), 100, higher_is_better=True)
        self.assertEqual(flat['direction'], 'flat')
        self.assertIsNone(flat['good'])


class DashboardComparisonTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='kafelki', password='pass123')
        self.account = self.user.owned_finance_accounts.get(account_type='personal')
        self.client.login(username='kafelki', password='pass123')
        self.june = Monthly.objects.create(
            user=self.user, account=self.account, date=date(2025, 6, 1),
            total_income=Decimal('5000'), total_expense=Decimal('1000'),
        )
        self.july = Monthly.objects.create(
            user=self.user, account=self.account, date=date(2025, 7, 1),
            total_income=Decimal('5000'), total_expense=Decimal('1500'),
        )
        for day, cost in [(3, 400), (20, 600)]:
            Daily.objects.create(
                user=self.user, account=self.account, month=self.june, date=date(2025, 6, day),
                title='Zakupy', category='Jedzenie', store='', cost=cost,
            )
        Daily.objects.create(
            user=self.user, account=self.account, month=self.july, date=date(2025, 7, 5),
            title='Zakupy', category='Jedzenie', store='', cost=1500,
        )
        Income.objects.create(
            user=self.user, account=self.account, month=self.june, date=date(2025, 6, 10),
            title='Pensja', source='Pensja', amount=Decimal('5000'),
        )

    def test_previous_month_same_span_for_month_in_progress(self):
        totals = _previous_month_totals(self.account, date(2025, 7, 1), days_passed=10)
        self.assertEqual(totals['spending'], Decimal('400'))   # tylko 1-10 czerwca
        self.assertEqual(totals['income'], Decimal('5000'))
        self.assertEqual(totals['label'], 'vs 1–10 cze')

    def test_previous_month_whole_month_when_month_complete(self):
        totals = _previous_month_totals(self.account, date(2025, 7, 1), days_passed=31)
        self.assertEqual(totals['spending'], Decimal('1000'))
        self.assertEqual(totals['label'], 'vs cze')

    def test_no_previous_month_record(self):
        self.assertIsNone(_previous_month_totals(self.account, date(2025, 6, 1), days_passed=30))

    def test_dashboard_tiles_show_change_and_month_title(self):
        response = self.client.get(reverse('finance:dashboard'), {'month': '2025-07'})
        self.assertEqual(response.status_code, 200)
        changes = response.context['changes']
        self.assertEqual(changes['label'], 'vs cze')
        self.assertEqual(changes['spending']['direction'], 'up')
        self.assertFalse(changes['spending']['good'])
        self.assertEqual(changes['income']['direction'], 'flat')
        self.assertEqual(response.context['chart_meta'], {'year': 2025, 'month': 7, 'daysPassed': 31})
        self.assertContains(response, 'Lipiec 2025 w skrócie')
        self.assertContains(response, 'u-metric-delta is-bad')
        self.assertContains(response, '1500 zł')
        self.assertContains(response, 'Pokaż jako tabelę')
        self.assertContains(response, 'role="img"')

    def test_first_month_has_no_change_line(self):
        response = self.client.get(reverse('finance:dashboard'), {'month': '2025-06'})
        self.assertEqual(response.context['changes'], {})
        self.assertNotContains(response, 'u-metric-delta')


class BrokerageDisclosureTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='makler', password='pass123')
        self.account = self.user.owned_finance_accounts.get(account_type='personal')
        self.client.login(username='makler', password='pass123')

    def test_everything_in_order(self):
        BrokerageAccount.objects.create(user=self.user, name='IKE', currency='PLN')
        response = self.client.get(reverse('finance:brokerage'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['data_checks']['total'], 0)
        self.assertContains(response, 'id="brokerage-data"')
        self.assertContains(response, 'W porządku')
        self.assertNotContains(response, 'brokerage-data-chip')

    def test_counter_counts_all_unassigned_investments(self):
        month = Monthly.objects.create(
            user=self.user, account=self.account, date=date(2025, 6, 1),
            total_income=0, total_expense=0,
        )
        for day in range(1, 14):
            Daily.objects.create(
                user=self.user, account=self.account, month=month, date=date(2025, 6, day),
                title='Inwestycje', category='Inwestycje', store='', cost=100,
            )
        response = self.client.get(reverse('finance:brokerage'))
        checks = response.context['data_checks']
        self.assertEqual(checks['unassigned'], 13)
        self.assertEqual(len(response.context['unassigned_investments']), 12)
        self.assertContains(response, '13 do sprawdzenia')
        self.assertContains(response, 'Poniżej 12 najnowszych.')
        self.assertContains(response, 'href="#brokerage-data" class="u-chip brokerage-data-chip"')
