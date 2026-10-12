"""Strategia GEM w widoku kont maklerskich (finance.gem)."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.template import Context, Template
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from finance import gem
from finance.market_data import MarketDataError
from finance.models import (
    BrokerageAccount,
    BrokerageInstrument,
    BrokerageTransaction,
    StrategyDailyPrice,
)

User = get_user_model()
TODAY = date(2026, 10, 12)          # poniedziałek
SIGNAL = date(2026, 9, 30)          # ostatnia sesja września


def local(*args):
    return timezone.make_aware(datetime(*args), timezone.get_current_timezone())


def add_prices(symbol, points):
    for day, value in points.items():
        StrategyDailyPrice.objects.create(symbol=symbol, trading_date=day, close=Decimal(value),
                                          adjusted_close=Decimal(value), currency='USD')


def seed(**twelve_month):
    """Ceny tak, by stopy zwrotu 12 mies. wyszły dokładnie takie, jak podane.

    Argumenty: symbol_bez_kropki=(wynik na koniec września, wynik dziś), w procentach.
    """
    for symbol, (signal_pct, today_pct) in twelve_month.items():
        add_prices(f'{symbol}.L', {
            date(2025, 10, 1): '100',         # pierwsza sesja od 1.10.2025 (dzień po 30.09 - 12 mies.)
            date(2025, 10, 13): '100',        # pierwsza sesja od 13.10.2025 (dzień po dziś - 12 mies.)
            SIGNAL: str(Decimal('100') + Decimal(str(signal_pct))),
            TODAY: str(Decimal('100') + Decimal(str(today_pct))),
        })


def position(ticker, price_symbol=''):
    return {'instrument': SimpleNamespace(ticker=ticker, price_symbol=price_symbol)}


class DateAndSeriesTests(SimpleTestCase):
    def test_add_months_clamps_to_month_end(self):
        self.assertEqual(gem.add_months(date(2026, 3, 31), -1), date(2026, 2, 28))
        self.assertEqual(gem.add_months(date(2024, 3, 31), -1), date(2024, 2, 29))
        self.assertEqual(gem.add_months(date(2026, 10, 12), -12), date(2025, 10, 12))
        self.assertEqual(gem.add_months(date(2026, 1, 15), -3), date(2025, 10, 15))

    def test_period_return_starts_at_first_session_on_or_after_target(self):
        series = gem.PriceSeries([
            (date(2025, 10, 10), Decimal('90')),     # piątek przed 12.10.2025 - nie liczy się
            (date(2025, 10, 13), Decimal('100')),    # pierwsza sesja od 13.10.2025
            (date(2026, 10, 12), Decimal('118')),
        ])
        self.assertEqual(series.period_return(date(2026, 10, 12), 12), Decimal('0.18'))
        # Dzień bez sesji na końcu: liczy od ostatniej sesji przed nim.
        self.assertEqual(series.period_return(date(2026, 10, 14), 12), Decimal('0.18'))
        # Pierwsza sesja ponad 7 dni po celu (brak historii) - brak wyniku.
        gap = gem.PriceSeries([(date(2025, 11, 3), Decimal('100')), (date(2026, 10, 12), Decimal('118'))])
        self.assertIsNone(gap.period_return(date(2026, 10, 12), 12))
        self.assertIsNone(gem.PriceSeries([]).period_return(date(2026, 10, 12), 12))

    def test_matches_monthly_yfinance_script(self):
        """Ta sama liczba co skrypt: data.iloc[-1] / data.iloc[0] - 1 dla start = dziś - 12 mies.

        Skrypt uruchomiony rano (zanim jest dzisiejsza sesja) bierze ostatnią
        sesję jako koniec i pierwszą sesję od dnia sprzed 12 miesięcy jako początek.
        """
        days, value, rows = date(2025, 1, 1), Decimal('100'), []
        while days <= date(2026, 12, 31):
            if days.weekday() < 5:
                value = (value * (Decimal('1.0004') if days.day % 3 else Decimal('0.999'))).quantize(Decimal('0.0001'))
                rows.append((days, value))
            days += timedelta(days=1)
        series = gem.PriceSeries(rows)

        def script(run_day):
            start = gem.add_months(run_day, -12)
            window = [price for day, price in rows if start <= day < run_day]   # yf: end bez dnia uruchomienia
            return window[-1] / window[0] - 1

        # Skrypt uruchomiony dzień po sesji (1.10 po sesji 30.09, wtorek po poniedziałku…).
        for run_day in (date(2026, 10, 13), date(2026, 10, 1), date(2026, 3, 3), date(2026, 6, 16), date(2026, 9, 1)):
            with self.subTest(run_day=run_day):
                last_session = run_day - timedelta(days=1)
                self.assertIn(last_session, series.dates)
                self.assertEqual(series.period_return(last_session, 12), script(run_day))

    def test_signed_percent_filters(self):
        rendered = Template(
            '{% load gem_extras %}{{ a|signed_pct }}|{{ b|signed_pct:2 }}|{{ c|signed_pct }}|{{ d|signed_pct }}|{{ e|signed_pp }}|{{ f|signed_pct }}'
        ).render(Context({'a': Decimal('18.42'), 'b': Decimal('-3.2'), 'c': Decimal('0.02'), 'd': None, 'e': Decimal('3.14'), 'f': Decimal('19.75')}))
        # 19,75 → 19,8 (zaokrąglenie jak na wykresie, nie obcięcie)
        self.assertEqual(rendered, '+18,4%|−3,20%|0,0%|–|3,1 pp|+19,8%')


class GemSummaryTests(TestCase):
    def setUp(self):
        cache.clear()

    def summary(self, positions=None):
        return gem.build_gem_summary(positions, today=TODAY)

    def test_monthly_signal_and_today_leader(self):
        seed(CSPX=(16, 19), IWDA=(11, 14), EIMI=(15, 24), IB01=(4, 4.5))
        result = self.summary()
        self.assertEqual(result['signal_date'], SIGNAL)
        self.assertEqual(result['as_of'], TODAY)
        self.assertEqual(result['signal_winner']['asset'].ticker, 'CSPX')
        self.assertEqual(result['winner']['asset'].ticker, 'EIMI')
        self.assertTrue(result['signal_changed'])
        self.assertEqual([row['asset'].ticker for row in result['rows']], ['EIMI', 'CSPX', 'IWDA', 'IB01'])
        self.assertEqual([row['rank'] for row in result['rows']], [1, 2, 3, 4])
        self.assertEqual(result['rows'][0]['r12'], Decimal('24.00'))
        self.assertEqual(result['margin'], Decimal('5.00'))            # EIMI 24 - CSPX 19
        self.assertEqual(result['runner_up']['asset'].ticker, 'CSPX')
        self.assertEqual(result['signal_vs_safe'], Decimal('12.00'))   # CSPX 16 - IB01 4
        self.assertFalse(result['safe_mode'])
        self.assertTrue(result['is_complete'])
        # Słupki: zero na lewej krawędzi, najlepszy wynik zajmuje całą szerokość,
        # próg IB01 tam, gdzie kończy się jego słupek.
        self.assertEqual(result['zero'], Decimal('0.00'))
        self.assertEqual(result['rows'][0]['bar']['width'], Decimal('100.00'))
        self.assertEqual(result['hurdle'], Decimal('18.75'))          # 4,5 / 24
        self.assertTrue(result['rows'][1]['beats_hurdle'])
        # Wykres: od pierwszej sesji okna do dziś, koniec linii = wynik 12 mies.
        chart = result['chart']
        self.assertEqual((chart['start'], chart['end']), (date(2025, 10, 13), TODAY))
        self.assertEqual(chart['labels'], ['2025-10-13', '2026-09-30', '2026-10-12'])
        self.assertEqual([item['ticker'] for item in chart['datasets']], ['CSPX', 'IWDA', 'EIMI', 'IB01'])
        self.assertEqual(chart['datasets'][2]['values'], [0.0, 15.0, 24.0])
        self.assertTrue(chart['datasets'][3]['safe'])
        self.assertEqual(chart['table_rows'], [0, 1, 2])

    def test_bills_win_when_no_equity_fund_beats_them(self):
        seed(CSPX=(-6, -3), IWDA=(-4, -2), EIMI=(-9, -8), IB01=(4, 4.2))
        result = self.summary()
        self.assertEqual(result['signal_winner']['asset'].ticker, 'IB01')
        self.assertTrue(result['signal_safe_mode'])
        self.assertTrue(result['safe_mode'])
        self.assertEqual(result['signal_vs_safe'], Decimal('-8.00'))   # najlepsze akcje -4 vs IB01 +4
        self.assertFalse(result['signal_changed'])
        # Wartości ujemne: zero przesuwa się w prawo.
        self.assertEqual(result['zero'], Decimal('65.57'))            # 8 / (8 + 4,2)

    def test_missing_history_gives_no_signal(self):
        seed(CSPX=(16, 19), IWDA=(11, 14), IB01=(4, 4.5))
        add_prices('EIMI.L', {date(2026, 6, 1): '40', TODAY: '42'})
        result = self.summary()
        self.assertIsNone(result['winner'])
        self.assertIsNone(result['signal_winner'])
        self.assertEqual(result['missing'], ['EIMI'])
        self.assertFalse(result['is_complete'])
        eimi = next(row for row in result['rows'] if row['asset'].ticker == 'EIMI')
        self.assertTrue(eimi['has_data'])
        self.assertIsNone(eimi['rank'])
        self.assertIsNone(result['chart'])

    def test_empty(self):
        result = self.summary()
        self.assertTrue(result['is_empty'])
        self.assertIsNone(result['as_of'])

    def test_holdings_against_signal(self):
        seed(CSPX=(16, 19), IWDA=(11, 14), EIMI=(15, 24), IB01=(4, 4.5))
        cases = [
            ([position('CSPX.UK')], 'match', ['CSPX']),
            ([position('IWDA.UK')], 'switch', ['IWDA']),
            ([position('Beta ETF', 'CSPX.L'), position('EIMI.UK')], 'mixed', ['CSPX', 'EIMI']),
            ([position('ETFSP500.WA')], 'none', []),
            (None, 'none', []),
        ]
        for positions, status, held in cases:
            with self.subTest(status=status, held=held):
                result = self.summary(positions)
                self.assertEqual((result['holding_status'], result['held_tickers']), (status, held))


@override_settings(GEM_PRICES_ENABLED=True)
class SyncTests(TestCase):
    def setUp(self):
        cache.clear()

    def points(self, days, value='100'):
        return [{'date': day, 'close': Decimal(value), 'adjusted_close': Decimal(value)} for day in days]

    def test_first_sync_then_incremental(self):
        calls = []

        def fake_history(client, symbol, start, end):
            calls.append((symbol, start, end))
            return self.points([end - timedelta(days=3), end])

        with mock.patch('finance.gem.YahooFinanceClient.fetch_daily_history', autospec=True, side_effect=fake_history):
            first = gem.sync_gem_prices(today=TODAY)
        self.assertEqual((first['synced'], first['created'], first['failed']), (4, 8, []))
        self.assertEqual({call[1] for call in calls}, {TODAY - timedelta(days=gem.HISTORY_DAYS)})
        self.assertEqual(StrategyDailyPrice.objects.filter(symbol='CSPX.L').count(), 2)

        calls.clear()

        def fake_update(client, symbol, start, end):
            calls.append((symbol, start, end))
            return self.points([end], value='105')

        with mock.patch('finance.gem.YahooFinanceClient.fetch_daily_history', autospec=True, side_effect=fake_update):
            second = gem.sync_gem_prices(today=TODAY)
        self.assertEqual({call[1] for call in calls}, {TODAY - timedelta(days=gem.REFETCH_DAYS)})
        self.assertEqual((second['created'], second['updated']), (0, 4))
        self.assertEqual(StrategyDailyPrice.objects.get(symbol='IB01.L', trading_date=TODAY).close, Decimal('105'))
        self.assertIsNotNone(gem.sync_status()['success_at'])

    def test_failures_are_reported_per_fund_and_old_prices_pruned(self):
        add_prices('CSPX.L', {TODAY - timedelta(days=gem.HISTORY_DAYS + 60): '90'})

        def flaky(client, symbol, start, end):
            if symbol == 'EIMI.L':
                raise MarketDataError('HTTP 429')
            return self.points([end])

        with mock.patch('finance.gem.YahooFinanceClient.fetch_daily_history', autospec=True, side_effect=flaky):
            result = gem.sync_gem_prices(today=TODAY)
        self.assertEqual(result['synced'], 3)
        self.assertEqual(result['failed'], ['EIMI: HTTP 429'])
        self.assertEqual(gem.sync_status()['failed'], ['EIMI: HTTP 429'])
        self.assertFalse(StrategyDailyPrice.objects.filter(trading_date__lt=TODAY).exists())

    @override_settings(GEM_PRICES_ENABLED=False)
    def test_disabled(self):
        with mock.patch('finance.gem.YahooFinanceClient.fetch_daily_history') as fetch:
            self.assertEqual(gem.sync_gem_prices(today=TODAY)['synced'], 0)
        fetch.assert_not_called()
        self.assertFalse(gem.needs_refresh())


@override_settings(GEM_PRICES_ENABLED=True)
class AutoRefreshTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_last_session_close(self):
        self.assertEqual(gem.last_session_close(local(2026, 10, 12, 20, 0)), local(2026, 10, 12, 18, 30))
        self.assertEqual(gem.last_session_close(local(2026, 10, 12, 9, 0)), local(2026, 10, 9, 18, 30))
        self.assertEqual(gem.last_session_close(local(2026, 10, 11, 12, 0)), local(2026, 10, 9, 18, 30))

    def test_needs_refresh(self):
        now = local(2026, 10, 12, 20, 0)
        self.assertTrue(gem.needs_refresh(now))
        cache.set(gem.CACHE_KEY, {'success_at': local(2026, 10, 12, 19, 0), 'attempt_at': local(2026, 10, 12, 19, 0)})
        self.assertFalse(gem.needs_refresh(now))
        # Udane pobranie przed zamknięciem sesji - trzeba dociągnąć zamknięcie.
        cache.set(gem.CACHE_KEY, {'success_at': local(2026, 10, 12, 12, 0), 'attempt_at': local(2026, 10, 12, 12, 0)})
        self.assertTrue(gem.needs_refresh(now))
        # Nieudana próba 10 minut temu - nie pytamy dostawcy przy każdym wejściu.
        cache.set(gem.CACHE_KEY, {'success_at': local(2026, 10, 12, 12, 0), 'attempt_at': local(2026, 10, 12, 19, 50)})
        self.assertFalse(gem.needs_refresh(now))
        self.assertTrue(gem.needs_refresh(local(2026, 10, 12, 20, 25)))


class GemViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='gem-investor', password='pass123')
        self.client.force_login(self.user)

    def test_brokerage_page_shows_panel(self):
        seed(CSPX=(16, 19), IWDA=(11, 14), EIMI=(15, 24), IB01=(4, 4.5))
        account = BrokerageAccount.objects.create(user=self.user, name='XTB IKE', broker='xtb', currency='PLN')
        instrument = BrokerageInstrument.objects.create(user=self.user, ticker='IWDA.UK', name='iShares MSCI World',
                                                        currency='USD', asset_type='etf')
        BrokerageTransaction.objects.create(account=account, instrument=instrument, transaction_type='buy',
                                            trade_date=date(2026, 3, 2), quantity=Decimal('2'), price=Decimal('100'))
        with mock.patch('finance.gem.timezone.localdate', return_value=TODAY):
            response = self.client.get('/finance/brokerage/')
        self.assertContains(response, 'id="gem"')
        self.assertContains(response, 'GEM · momentum 12 miesięcy')
        self.assertContains(response, 'Sygnał z końca września')
        self.assertContains(response, '<strong class="gem-verdict-ticker">CSPX</strong>', html=False)
        self.assertContains(response, 'Dziś prowadzi EIMI')
        self.assertContains(response, 'Masz IWDA, a sygnał wskazuje CSPX.')
        self.assertContains(response, 'próg: IB01 +4,5%')
        self.assertContains(response, 'js/gem-strategy.js')
        self.assertNotContains(response, 'data-gem-auto')   # w testach pobieranie wyłączone

    def test_empty_panel(self):
        response = self.client.get('/finance/brokerage/')
        self.assertContains(response, 'Czekam na notowania CSPX, IWDA, EIMI, IB01')

    @override_settings(GEM_PRICES_ENABLED=True)
    def test_auto_flag_when_refresh_needed(self):
        self.assertContains(self.client.get('/finance/brokerage/'), 'data-gem-auto="1"')

    def test_refresh_from_script_returns_panel(self):
        result = {'created': 4, 'updated': 0, 'synced': 4, 'failed': []}
        with mock.patch('finance.views.sync_gem_prices', return_value=result) as sync:
            response = self.client.post('/finance/brokerage/gem/refresh/', {'mode': 'manual'},
                                        HTTP_X_REQUESTED_WITH='fetch')
        sync.assert_called_once()
        payload = response.json()
        self.assertTrue(payload['fetched'])
        self.assertIn('data-gem-panel', payload['html'])

    def test_auto_refresh_skips_fetch_when_current(self):
        with mock.patch('finance.views.gem_needs_refresh', return_value=False), \
                mock.patch('finance.views.sync_gem_prices') as sync:
            response = self.client.post('/finance/brokerage/gem/refresh/', {'mode': 'auto'},
                                        HTTP_X_REQUESTED_WITH='fetch')
        sync.assert_not_called()
        self.assertFalse(response.json()['fetched'])

    def test_refresh_failure_shown_in_panel(self):
        result = {'created': 0, 'updated': 0, 'synced': 0, 'failed': ['CSPX: HTTP 403'] * 4}
        with mock.patch('finance.views.sync_gem_prices', return_value=result):
            response = self.client.post('/finance/brokerage/gem/refresh/', {}, HTTP_X_REQUESTED_WITH='fetch')
        self.assertIn('Nie udało się pobrać nowych notowań (4 z 4 funduszy)', response.json()['html'])

        with mock.patch('finance.views.sync_gem_prices', return_value=result):
            response = self.client.post('/finance/brokerage/gem/refresh/', {}, follow=True)
        self.assertRedirects(response, '/finance/brokerage/#gem')
        self.assertContains(response, 'Nie udało się pobrać notowań GEM')

    def test_refresh_requires_login(self):
        self.client.logout()
        response = self.client.post('/finance/brokerage/gem/refresh/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])


class CommandTests(TestCase):
    @mock.patch('finance.management.commands.sync_price_history.sync_price_history_for_user')
    @mock.patch('finance.management.commands.sync_price_history.sync_gem_prices',
                return_value={'created': 3, 'updated': 1, 'synced': 4, 'failed': ['EIMI: HTTP 429']})
    def test_sync_price_history_also_syncs_gem(self, sync_gem, _sync_user):
        stdout, stderr = StringIO(), StringIO()
        call_command('sync_price_history', stdout=stdout, stderr=stderr)
        sync_gem.assert_called_once_with(force=False)
        self.assertIn('GEM: fundusze: 4, nowe sesje: 3, zaktualizowane: 1', stdout.getvalue())
        self.assertIn('GEM EIMI: HTTP 429', stderr.getvalue())

        sync_gem.reset_mock()
        call_command('sync_price_history', '--bez-gem', stdout=StringIO(), stderr=StringIO())
        sync_gem.assert_not_called()
