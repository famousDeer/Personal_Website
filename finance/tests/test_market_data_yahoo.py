from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from finance.market_data import (
    YahooFinanceClient,
    _yahoo_symbol_candidates,
    fetch_historical_market_prices,
    fetch_latest_market_price,
    refresh_market_data_for_user,
)
from finance.models import (
    BrokerageAccount,
    BrokerageInstrument,
    BrokeragePositionSnapshot,
    BrokeragePriceSnapshot,
)


User = get_user_model()


def _chart(
    symbol,
    price,
    currency,
    exchange,
    timestamps=None,
    closes=None,
    adjusted_closes=None,
):
    timestamps = timestamps or [1786345200, 1786431600]
    closes = closes or [float(price) - 1, float(price)]
    indicators = {'quote': [{'close': closes}]}
    if adjusted_closes is not None:
        indicators['adjclose'] = [{'adjclose': adjusted_closes}]
    return {
        'meta': {
            'symbol': symbol,
            'currency': currency,
            'exchangeName': exchange,
            'longName': f'{symbol} instrument',
            'regularMarketPrice': float(price),
            'regularMarketTime': timestamps[-1] + 3600,
        },
        'timestamp': timestamps,
        'indicators': indicators,
    }


def _spark_payload(*charts):
    return {
        'spark': {
            'result': [
                {
                    'symbol': chart['meta']['symbol'],
                    'response': [chart],
                }
                for chart in charts
            ],
            'error': None,
        },
    }


class YahooSymbolTests(TestCase):
    def test_xtb_symbols_are_translated_to_yahoo_symbols(self):
        cases = {
            'AAPL.US': 'AAPL',
            'ASB.PL': 'ASB.WA',
            'ASML.NL': 'ASML.AS',
            'LYPS.PL': 'ETFSP500.WA',
            'SAP.DE': 'SAP.DE',
            'UBI.FR': 'UBI.PA',
            'CNDX.UK': 'CNDX.L',
        }

        for xtb_symbol, yahoo_symbol in cases.items():
            with self.subTest(xtb_symbol=xtb_symbol):
                self.assertEqual(_yahoo_symbol_candidates(xtb_symbol)[0], yahoo_symbol)

        # Account currency must not turn a London-listed ETF into a GPW symbol.
        candidates = _yahoo_symbol_candidates('EIMI.UK', currency='PLN')
        self.assertEqual(candidates[0], 'EIMI.L')
        self.assertNotIn('EIMI.WA', candidates)
        self.assertEqual(
            _yahoo_symbol_candidates('LYPS', exchange='XWAR')[0],
            'ETFSP500.WA',
        )

    @patch('finance.market_data._request_json')
    def test_bulk_quote_parses_market_price_currency_and_recent_sessions(self, mock_request_json):
        mock_request_json.return_value = _spark_payload(
            _chart('ASML.AS', Decimal('1554.00'), 'EUR', 'AMS')
        )

        result = YahooFinanceClient().fetch_quotes(['ASML.AS'])['ASML.AS']

        self.assertEqual(result['price'], Decimal('1554.0'))
        self.assertEqual(result['currency'], 'EUR')
        self.assertEqual(result['exchange'], 'AMS')
        self.assertEqual(len(result['points']), 2)
        self.assertEqual(result['points'][-1]['close'], Decimal('1554.0'))
        self.assertIn('query2.finance.yahoo.com/v7/finance/spark', mock_request_json.call_args[0][0])

    def test_chart_history_preserves_adjusted_close_for_return_statistics(self):
        chart = _chart(
            'AAPL',
            Decimal('102'),
            'USD',
            'NMS',
            closes=[100.0, 102.0],
            adjusted_closes=[98.5, 100.47],
        )

        result = YahooFinanceClient()._parse_chart(
            chart,
            'AAPL',
            include_current=False,
        )

        self.assertEqual(result['points'][0]['close'], Decimal('100.0'))
        self.assertEqual(result['points'][0]['adjusted_close'], Decimal('98.5'))
        self.assertEqual(result['points'][1]['adjusted_close'], Decimal('100.47'))

    @patch('finance.market_data._request_json')
    def test_bulk_quotes_are_split_into_provider_safe_batches(self, mock_request_json):
        symbols = [f'TEST{index}' for index in range(21)]
        mock_request_json.side_effect = [
            _spark_payload(*[_chart(symbol, Decimal('10'), 'USD', 'NMS') for symbol in symbols[:20]]),
            _spark_payload(_chart(symbols[-1], Decimal('10'), 'USD', 'NMS')),
        ]

        quotes = YahooFinanceClient().fetch_quotes(symbols)

        self.assertEqual(len(quotes), 21)
        self.assertEqual(mock_request_json.call_count, 2)

    @patch('finance.market_data.YahooFinanceClient.fetch_quotes')
    def test_explicit_xtb_price_symbol_uses_yahoo_without_openfigi(self, mock_fetch_quotes):
        mock_fetch_quotes.return_value = {
            'AAPL': {
                'price': Decimal('305.00'),
                'observed_at': datetime(2026, 8, 11, 18, tzinfo=datetime_timezone.utc),
                'points': [],
                'symbol': 'AAPL',
                'currency': 'USD',
                'exchange': 'NMS',
                'name': 'Apple Inc.',
            },
        }

        result = fetch_latest_market_price(
            symbol='AAPL.US',
            price_symbol='AAPL.US',
            isin='US0378331005',
            currency='USD',
        )

        self.assertEqual(result['price'], Decimal('305.00'))
        self.assertEqual(result['source'], 'Yahoo Finance')
        self.assertEqual(result['price_symbol'], 'AAPL')
        self.assertEqual(mock_fetch_quotes.call_args[0][0][0], 'AAPL')


