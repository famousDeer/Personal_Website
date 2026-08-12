from datetime import date, datetime, timezone as datetime_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from finance.investment_statistics import (
    actual_profit_by_currency,
    calculate_market_statistics,
)
from finance.market_data import MarketDataError, sync_price_history_for_user
from finance.models import (
    BrokerageAccount,
    BrokerageCashOperation,
    BrokerageDailyPrice,
    BrokerageInstrument,
    BrokerageTransaction,
)


User = get_user_model()


def _point(day, close, *, adjusted_close=None):
    close = Decimal(str(close))
    return {
        'date': day,
        'open': close - Decimal('1'),
        'high': close + Decimal('2'),
        'low': close - Decimal('2'),
        'close': close,
        'adjusted_close': Decimal(str(adjusted_close)) if adjusted_close is not None else close,
        'volume': 1234,
    }


class BrokeragePriceHistorySyncTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='history-user', password='pass123')
        self.account = BrokerageAccount.objects.create(
            user=self.user,
            name='XTB USD',
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency='USD',
        )
        self.instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker='AAPL.US',
            price_symbol='AAPL.US',
            name='Apple',
            currency='USD',
            asset_type=BrokerageInstrument.STOCK,
        )
        BrokerageTransaction.objects.create(
            account=self.account,
            instrument=self.instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 10),
            quantity='5',
            price='100',
        )
        self.now = datetime(2026, 2, 1, 12, tzinfo=datetime_timezone.utc)

    @patch('finance.market_data.YahooFinanceClient.fetch_daily_history')
    @patch('finance.market_data.timezone.now')
    def test_full_history_is_persisted_once_and_second_sync_uses_database(
        self,
        mock_now,
        mock_fetch_history,
    ):
        mock_now.return_value = self.now
        mock_fetch_history.return_value = [
            _point(date(2026, 1, 10), '101', adjusted_close='99.5'),
            _point(date(2026, 1, 12), '104', adjusted_close='102.0'),
        ]

        first_result = sync_price_history_for_user(self.user)

        self.instrument.refresh_from_db()
        self.assertEqual(first_result['instruments_synced'], 1)
        self.assertEqual(first_result['points_created'], 2)
        self.assertEqual(self.instrument.price_symbol, 'AAPL')
        self.assertEqual(self.instrument.history_synced_from, date(2026, 1, 10))
        self.assertEqual(self.instrument.history_synced_through, date(2026, 2, 1))
        stored = BrokerageDailyPrice.objects.get(
            instrument=self.instrument,
            trading_date=date(2026, 1, 10),
        )
        self.assertEqual(stored.open, Decimal('100'))
        self.assertEqual(stored.high, Decimal('103'))
        self.assertEqual(stored.low, Decimal('99'))
        self.assertEqual(stored.close, Decimal('101'))
        self.assertEqual(stored.adjusted_close, Decimal('99.5'))
        self.assertEqual(stored.volume, 1234)
        self.assertTrue(stored.is_final)
        self.assertEqual(stored.source, 'Yahoo Finance')

        mock_fetch_history.reset_mock()
        second_result = sync_price_history_for_user(self.user)

        mock_fetch_history.assert_not_called()
        self.assertEqual(second_result['already_current'], 1)
        self.assertEqual(BrokerageDailyPrice.objects.count(), 2)

    @patch('finance.market_data.YahooFinanceClient.fetch_daily_history')
    @patch('finance.market_data.timezone.now')
    def test_older_purchase_fetches_only_missing_prefix(
        self,
        mock_now,
        mock_fetch_history,
    ):
        mock_now.return_value = self.now
        mock_fetch_history.return_value = [_point(date(2026, 1, 10), '101')]
        sync_price_history_for_user(self.user)

        BrokerageTransaction.objects.create(
            account=self.account,
            instrument=self.instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 2),
            quantity='1',
            price='90',
        )
        mock_fetch_history.reset_mock()
        mock_fetch_history.return_value = [_point(date(2026, 1, 2), '91')]

        result = sync_price_history_for_user(self.user)

        self.assertEqual(result['instruments_synced'], 1)
        mock_fetch_history.assert_called_once_with(
            'AAPL',
            date(2026, 1, 2),
            date(2026, 1, 9),
            allow_empty=True,
        )
        self.instrument.refresh_from_db()
        self.assertEqual(self.instrument.history_synced_from, date(2026, 1, 2))
        self.assertEqual(BrokerageDailyPrice.objects.count(), 2)

    @patch('finance.market_data.YahooFinanceClient.fetch_daily_history')
    @patch('finance.market_data.timezone.now')
    def test_incremental_sync_replaces_lightweight_close_with_adjusted_close(
        self,
        mock_now,
        mock_fetch_history,
    ):
        mock_now.return_value = self.now
        self.instrument.history_synced_from = date(2026, 1, 10)
        self.instrument.history_synced_through = date(2026, 1, 31)
        self.instrument.save(update_fields=['history_synced_from', 'history_synced_through'])
        BrokerageDailyPrice.objects.create(
            instrument=self.instrument,
            trading_date=date(2026, 1, 31),
            close='110',
            adjusted_close='110',
            currency='USD',
            provider_symbol='AAPL',
            source='Yahoo Finance',
            is_final=False,
        )
        mock_fetch_history.return_value = [
            _point(date(2026, 1, 31), '110', adjusted_close='106.50'),
        ]

        result = sync_price_history_for_user(self.user)

        mock_fetch_history.assert_called_once_with(
            'AAPL',
            date(2026, 1, 31),
            date(2026, 2, 1),
            allow_empty=True,
        )
        stored = BrokerageDailyPrice.objects.get(
            instrument=self.instrument,
            trading_date=date(2026, 1, 31),
        )
        self.assertEqual(result['points_updated'], 1)
        self.assertEqual(stored.adjusted_close, Decimal('106.50'))
        self.assertTrue(stored.is_final)

    @patch('finance.market_data.YahooFinanceClient.fetch_daily_history')
    @patch('finance.market_data.timezone.now')
    def test_provider_failure_is_isolated_per_instrument(
        self,
        mock_now,
        mock_fetch_history,
    ):
        mock_now.return_value = self.now
        second = BrokerageInstrument.objects.create(
            user=self.user,
            ticker='MSFT.US',
            price_symbol='MSFT.US',
            name='Microsoft',
            currency='USD',
            asset_type=BrokerageInstrument.STOCK,
        )
        BrokerageTransaction.objects.create(
            account=self.account,
            instrument=second,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 15),
            quantity='2',
            price='200',
        )

        def provider_result(symbol, start_date, _end_date, **_kwargs):
            if symbol == 'AAPL':
                raise MarketDataError('chwilowy błąd')
            return [_point(start_date, '205')]

        mock_fetch_history.side_effect = provider_result

        result = sync_price_history_for_user(self.user)

        self.assertEqual(result['instruments_synced'], 1)
        self.assertEqual(len(result['failed']), 1)
        self.assertIn('AAPL.US:', result['failed'][0])
        self.assertIn('chwilowy błąd', result['failed'][0])
        self.assertFalse(BrokerageDailyPrice.objects.filter(instrument=self.instrument).exists())
        self.assertTrue(BrokerageDailyPrice.objects.filter(instrument=second).exists())


