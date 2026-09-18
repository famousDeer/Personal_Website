from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from finance.market_data import MarketDataError
from finance.models import BrokerageAccount, BrokerageInstrument

User = get_user_model()

REFRESH_TARGET = 'finance.management.commands.refresh_market_data.refresh_market_data_for_user'
SYNC_TARGET = 'finance.management.commands.sync_price_history.sync_price_history_for_user'


def refresh_result(**overrides):
    result = {
        'updated_quotes': 0,
        'updated_dividends': 0,
        'merged_instruments': 0,
        'failed_quotes': [],
        'failed_dividends': [],
        'inactive_skipped': 0,
        'dividends_checked': False,
        'source': 'Yahoo Finance',
    }
    result.update(overrides)
    return result


def sync_result(**overrides):
    result = {
        'instruments_synced': 0,
        'points_created': 0,
        'points_updated': 0,
        'already_current': 0,
        'failed': [],
    }
    result.update(overrides)
    return result


class MarketDataCommandTests(TestCase):
    """Komendy danych rynkowych uruchamiane z crona.

    Zewnętrzni dostawcy są zamockowani - testy sprawdzają wybór użytkowników,
    izolację błędów i przekazywanie flag, a nie samą integrację sieciową.
    """

    @classmethod
    def setUpTestData(cls):
        cls.with_instruments = User.objects.create_user('ala', password='x')
        cls.also_with_instruments = User.objects.create_user('bob', password='x')
        cls.without_instruments = User.objects.create_user('cela', password='x')
        for user in (cls.with_instruments, cls.also_with_instruments):
            BrokerageAccount.objects.create(
                user=user, name=f'XTB {user.username}', broker='xtb', currency='PLN',
            )
            BrokerageInstrument.objects.create(
                user=user, ticker=f'TST{user.id}', name='Test', currency='PLN',
            )

    def test_refresh_skips_users_without_instruments(self):
        stdout = StringIO()
        with mock.patch(REFRESH_TARGET, return_value=refresh_result(updated_quotes=2)) as refresh:
            call_command('refresh_market_data', stdout=stdout, stderr=StringIO())

        processed = {call.args[0].username for call in refresh.call_args_list}
        self.assertEqual(processed, {'ala', 'bob'})
        self.assertNotIn('cela', stdout.getvalue())

    def test_refresh_continues_after_one_user_fails(self):
        stdout, stderr = StringIO(), StringIO()

        def flaky(user, **kwargs):
            if user.username == 'ala':
                raise MarketDataError('dostawca nie odpowiada')
            return refresh_result(updated_quotes=2)

        with mock.patch(REFRESH_TARGET, flaky):
            call_command('refresh_market_data', stdout=stdout, stderr=stderr)

        self.assertIn('bob: ceny: 2', stdout.getvalue())
        self.assertIn('dostawca nie odpowiada', stderr.getvalue())

    def test_refresh_does_not_touch_dividends_by_default(self):
        with mock.patch(REFRESH_TARGET, return_value=refresh_result()) as refresh:
            call_command('refresh_market_data', '--user', 'ala', stdout=StringIO(), stderr=StringIO())

        self.assertFalse(refresh.call_args.kwargs['refresh_dividends'])

    def test_refresh_dividends_flag_is_forwarded(self):
        with mock.patch(REFRESH_TARGET, return_value=refresh_result(dividends_checked=True)) as refresh:
            call_command(
                'refresh_market_data', '--user', 'ala', '--dividends',
                stdout=StringIO(), stderr=StringIO(),
            )

        self.assertTrue(refresh.call_args.kwargs['refresh_dividends'])

    def test_refresh_reports_failed_quotes(self):
        stderr = StringIO()
        with mock.patch(REFRESH_TARGET, return_value=refresh_result(failed_quotes=['TST: brak symbolu'])):
            call_command('refresh_market_data', '--user', 'ala', stdout=StringIO(), stderr=stderr)

        self.assertIn('TST: brak symbolu', stderr.getvalue())

    def test_unknown_username_is_rejected(self):
        with self.assertRaises(CommandError) as ctx:
            call_command('refresh_market_data', '--user', 'nie-ma-mnie', stdout=StringIO(), stderr=StringIO())

        self.assertIn('nie-ma-mnie', str(ctx.exception))

    def test_sync_forwards_force_flag(self):
        with mock.patch(SYNC_TARGET, return_value=sync_result()) as sync:
            call_command('sync_price_history', '--user', 'ala', '--force', stdout=StringIO(), stderr=StringIO())

        self.assertTrue(sync.call_args.kwargs['force'])

    def test_sync_defaults_to_incremental(self):
        with mock.patch(SYNC_TARGET, return_value=sync_result()) as sync:
            call_command('sync_price_history', '--user', 'ala', stdout=StringIO(), stderr=StringIO())

        self.assertFalse(sync.call_args.kwargs['force'])

    def test_sync_reports_counts_and_failures(self):
        stdout, stderr = StringIO(), StringIO()
        with mock.patch(
            SYNC_TARGET,
            return_value=sync_result(instruments_synced=1, points_created=12, failed=['TST: brak symbolu']),
        ):
            call_command('sync_price_history', '--user', 'ala', stdout=stdout, stderr=stderr)

        self.assertIn('nowe sesje: 12', stdout.getvalue())
        self.assertIn('TST: brak symbolu', stderr.getvalue())

    def test_no_eligible_users_is_not_an_error(self):
        BrokerageInstrument.objects.all().delete()
        stdout = StringIO()
        with mock.patch(REFRESH_TARGET) as refresh:
            call_command('refresh_market_data', stdout=stdout, stderr=StringIO())

        refresh.assert_not_called()
        self.assertIn('Nic do zrobienia', stdout.getvalue())
