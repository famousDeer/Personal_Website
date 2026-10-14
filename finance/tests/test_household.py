"""Analiza domowa: zakres kont ze zgodą, minimum na życie, klasyfikacja kategorii."""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from finance.account_utils import TRANSFER_INCOME_SOURCE, TRANSFER_TO_SHARED_CATEGORY
from finance.household import build_household_analysis, household_scope, save_category_kinds
from finance.models import CategoryKind, Daily, FinanceAccount, Income, Monthly

User = get_user_model()
TODAY = date(2026, 10, 14)


class HouseholdTestCase(TestCase):
    def setUp(self):
        self.dawid = User.objects.create_user(username='dawid', password='pass123')
        self.ania = User.objects.create_user(username='ania', first_name='Ania', password='pass123')
        self.obcy = User.objects.create_user(username='obcy', password='pass123')
        self.dawid_account = self.dawid.owned_finance_accounts.get(account_type='personal')
        self.ania_account = self.ania.owned_finance_accounts.get(account_type='personal')
        self.obcy_account = self.obcy.owned_finance_accounts.get(account_type='personal')
        self.shared = FinanceAccount.objects.create(name='Wspólne mBank', account_type=FinanceAccount.SHARED, owner=self.dawid)
        self.shared.members.add(self.dawid, self.ania)
        self._months = {}

    def month(self, user, account, day):
        key = (account.pk, day.replace(day=1))
        if key not in self._months:
            self._months[key] = Monthly.objects.create(user=user, account=account, date=day.replace(day=1),
                                                       total_income=0, total_expense=0)
        return self._months[key]

    def expense(self, user, account, day, category, cost, title='Wydatek'):
        return Daily.objects.create(user=user, account=account, month=self.month(user, account, day), date=day,
                                    title=title, category=category, cost=Decimal(cost))

    def income(self, user, account, day, source, amount, **extra):
        return Income.objects.create(user=user, account=account, month=self.month(user, account, day), date=day,
                                     title='Wpływ', source=source, amount=Decimal(amount), **extra)

    def seed(self):
        d, a = self.dawid, self.ania
        self.expense(d, self.dawid_account, date(2026, 7, 3), 'Zakupy spozywcze', '1000')
        self.expense(d, self.dawid_account, date(2026, 7, 9), 'Jedzenie na miescie', '300')
        self.expense(d, self.dawid_account, date(2026, 8, 1), 'Rachunki', '500')
        transfer = self.expense(d, self.dawid_account, date(2026, 8, 2), TRANSFER_TO_SHARED_CATEGORY, '2000')
        self.income(d, self.shared, date(2026, 8, 2), TRANSFER_INCOME_SOURCE, '2000', linked_expense=transfer)
        self.expense(d, self.shared, date(2026, 8, 10), 'Rachunki', '700')
        self.expense(d, self.dawid_account, date(2026, 9, 5), 'Inwestycje', '400')
        self.expense(d, self.dawid_account, date(2026, 9, 6), 'Delegacje', '200')
        self.income(d, self.dawid_account, date(2026, 7, 10), 'Pensja', '6000')
        self.expense(a, self.ania_account, date(2026, 9, 12), 'Zakupy spozywcze', '800', title='Sekretny prezent')
        self.income(a, self.ania_account, date(2026, 9, 10), 'Pensja', '5000')
        # Bieżący miesiąc i obca osoba - poza analizą.
        self.expense(d, self.dawid_account, date(2026, 10, 2), 'Zakupy spozywcze', '999')
        self.obcy_account.share_in_household_analysis = True
        self.obcy_account.save()
        self.expense(self.obcy, self.obcy_account, date(2026, 8, 5), 'Zakupy spozywcze', '5555')


class ScopeTests(HouseholdTestCase):
    def test_partner_only_with_consent_and_shared_account(self):
        scope = household_scope(self.dawid)
        roles = [(entry.label, entry.role, entry.included) for entry in scope.entries]
        self.assertEqual(roles, [('Ty (dawid)', 'me', True), ('Wspólne mBank', 'shared', True), ('Ania', 'partner', False)])
        self.assertEqual([entry.label for entry in scope.missing_partners], ['Ania'])

        self.ania_account.share_in_household_analysis = True
        self.ania_account.save()
        scope = household_scope(self.dawid)
        self.assertTrue(scope.shares_partner_data)
        self.assertNotIn('obcy', [entry.label for entry in scope.entries])   # zgoda bez wspólnego konta

    def test_without_shared_account_only_own_data(self):
        scope = household_scope(self.obcy)
        self.assertEqual([(entry.role, entry.included) for entry in scope.entries], [('me', True)])
        self.assertFalse(scope.has_partner)


