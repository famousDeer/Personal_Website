"""Konto finansowe liczone raz na żądanie, a poza modułem finansów wcale."""
from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from finance.account_utils import (
    ACTIVE_FINANCE_ACCOUNT_SESSION_KEY,
    get_active_finance_account,
    set_active_finance_account,
)
from finance.models import Daily, FinanceAccount, Monthly

User = get_user_model()


class ActiveAccountMemoTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='memo', password='pass123')
        self.shared = FinanceAccount.objects.create(
            owner=self.user, name='Dom', account_type=FinanceAccount.SHARED,
        )
        self.shared.members.add(self.user)

    def _request(self):
        request = RequestFactory().get('/')
        request.user = User.objects.get(pk=self.user.pk)
        request.session = SessionStore()
        return request

    def test_second_call_in_request_does_not_query(self):
        request = self._request()
        first = get_active_finance_account(request)
        with self.assertNumQueries(0):
            self.assertEqual(get_active_finance_account(request), first)
        self.assertEqual(first.account_type, FinanceAccount.PERSONAL)
        self.assertEqual(request.session[ACTIVE_FINANCE_ACCOUNT_SESSION_KEY], first.id)

    def test_switching_account_in_request_is_respected(self):
        request = self._request()
        get_active_finance_account(request)
        set_active_finance_account(request, self.shared)
        self.assertEqual(get_active_finance_account(request), self.shared)

    def test_account_without_membership_falls_back_to_personal(self):
        foreign = FinanceAccount.objects.create(
            owner=User.objects.create_user(username='obcy', password='pass123'),
            name='Obce', account_type=FinanceAccount.SHARED,
        )
        request = self._request()
        request.session[ACTIVE_FINANCE_ACCOUNT_SESSION_KEY] = foreign.id
        active = get_active_finance_account(request)
        self.assertEqual(active.account_type, FinanceAccount.PERSONAL)
        self.assertEqual(request.session[ACTIVE_FINANCE_ACCOUNT_SESSION_KEY], active.id)


class LazyContextProcessorTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='leniwy', password='pass123')
        self.client.force_login(self.user)

    def _queries(self, url):
        self.client.get(url)  # pierwsze wejście tworzy sesję i konto osobiste
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return [q['sql'] for q in ctx.captured_queries]

    def test_pages_outside_finance_do_not_query_accounts(self):
        for url in (reverse('index'), reverse('cooking:index'), reverse('cars:garage')):
            with self.subTest(url=url):
                queries = self._queries(url)
                self.assertFalse([q for q in queries if 'finance_accounts' in q], url)

    def test_finance_switcher_still_lists_accounts(self):
        response = self.client.get(reverse('finance:index'))
        self.assertContains(response, 'leniwy - konto osobiste')
        self.assertEqual(len(response.context['finance_accounts']), 1)


class BrokerageQueryCountTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='makler2', password='pass123')
        self.account = self.user.owned_finance_accounts.get(account_type='personal')
        self.client.force_login(self.user)
        self.month = Monthly.objects.create(
            user=self.user, account=self.account, date=date(2025, 6, 1),
            total_income=0, total_expense=0,
        )

    def _add_investments(self, count, start):
        for day in range(start, start + count):
            Daily.objects.create(
                user=self.user, account=self.account, month=self.month, date=date(2025, 6, day),
                title='Inwestycje', category='Inwestycje', store='', cost=100,
            )

    def _queries(self):
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse('finance:brokerage'))
        return [q['sql'] for q in ctx.captured_queries]

    def test_unassigned_list_does_not_query_per_row(self):
        self._add_investments(2, 1)
        self._queries()  # pierwsze wejście wypełnia cache podsumowania portfela
        few = self._queries()
        self._add_investments(8, 3)
        many = self._queries()
        self.assertEqual(len(many), len(few))