class InvestmentStatisticsTests(TestCase):
    def test_market_statistics_report_return_risk_and_drawdown(self):
        points = [
            _point(date(2026, 1, 1), '100'),
            _point(date(2026, 1, 2), '110'),
            _point(date(2026, 1, 3), '88'),
            _point(date(2026, 1, 4), '96.8'),
        ]

        result = calculate_market_statistics(
            points,
            first_purchase_price=Decimal('80'),
            current_price=Decimal('100'),
        )

        self.assertEqual(result['sessions'], 4)
        self.assertEqual(result['period_return_percent'], Decimal('-3.200'))
        self.assertEqual(result['price_change_since_purchase'], Decimal('20'))
        self.assertEqual(result['price_change_since_purchase_percent'], Decimal('25.00'))
        self.assertEqual(result['average_daily_return_percent'], Decimal('0.0'))
        self.assertEqual(result['best_day_percent'], Decimal('10.0'))
        self.assertEqual(result['best_day_date'], date(2026, 1, 2))
        self.assertEqual(result['worst_day_percent'], Decimal('-20.0'))
        self.assertEqual(result['worst_day_date'], date(2026, 1, 3))
        self.assertAlmostEqual(float(result['positive_sessions_percent']), 66.6667, places=3)
        self.assertEqual(result['maximum_drawdown_percent'], Decimal('-20.0'))
        self.assertEqual(result['maximum_drawdown_peak_date'], date(2026, 1, 2))
        self.assertEqual(result['maximum_drawdown_trough_date'], date(2026, 1, 3))
        self.assertIsNotNone(result['annualized_volatility_percent'])
        self.assertIsNotNone(result['cagr_percent'])

    def test_constant_prices_have_zero_volatility_and_drawdown(self):
        points = [
            _point(date(2026, 1, 1), '100'),
            _point(date(2026, 1, 2), '100'),
            _point(date(2026, 1, 3), '100'),
        ]

        result = calculate_market_statistics(points)

        self.assertEqual(result['period_return_percent'], Decimal('0'))
        self.assertEqual(result['average_daily_return_percent'], Decimal('0'))
        self.assertEqual(result['annualized_volatility_percent'], Decimal('0.0'))
        self.assertEqual(result['maximum_drawdown_percent'], Decimal('0'))
        self.assertEqual(result['positive_sessions_percent'], Decimal('0'))

    def test_actual_profit_uses_cash_ledger_and_keeps_currencies_separate(self):
        user = User.objects.create_user(username='profit-user', password='pass123')
        usd_account = BrokerageAccount.objects.create(
            user=user,
            name='XTB USD',
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency='USD',
        )
        eur_account = BrokerageAccount.objects.create(
            user=user,
            name='XTB EUR',
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency='EUR',
        )
        instrument = BrokerageInstrument.objects.create(
            user=user,
            ticker='TEST',
            name='Test instrument',
            currency='USD',
        )
        occurred_at = datetime(2026, 1, 1, 12, tzinfo=datetime_timezone.utc)
        for index, (account, operation_type, amount) in enumerate([
            (usd_account, BrokerageCashOperation.BUY, '-1000'),
            (usd_account, BrokerageCashOperation.SELL, '400'),
            (usd_account, BrokerageCashOperation.DIVIDEND, '20'),
            (usd_account, BrokerageCashOperation.WITHHOLDING_TAX, '-3'),
            (usd_account, BrokerageCashOperation.FEE, '-2'),
            (eur_account, BrokerageCashOperation.BUY, '-500'),
        ]):
            BrokerageCashOperation.objects.create(
                account=account,
                instrument=instrument,
                operation_type=operation_type,
                occurred_at=occurred_at,
                amount=amount,
                currency=account.currency,
                external_id=str(index),
            )
        portfolio_summary = {
            'positions': [
                {
                    'account': usd_account,
                    'instrument': instrument,
                    'current_value': Decimal('700'),
                    'cost': Decimal('600'),
                    'profit': Decimal('100'),
                },
                {
                    'account': eur_account,
                    'instrument': instrument,
                    'current_value': Decimal('550'),
                    'cost': Decimal('500'),
                    'profit': Decimal('50'),
                },
            ],
        }

        result = actual_profit_by_currency(user, instrument, portfolio_summary)

        self.assertEqual([bucket['currency'] for bucket in result], ['EUR', 'USD'])
        eur, usd = result
        self.assertEqual(eur['total_profit'], Decimal('50'))
        self.assertEqual(eur['return_percent'], Decimal('10.0'))
        self.assertEqual(usd['trade_profit'], Decimal('100'))
        self.assertEqual(usd['net_income'], Decimal('15'))
        self.assertEqual(usd['total_profit'], Decimal('115'))
        self.assertEqual(usd['return_percent'], Decimal('11.500'))
        self.assertTrue(usd['has_ledger'])
        self.assertEqual(usd['source'], 'XTB cash ledger')

    def test_actual_profit_falls_back_to_signed_transactions(self):
        user = User.objects.create_user(username='fallback-user', password='pass123')
        account = BrokerageAccount.objects.create(
            user=user,
            name='Manual USD',
            broker=BrokerageAccount.BROKER_OTHER,
            account_type=BrokerageAccount.STANDARD,
            currency='USD',
        )
        instrument = BrokerageInstrument.objects.create(
            user=user,
            ticker='MANUAL',
            name='Manual instrument',
            currency='USD',
        )
        BrokerageTransaction.objects.create(
            account=account,
            instrument=instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 1),
            quantity='2',
            price='100',
            fees='5',
        )
        BrokerageTransaction.objects.create(
            account=account,
            instrument=instrument,
            transaction_type=BrokerageTransaction.SELL,
            trade_date=date(2026, 1, 2),
            quantity='1',
            price='120',
            fees='2',
        )
        portfolio_summary = {
            'positions': [{
                'account': account,
                'instrument': instrument,
                'current_value': Decimal('130'),
                'cost': Decimal('102.50'),
                'profit': Decimal('27.50'),
            }],
        }

        result = actual_profit_by_currency(user, instrument, portfolio_summary)[0]

        self.assertEqual(result['purchase_outflows'], Decimal('205'))
        self.assertEqual(result['sale_inflows'], Decimal('118'))
        self.assertEqual(result['total_profit'], Decimal('43'))
        self.assertEqual(result['source'], 'Transakcje i bieżąca wycena')

    def test_actual_profit_is_unknown_when_open_position_has_no_valuation(self):
        user = User.objects.create_user(username='missing-price-user', password='pass123')
        account = BrokerageAccount.objects.create(
            user=user,
            name='XTB USD',
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency='USD',
        )
        instrument = BrokerageInstrument.objects.create(
            user=user,
            ticker='NOPRICE',
            name='No price',
            currency='USD',
        )
        BrokerageTransaction.objects.create(
            account=account,
            instrument=instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 1),
            quantity='1',
            price='100',
        )
        BrokerageCashOperation.objects.create(
            account=account,
            instrument=instrument,
            operation_type=BrokerageCashOperation.BUY,
            occurred_at=datetime(2026, 1, 1, 12, tzinfo=datetime_timezone.utc),
            amount='-100',
            currency='USD',
        )
        portfolio_summary = {
            'positions': [{
                'account': account,
                'instrument': instrument,
                'current_value': None,
                'cost': Decimal('100'),
                'profit': None,
            }],
        }

        result = actual_profit_by_currency(user, instrument, portfolio_summary)[0]

        self.assertFalse(result['valuation_complete'])
        self.assertIsNone(result['current_value'])
        self.assertIsNone(result['total_profit'])
        self.assertIsNone(result['return_percent'])