class AnalysisTests(HouseholdTestCase):
    def analysis(self, months=3):
        return build_household_analysis(self.dawid, months, today=TODAY)

    def test_minimum_and_totals(self):
        self.seed()
        self.ania_account.share_in_household_analysis = True
        self.ania_account.save()
        result = self.analysis()
        self.assertEqual((result['start'], result['end']), (date(2026, 7, 1), date(2026, 10, 1)))
        self.assertEqual(result['active_months'], 3)
        # Niezbędne: 1000 + 500 + 700 (konto wspólne) + 800 (Ania) = 3000 / 3 mies.
        self.assertEqual(result['minimum'], Decimal('1000.00'))
        self.assertEqual((result['minimum_low'], result['minimum_high']), (Decimal('800.00'), Decimal('1200.00')))
        self.assertEqual(result['spending'], Decimal('1100.00'))          # + 300 jedzenie na mieście
        self.assertEqual(result['savings'], Decimal('133.33'))
        self.assertEqual(result['excluded'], Decimal('66.67'))            # delegacje; przelew na wspólne wcale
        self.assertEqual(result['income'], Decimal('3666.67'))            # 6000 + 5000, bez wpływu z przelewu
        self.assertEqual(result['savings_rate'], Decimal('70.0'))
        self.assertEqual([item['amount'] for item in result['reserves']], [Decimal('3000.00'), Decimal('6000.00')])
        self.assertEqual([group['kind'] for group in result['groups']], ['essential', 'lifestyle', 'savings', 'excluded'])
        groceries = result['groups'][0]['rows'][0]
        self.assertEqual((groceries['name'], groceries['avg']), ('Zakupy spozywcze', Decimal('600.00')))
        self.assertEqual([item['avg'] for item in groceries['people']],
                         [Decimal('333.33'), Decimal('0.00'), Decimal('266.67')])
        self.assertEqual(result['people'], ['Ty (dawid)', 'Wspólne mBank', 'Ania'])
        self.assertEqual(result['chart']['essential'], [1000.0, 1200.0, 800.0])

    def test_partner_data_excluded_without_consent(self):
        self.seed()
        result = self.analysis()
        self.assertEqual(result['minimum'], Decimal('733.33'))            # (1000 + 500 + 700) / 3
        self.assertEqual(result['people'], ['Ty (dawid)', 'Wspólne mBank'])

    def test_average_over_months_with_data(self):
        self.expense(self.dawid, self.dawid_account, date(2026, 9, 3), 'Rachunki', '900')
        result = self.analysis(months=12)
        self.assertEqual(result['active_months'], 1)
        self.assertEqual(result['minimum'], Decimal('900.00'))

    def test_category_kinds_are_household_wide(self):
        self.seed()
        changed = save_category_kinds({'Jedzenie na miescie': 'essential', TRANSFER_TO_SHARED_CATEGORY: 'essential',
                                       'Rachunki': 'essential', 'Zakupy spozywcze': 'bogus'}, self.ania)
        self.assertEqual(changed, 1)                                      # tylko faktyczna zmiana
        self.assertEqual(CategoryKind.objects.get().updated_by, self.ania)
        self.assertEqual(self.analysis()['minimum'], Decimal('833.33'))  # + 300 jedzenie na mieście


class ViewTests(HouseholdTestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.client.force_login(self.dawid)

    def get(self, **params):
        with mock.patch('finance.household.timezone.localdate', return_value=TODAY):
            return self.client.get(reverse('finance:household_analysis'), params)

    def test_page(self):
        response = self.get(okres=3)
        self.assertContains(response, 'Minimum na życie')
        self.assertContains(response, '733\xa0zł')
        self.assertContains(response, 'Ania')
        self.assertContains(response, 'jeszcze nie udostępnia')
        self.assertNotContains(response, '5555')
        self.assertContains(response, 'js/household-analysis.js')

    def test_partner_consent_shows_sums_but_never_titles(self):
        self.client.force_login(self.ania)
        response = self.client.post(reverse('finance:household_analysis'), {'action': 'share', 'share': '1', 'okres': '3'})
        self.assertRedirects(response, reverse('finance:household_analysis') + '?okres=3#udostepnianie',
                             fetch_redirect_response=False)
        self.ania_account.refresh_from_db()
        self.assertTrue(self.ania_account.share_in_household_analysis)

        self.client.force_login(self.dawid)
        response = self.get(okres=3)
        self.assertContains(response, '1000\xa0zł')
        self.assertNotContains(response, 'Sekretny prezent')

    def test_share_changes_only_own_account(self):
        self.client.post(reverse('finance:household_analysis'), {'action': 'share', 'share': '1'})
        self.dawid_account.refresh_from_db()
        self.ania_account.refresh_from_db()
        self.assertTrue(self.dawid_account.share_in_household_analysis)
        self.assertFalse(self.ania_account.share_in_household_analysis)

    def test_save_kinds_from_form(self):
        response = self.client.post(reverse('finance:household_analysis'), {
            'action': 'kinds', 'okres': '3',
            'category_1_1': 'Jedzenie na miescie', 'kind_1_1': 'essential',
        }, follow=True)
        self.assertContains(response, 'Zapisano rodzaj 1 kategorii')
        self.assertEqual(CategoryKind.objects.get(category='Jedzenie na miescie').kind, 'essential')

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('finance:household_analysis'))
        self.assertEqual(response.status_code, 302)

    def test_index_links_to_analysis(self):
        self.assertContains(self.client.get(reverse('finance:index')), reverse('finance:household_analysis'))
