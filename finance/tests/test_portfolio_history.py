from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from finance.brokerage import build_portfolio_summary
from finance.market_data import MarketDataError
from finance.models import (
    BrokerageAccount,
    BrokerageCashOperation,
    BrokerageDailyPrice,
    BrokerageInstrument,
    BrokeragePositionSnapshot,
    BrokerageTransaction,
)
from finance.portfolio_history import build_portfolio_value_history


User = get_user_model()


class PortfolioValueHistoryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='portfolio-history', password='pass123')
        self.client.login(username='portfolio-history', password='pass123')

    def tearDown(self):
        cache.clear()

    def _at(self, day, hour=12):
        value = datetime.combine(day, time(hour=hour))
        return timezone.make_aware(value)

    def _account(self, name, currency):
        return BrokerageAccount.objects.create(
            user=self.user,
            name=name,
            broker=BrokerageAccount.BROKER_XTB,
            currency=currency,
        )

    def _instrument(self, ticker, currency, **kwargs):
        return BrokerageInstrument.objects.create(
            user=self.user,
            ticker=ticker,
            name=ticker,
            currency=currency,
            **kwargs,
        )

    def _snapshot(self, account, instrument, day, *, quantity, market_value, current_price):
        return BrokeragePositionSnapshot.objects.create(
            account=account,
            instrument=instrument,
            as_of=self._at(day),
            quantity=quantity,
            market_value=market_value,
            current_price=current_price,
            currency=account.currency,
            source='XTB',
        )

    def _cash(self, account, day, amount, operation_type=BrokerageCashOperation.DEPOSIT):
        return BrokerageCashOperation.objects.create(
            account=account,
            operation_type=operation_type,
            occurred_at=self._at(day),
            amount=amount,
            currency=account.currency,
            import_source='test',
        )

    @patch('finance.portfolio_history.fetch_latest_fx_rates_to_pln')
    def test_all_accounts_include_snapshots_cash_and_latest_nbp_rates(self, mock_fx_rates):
        mock_fx_rates.return_value = {
            'rates': {
                'PLN': Decimal('1'),
                'EUR': Decimal('4.5'),
                'USD': Decimal('4.0'),
            },
            'source': 'NBP tabela 150/A/NBP/2026',
            'table_date': '2026-08-11',
        }
        today = timezone.localdate()
        snapshot_day = today - timedelta(days=1)

        pln_account = self._account('XTB PLN', 'PLN')
        eur_account = self._account('XTB EUR', 'EUR')
        usd_account = self._account('XTB USD', 'USD')
        pln = self._instrument('PLN-ONLY', 'PLN')
        eur = self._instrument('EUR-ONLY', 'EUR')
        usd = self._instrument('USD-ONLY', 'USD')

        self._snapshot(pln_account, pln, snapshot_day, quantity='1', market_value='100', current_price='100')
        self._snapshot(eur_account, eur, snapshot_day, quantity='2', market_value='20', current_price='10')
        self._snapshot(usd_account, usd, snapshot_day, quantity='3', market_value='60', current_price='20')
        self._cash(eur_account, snapshot_day, '80')
        self._cash(usd_account, snapshot_day, '40')

        history = build_portfolio_value_history(
            self.user,
            start_date=snapshot_day,
            end_date=today,
        )

        latest = history['points'][-1]
        self.assertEqual(latest['securities_value'], '430.00')
        self.assertEqual(latest['cash_value'], '520.00')
        self.assertEqual(latest['total_value'], '950.00')
        self.assertEqual(latest['value'], '950.00')
        self.assertEqual(history['summary']['latest_total_value'], '950.00')
        self.assertEqual(history['currency'], 'PLN')
        self.assertEqual(history['fx_source'], 'NBP tabela 150/A/NBP/2026')
        self.assertEqual(history['fx_mode'], 'latest_nbp')
        self.assertTrue(history['coverage']['is_complete'])
        mock_fx_rates.assert_called_once_with({'PLN', 'EUR', 'USD'})

        summary = build_portfolio_summary(self.user)
        expected = sum(
            item['total_value'] * mock_fx_rates.return_value['rates'][item['currency']]
            for item in summary['currency_totals']
        )
        self.assertEqual(Decimal(latest['total_value']), expected)

    @patch('finance.portfolio_history.fetch_latest_fx_rates_to_pln')
    def test_pln_account_foreign_instrument_keeps_xtb_snapshot_conversion(self, mock_fx_rates):
        mock_fx_rates.return_value = {
            'rates': {'PLN': Decimal('1'), 'USD': Decimal('3.5')},
            'source': 'NBP test',
            'table_date': timezone.localdate().isoformat(),
        }
        today = timezone.localdate()
        snapshot_day = today - timedelta(days=1)
        account = self._account('XTB IKE', 'PLN')
        instrument = self._instrument(
            'USD-IN-IKE',
            'USD',
            last_price='110',
            last_price_at=self._at(today),
        )
        self._snapshot(
            account,
            instrument,
            snapshot_day,
            quantity='2',
            market_value='800',
            current_price='100',
        )

        history = build_portfolio_value_history(
            self.user,
            start_date=snapshot_day,
            end_date=today,
            selected_account=account,
        )
        summary = build_portfolio_summary(self.user, selected_account=account)

        self.assertEqual(history['points'][0]['total_value'], '800.00')
        self.assertEqual(history['points'][-1]['total_value'], '880.00')
        self.assertEqual(
            Decimal(history['points'][-1]['total_value']),
            summary['currency_totals'][0]['total_value'],
        )

    def test_cash_is_included_and_seeded_before_selected_range(self):
        account = self._account('XTB PLN', 'PLN')
        instrument = self._instrument('CASH-HISTORY', 'PLN')
        BrokerageTransaction.objects.create(
            account=account,
            instrument=instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 1),
            quantity='4',
            price='100',
        )
        self._cash(account, date(2026, 1, 1), '1000')
        self._cash(account, date(2026, 1, 1), '-400', BrokerageCashOperation.BUY)
        self._cash(account, date(2026, 1, 3), '10', BrokerageCashOperation.DIVIDEND)
        self._cash(account, date(2026, 1, 3), '-1.90', BrokerageCashOperation.WITHHOLDING_TAX)
        BrokerageDailyPrice.objects.create(
            instrument=instrument,
            trading_date=date(2026, 1, 2),
            close='110',
            currency='PLN',
            source='Test',
        )
        BrokerageDailyPrice.objects.create(
            instrument=instrument,
            trading_date=date(2026, 1, 3),
            close='120',
            currency='PLN',
            source='Test',
        )

        history = build_portfolio_value_history(
            self.user,
            start_date=date(2026, 1, 2),
            end_date=date(2026, 1, 3),
            selected_account=account,
        )

        self.assertEqual(
            history['points'][0],
            {
                'date': '2026-01-02',
                'value': '1040.00',
                'securities_value': '440.00',
                'cash_value': '600.00',
                'total_value': '1040.00',
                'is_complete': True,
            },
        )
        self.assertEqual(history['points'][1]['securities_value'], '480.00')
        self.assertEqual(history['points'][1]['cash_value'], '608.10')
        self.assertEqual(history['points'][1]['total_value'], '1088.10')

    def test_snapshot_only_account_produces_value_without_transactions(self):
        today = timezone.localdate()
        account = self._account('XTB snapshot', 'PLN')
        instrument = self._instrument('SNAPSHOT-ONLY', 'PLN')
        self._snapshot(
            account,
            instrument,
            today,
            quantity='2',
            market_value='540',
            current_price='270',
        )

        history = build_portfolio_value_history(
            self.user,
            start_date=today - timedelta(days=1),
            end_date=today,
            selected_account=account,
        )

        self.assertEqual(history['points'][-1]['securities_value'], '540.00')
        self.assertEqual(history['points'][-1]['cash_value'], '0.00')
        self.assertEqual(history['points'][-1]['total_value'], '540.00')
        self.assertEqual(history['summary']['latest_value'], '540.00')

    @patch('finance.portfolio_history.fetch_latest_fx_rates_to_pln')
    def test_nbp_failure_falls_back_to_credible_transaction_rate(self, mock_fx_rates):
        mock_fx_rates.side_effect = MarketDataError('NBP chwilowo niedostępne')
        pln_account = self._account('XTB PLN fallback', 'PLN')
        eur_account = self._account('XTB EUR fallback', 'EUR')
        pln = self._instrument('PLN-FALLBACK', 'PLN', last_price='100')
        eur = self._instrument('EUR-FALLBACK', 'EUR', last_price='20')
        BrokerageTransaction.objects.create(
            account=pln_account,
            instrument=pln,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 1),
            quantity='1',
            price='100',
        )
        BrokerageTransaction.objects.create(
            account=eur_account,
            instrument=eur,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 1),
            quantity='1',
            price='20',
            fx_rate_to_pln='4.5',
        )

        history = build_portfolio_value_history(
            self.user,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 2),
        )

        self.assertEqual(history['points'][-1]['total_value'], '190.00')
        self.assertEqual(history['fx_source'], 'Kursy zapisane przy transakcjach')
        self.assertTrue(any('NBP' in warning and 'transakcjach' in warning for warning in history['warnings']))

    def test_endpoint_cache_changes_after_cash_and_new_snapshot(self):
        today = timezone.localdate()
        account = self._account('XTB cache', 'PLN')
        instrument = self._instrument('CACHE-SNAPSHOT', 'PLN')
        self._snapshot(
            account,
            instrument,
            today - timedelta(days=1),
            quantity='1',
            market_value='100',
            current_price='100',
        )
        url = reverse('finance:brokerage_value_history_data')
        params = {'account': account.id, 'days': 30}

        first = self.client.get(url, params).json()
        self.assertEqual(first['summary']['latest_total_value'], '100.00')

        self._cash(account, today, '10')
        second = self.client.get(url, params).json()
        self.assertEqual(second['summary']['latest_total_value'], '110.00')

        self._snapshot(
            account,
            instrument,
            today,
            quantity='1',
            market_value='120',
            current_price='120',
        )
        third = self.client.get(url, params).json()
        self.assertEqual(third['summary']['latest_total_value'], '130.00')
