from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from finance.bank_import import import_candidates_from_post
from finance.models import (
    BrokerageAccount,
    BrokerageCashOperation,
    Daily,
    InvestmentFunding,
)


User = get_user_model()


class InvestmentFundingFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='investor', password='pass123')
        self.finance_account = self.user.owned_finance_accounts.get(account_type='personal')
        self.brokerage_account = BrokerageAccount.objects.create(
            user=self.user,
            name='XTB PLN',
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency='PLN',
            external_account_id='10020030',
        )
        self.client.login(username='investor', password='pass123')

    def _add_expense(self, **overrides):
        payload = {
            'date': '2026-08-08',
            'title': 'Wpłata XTB',
            'category': 'Inwestycje',
            'store': 'XTB',
            'cost': '1000.00',
            'brokerage_account': str(self.brokerage_account.id),
        }
        payload.update(overrides)
        return self.client.post(reverse('finance:add_expense'), payload, follow=True)

    def test_add_targeted_investment_creates_pending_funding(self):
        response = self._add_expense()

        self.assertEqual(response.status_code, 200)
        expense = Daily.objects.get(title='Wpłata XTB')
        funding = expense.investment_funding
        self.assertEqual(expense.brokerage_account, self.brokerage_account)
        self.assertEqual(funding.status, InvestmentFunding.PENDING)
        self.assertEqual(funding.source_amount, Decimal('1000.00'))

    def test_investment_without_target_does_not_guess_account(self):
        response = self._add_expense(brokerage_account='')

        self.assertEqual(response.status_code, 200)
        expense = Daily.objects.get(title='Wpłata XTB')
        self.assertIsNone(expense.brokerage_account)
        self.assertFalse(InvestmentFunding.objects.exists())

    def test_non_investment_cannot_receive_tampered_brokerage_target(self):
        response = self._add_expense(category='Rachunki')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Daily.objects.exists())
        self.assertContains(response, 'Konto maklerskie można przypisać tylko do kategorii Inwestycje')

    def test_other_users_brokerage_account_is_rejected(self):
        other_user = User.objects.create_user(username='other', password='pass123')
        other_account = BrokerageAccount.objects.create(
            user=other_user,
            name='Cudze XTB',
            broker=BrokerageAccount.BROKER_XTB,
            currency='PLN',
        )

        response = self._add_expense(brokerage_account=str(other_account.id))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Daily.objects.exists())
        self.assertContains(response, 'Wybrane konto maklerskie nie jest dostępne')

    def test_existing_xtb_deposit_is_matched_when_expense_is_added(self):
        operation = BrokerageCashOperation.objects.create(
            account=self.brokerage_account,
            operation_type=BrokerageCashOperation.DEPOSIT,
            occurred_at=timezone.make_aware(datetime(2026, 8, 9, 10, 30)),
            amount='1000.00',
            currency='PLN',
            external_id='cash-1',
            import_source='xtb',
        )

        self._add_expense()

        funding = InvestmentFunding.objects.get()
        self.assertEqual(funding.status, InvestmentFunding.MATCHED)
        self.assertEqual(funding.cash_operation, operation)

    def test_ambiguous_deposits_are_left_for_review(self):
        for index in range(2):
            BrokerageCashOperation.objects.create(
                account=self.brokerage_account,
                operation_type=BrokerageCashOperation.DEPOSIT,
                occurred_at=timezone.make_aware(datetime(2026, 8, 8 + index, 10, 30)),
                amount='1000.00',
                currency='PLN',
                external_id=f'ambiguous-{index}',
                import_source='xtb',
            )

        self._add_expense()

        funding = InvestmentFunding.objects.get()
        self.assertEqual(funding.status, InvestmentFunding.NEEDS_REVIEW)
        self.assertIsNone(funding.cash_operation)

    def test_confirmed_bank_import_creates_targeted_funding(self):
        payload = QueryDict('', mutable=True)
        payload.update({
            'row_count': '1',
            'row_0_selected': 'on',
            'row_0_kind': 'expense',
            'row_0_external_id': 'millennium:investment-1',
            'row_0_date': '2026-08-08',
            'row_0_amount': '750.00',
            'row_0_title': 'Przelew do XTB',
            'row_0_category': 'Inwestycje',
            'row_0_store': 'XTB',
            'row_0_brokerage_account': str(self.brokerage_account.id),
        })

        result = import_candidates_from_post(self.user, self.finance_account, payload)

        self.assertEqual(result.created_expenses, 1)
        funding = InvestmentFunding.objects.select_related('expense').get()
        self.assertEqual(funding.account, self.brokerage_account)
        self.assertEqual(funding.source_amount, Decimal('750.00'))
        self.assertEqual(funding.expense.brokerage_account, self.brokerage_account)

    def test_editing_matched_funding_unlinks_broker_fact_without_deleting_it(self):
        operation = BrokerageCashOperation.objects.create(
            account=self.brokerage_account,
            operation_type=BrokerageCashOperation.DEPOSIT,
            occurred_at=timezone.make_aware(datetime(2026, 8, 8, 12, 0)),
            amount='1000.00',
            currency='PLN',
            external_id='cash-2',
            import_source='xtb',
        )
        self._add_expense()
        expense = Daily.objects.get()

        response = self.client.post(reverse('finance:edit_expense', args=[expense.id]), {
            'date': '2026-08-08',
            'title': 'Skorygowana wpłata XTB',
            'category': 'Inwestycje',
            'store': 'XTB',
            'cost': '900.00',
            'brokerage_account': str(self.brokerage_account.id),
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        funding = InvestmentFunding.objects.get()
        self.assertEqual(funding.status, InvestmentFunding.NEEDS_REVIEW)
        self.assertIsNone(funding.cash_operation)
        self.assertTrue(BrokerageCashOperation.objects.filter(id=operation.id).exists())

    def test_deleting_matched_expense_preserves_imported_cash_operation(self):
        operation = BrokerageCashOperation.objects.create(
            account=self.brokerage_account,
            operation_type=BrokerageCashOperation.DEPOSIT,
            occurred_at=timezone.make_aware(datetime(2026, 8, 8, 12, 0)),
            amount='1000.00',
            currency='PLN',
            external_id='cash-3',
            import_source='xtb',
        )
        self._add_expense()
        expense = Daily.objects.get()

        self.client.post(reverse('finance:delete_expense', args=[expense.id]), follow=True)

        self.assertFalse(InvestmentFunding.objects.exists())
        self.assertTrue(BrokerageCashOperation.objects.filter(id=operation.id).exists())