@override_settings(ALPHA_VANTAGE_API_KEY='alpha-key')
class YahooPortfolioRefreshTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='yahoo-refresh', password='pass123')
        self.account = BrokerageAccount.objects.create(
            user=self.user,
            name='XTB IKE',
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.IKE,
            currency='PLN',
        )
        self.active = BrokerageInstrument.objects.create(
            user=self.user,
            ticker='EIMI.UK',
            price_symbol='EIMI.UK',
            name='Core MSCI EM IMI',
            exchange='',
            asset_type=BrokerageInstrument.ETF,
            currency='PLN',
        )
        self.inactive = BrokerageInstrument.objects.create(
            user=self.user,
            ticker='LYPS',
            price_symbol='LYPS.PL',
            name='S&P 500 Swap',
            exchange='XWAR',
            asset_type=BrokerageInstrument.ETF,
            currency='PLN',
        )
        BrokeragePositionSnapshot.objects.create(
            account=self.account,
            instrument=self.active,
            as_of=datetime(2026, 8, 10, 20, tzinfo=datetime_timezone.utc),
            quantity='10',
            market_value='2000.00',
            current_price='53.62',
            profit='100.00',
            profit_percent='5.2632',
            currency='PLN',
            source='XTB',
        )

    @patch('finance.market_data.AlphaVantageClient.fetch_dividends')
    @patch('finance.market_data._request_json')
    def test_refresh_updates_only_open_positions_in_one_bulk_request(
        self,
        mock_request_json,
        mock_fetch_dividends,
    ):
        mock_request_json.return_value = _spark_payload(
            _chart('EIMI.L', Decimal('53.85'), 'USD', 'LSE')
        )

        result = refresh_market_data_for_user(self.user)

        self.active.refresh_from_db()
        self.inactive.refresh_from_db()
        self.assertEqual(result['updated_quotes'], 1)
        self.assertEqual(result['inactive_skipped'], 1)
        self.assertEqual(result['failed_quotes'], [])
        self.assertFalse(result['dividends_checked'])
        self.assertEqual(mock_request_json.call_count, 1)
        mock_fetch_dividends.assert_not_called()
        self.assertEqual(self.active.price_symbol, 'EIMI.L')
        self.assertEqual(self.active.currency, 'USD')
        self.assertEqual(self.active.last_price, Decimal('53.8500'))
        self.assertEqual(self.active.market_data_source, 'Yahoo Finance')
        self.assertIsNone(self.inactive.last_price)
        self.assertEqual(
            BrokeragePriceSnapshot.objects.filter(
                instrument=self.active,
                source='Yahoo Finance',
            ).count(),
            2,
        )

    @patch('finance.market_data._request_json')
    def test_same_market_day_updates_snapshot_instead_of_duplicating_it(self, mock_request_json):
        first = _chart('EIMI.L', Decimal('53.85'), 'USD', 'LSE')
        second = _chart('EIMI.L', Decimal('54.10'), 'USD', 'LSE')
        second['meta']['regularMarketTime'] += 900
        second['indicators']['quote'][0]['close'][-1] = 54.10
        mock_request_json.side_effect = [_spark_payload(first), _spark_payload(second)]

        refresh_market_data_for_user(self.user)
        refresh_market_data_for_user(self.user)

        snapshots = BrokeragePriceSnapshot.objects.filter(
            instrument=self.active,
            source='Yahoo Finance',
        ).order_by('observed_at')
        self.assertEqual(snapshots.count(), 2)
        self.assertEqual(snapshots.last().price, Decimal('54.100000'))

    @patch('finance.market_data._request_json')
    def test_explicit_price_symbol_uses_yahoo_for_history(self, mock_request_json):
        chart = _chart('EIMI.L', Decimal('53.85'), 'USD', 'LSE')
        chart['meta'].pop('regularMarketPrice')
        chart['meta'].pop('regularMarketTime')
        mock_request_json.return_value = {'chart': {'result': [chart], 'error': None}}

        result = fetch_historical_market_prices(
            symbol='EIMI.UK',
            price_symbol='EIMI.UK',
            currency='PLN',
            start_date=datetime(2026, 8, 1).date(),
            end_date=datetime(2026, 8, 12).date(),
        )

        self.assertEqual(result['source'], 'Yahoo Finance')
        self.assertEqual(result['price_symbol'], 'EIMI.L')
        self.assertEqual(len(result['points']), 2)
        self.assertIn('/EIMI.L?', mock_request_json.call_args[0][0])
