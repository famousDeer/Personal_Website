from datetime import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from finance.brokerage import build_portfolio_summary
from finance.models import (
    BrokerageAccount,
    BrokerageCashOperation,
    BrokerageInstrument,
    BrokeragePositionSnapshot,
    BrokeragePriceSnapshot,
)


User = get_user_model()


class BrokerageSummaryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='summary-investor', password='pass123')
        self.account = BrokerageAccount.objects.create(
            user=self.user,
            name='XTB PLN',
            broker=BrokerageAccount.BROKER_XTB,
            currency='PLN',
        )
        self.instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker='KRU',
            name='Kruk',
            asset_type=BrokerageInstrument.STOCK,
            currency='PLN',
        )

    def _at(self, day, hour=12):
        return timezone.make_aware(datetime(2026, 8, day, hour, 0))

    def test_latest_broker_snapshot_cash_and_daily_move_build_account_value(self):
        BrokeragePositionSnapshot.objects.create(
            account=self.account,
            instrument=self.instrument,
            as_of=self._at(9),
            quantity='10',
            market_value='450.00',
            current_price='45.00',
            profit='50.00',
            profit_percent='12.50',
            currency='PLN',
            source='XTB',
        )
        BrokeragePositionSnapshot.objects.create(
            account=self.account,
            instrument=self.instrument,
            as_of=self._at(10),
            quantity='10',
            market_value='500.00',
            current_price='50.00',
            profit='100.00',
            profit_percent='25.00',
            currency='PLN',
            source='XTB',
        )
        BrokeragePriceSnapshot.objects.create(
            instrument=self.instrument,
            observed_at=self._at(9),
            price='45.00',
            source='XTB',
        )
        BrokeragePriceSnapshot.objects.create(
            instrument=self.instrument,
            observed_at=self._at(10),
            price='50.00',
            source='XTB',
        )
        operations = [
            (BrokerageCashOperation.DEPOSIT, '1000.00'),
            (BrokerageCashOperation.INTERNAL_TRANSFER, '200.00'),
            (BrokerageCashOperation.BUY, '-600.00'),
            (BrokerageCashOperation.DIVIDEND, '10.00'),
            (BrokerageCashOperation.WITHHOLDING_TAX, '-1.90'),
        ]
        for index, (operation_type, amount) in enumerate(operations):
            BrokerageCashOperation.objects.create(
                account=self.account,
                operation_type=operation_type,
                occurred_at=self._at(10, index + 1),
                amount=amount,
                currency='PLN',
                import_source='xtb',
                external_id=f'cash-{index}',
            )

        summary = build_portfolio_summary(self.user)

        totals = summary['currency_totals'][0]
        self.assertEqual(totals['securities_value'], Decimal('500.00'))
        self.assertEqual(totals['cash_balance'], Decimal('608.10'))
        self.assertEqual(totals['total_value'], Decimal('1108.10'))
        self.assertEqual(totals['net_contributions'], Decimal('1000.00'))
        self.assertEqual(totals['net_income'], Decimal('8.10'))
        self.assertEqual(totals['day_change'], Decimal('50.00'))
        self.assertEqual(len(summary['positions']), 1)
        self.assertEqual(summary['positions'][0]['source'], 'XTB')
        self.assertEqual(summary['positions'][0]['day_change_percent'], Decimal('11.11'))
        self.assertEqual(summary['positions'][0]['day_impact'], Decimal('50.00'))
        self.assertEqual(summary['positions'][0]['allocation_percent'], Decimal('100.00'))
        self.assertEqual(summary['top_gainers'][0]['impact'], Decimal('50.00'))
        self.assertEqual(summary['top_gainers'][0]['change_percent'], Decimal('11.11'))
        self.assertEqual(summary['top_losers'], [])
        self.assertEqual(summary['account_totals'][0]['net_contributions'], Decimal('1200.00'))
        self.assertEqual(summary['portfolio_health']['positions_count'], 1)
        self.assertEqual(summary['portfolio_health']['priced_positions_count'], 1)
        self.assertEqual(summary['portfolio_health']['daily_coverage_count'], 1)
        self.assertEqual(summary['portfolio_health']['valuation_coverage_percent'], Decimal('100.00'))

    def test_only_latest_complete_snapshot_batch_and_selected_account_are_used(self):
        old_only_instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker='OLD',
            name='Closed position',
            currency='PLN',
        )
        for instrument in (self.instrument, old_only_instrument):
            BrokeragePositionSnapshot.objects.create(
                account=self.account,
                instrument=instrument,
                as_of=self._at(9),
                quantity='2',
                market_value='200.00',
                current_price='100.00',
                profit='20.00',
                currency='PLN',
                source='XTB',
            )
        BrokeragePositionSnapshot.objects.create(
            account=self.account,
            instrument=self.instrument,
            as_of=self._at(10),
            quantity='3',
            market_value='330.00',
            current_price='110.00',
            profit='30.00',
            currency='PLN',
            source='XTB',
        )
        other_account = BrokerageAccount.objects.create(
            user=self.user,
            name='XTB EUR',
            broker=BrokerageAccount.BROKER_XTB,
            currency='EUR',
        )
        BrokerageCashOperation.objects.create(
            account=other_account,
            operation_type=BrokerageCashOperation.DEPOSIT,
            occurred_at=self._at(10),
            amount='999.00',
            currency='EUR',
        )

        summary = build_portfolio_summary(self.user, selected_account=self.account)

        self.assertEqual([item['instrument'].ticker for item in summary['positions']], ['KRU'])
        self.assertEqual(summary['securities_value'], Decimal('330.00'))
        self.assertEqual(summary['cash_balance'], Decimal('0.00'))
        self.assertEqual([item['currency'] for item in summary['currency_totals']], ['PLN'])

    def test_daily_impact_uses_conversion_implied_by_account_value(self):
        foreign_instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker='AAPL.US',
            name='Apple',
            currency='USD',
        )
        BrokeragePositionSnapshot.objects.create(
            account=self.account,
            instrument=foreign_instrument,
            as_of=self._at(10),
            quantity='2',
            market_value='800.00',
            current_price='100.00',
            profit='80.00',
            currency='PLN',
            source='XTB',
        )
        BrokeragePriceSnapshot.objects.create(
            instrument=foreign_instrument,
            observed_at=self._at(9),
            price='90.00',
            source='XTB',
        )
        BrokeragePriceSnapshot.objects.create(
            instrument=foreign_instrument,
            observed_at=self._at(10),
            price='100.00',
            source='XTB',
        )

        mover = build_portfolio_summary(self.user)['daily_movers'][0]

        self.assertEqual(mover['change'], Decimal('10.00'))
        self.assertEqual(mover['impact'], Decimal('80.00'))
        self.assertEqual(mover['currency'], 'PLN')
        self.assertEqual(mover['price_currency'], 'USD')

    def test_newer_market_quote_advances_an_imported_position_value(self):
        self.instrument.last_price = Decimal('55.00')
        self.instrument.last_price_at = self._at(10)
        self.instrument.market_data_source = 'Stooq'
        self.instrument.save(update_fields=['last_price', 'last_price_at', 'market_data_source'])
        BrokeragePositionSnapshot.objects.create(
            account=self.account,
            instrument=self.instrument,
            as_of=self._at(9),
            quantity='10',
            market_value='500.00',
            current_price='50.00',
            profit='100.00',
            currency='PLN',
            source='XTB',
        )

        summary = build_portfolio_summary(self.user)
        position = summary['positions'][0]

        self.assertEqual(position['current_price'], Decimal('55.00'))
        self.assertEqual(position['current_value'], Decimal('550.00'))
        self.assertEqual(position['unrealized'], Decimal('150.00'))
        self.assertEqual(position['source'], 'Stooq')
        self.assertEqual(position['as_of'], self._at(10))
