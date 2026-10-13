"""Kategorie wydatków i źródła przychodów są wspólne dla wszystkich domowników."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from finance.models import Daily, Income, Monthly

User = get_user_model()


class SharedCategoriesTests(TestCase):
    def setUp(self):
        self.ala = User.objects.create_user(username='ala', password='pass123')
        self.bob = User.objects.create_user(username='bob', password='pass123')
        account = self.ala.owned_finance_accounts.get(account_type='personal')
        month = Monthly.objects.create(user=self.ala, account=account, date=date(2026, 10, 1),
                                       total_income=Decimal('0'), total_expense=Decimal('0'))
        Daily.objects.create(user=self.ala, account=account, month=month, date=date(2026, 10, 2),
                             title='Serwis roweru', category='Rower', cost=Decimal('120'))
        Income.objects.create(user=self.ala, account=account, month=month, date=date(2026, 10, 3),
                              title='Lekcja', source='Korepetycje', amount=Decimal('80'))
        self.client.force_login(self.bob)

    def test_other_users_custom_category_and_source_are_suggested(self):
        response = self.client.get(reverse('finance:add_expense'))
        self.assertIn('Rower', response.context['categories'])
        response = self.client.get(reverse('finance:add_income'))
        self.assertContains(response, '<option value="Korepetycje"', html=False)
        response = self.client.get(reverse('finance:import_bank_transactions'))
        self.assertIn('Rower', response.context['expense_categories'])
        self.assertIn('Korepetycje', response.context['income_sources'])
        response = self.client.get(reverse('finance:import_rules') + '?rodzaj=przychody')
        self.assertEqual(response.status_code, 200)

    def test_edit_income_keeps_custom_source_selected(self):
        account = self.bob.owned_finance_accounts.get(account_type='personal')
        month = Monthly.objects.create(user=self.bob, account=account, date=date(2026, 10, 1),
                                       total_income=Decimal('0'), total_expense=Decimal('0'))
        income = Income.objects.create(user=self.bob, account=account, month=month, date=date(2026, 10, 5),
                                       title='Zwrot za bilety', source='Zwrot od znajomych', amount=Decimal('50'))
        response = self.client.get(reverse('finance:edit_income', args=[income.id]))
        self.assertContains(response, 'value="Zwrot od znajomych"', html=False)
        self.assertContains(response, 'selected', html=False)

    def test_built_in_categories_stay(self):
        response = self.client.get(reverse('finance:add_expense'))
        self.assertIn('Inne', response.context['categories'])
