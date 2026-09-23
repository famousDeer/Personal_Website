from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError
from zipfile import ZipFile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.utils import timezone
from decimal import Decimal
from finance.models import (
    BrokerageAccount,
    BrokerageDailyPrice,
    BrokerageDividend,
    BrokerageInstrument,
    BrokerageTransaction,
    Daily,
    Income,
    Monthly,
)
from finance.market_data import (
    MarketDataError,
    fetch_latest_fx_rates_to_pln,
    fetch_historical_market_prices,
    fetch_latest_market_price,
    fetch_transaction_market_price,
    refresh_market_data_for_user,
)
from finance.serializers import MonthlySerializer
from datetime import date, time, timedelta

User = get_user_model()


def _xlsx_inline_string_cell(ref, value):
    return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'


def _minimal_xtb_xlsx(rows):
    buffer = BytesIO()
    with ZipFile(buffer, 'w') as archive:
        archive.writestr('[Content_Types].xml', (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>'
        ))
        archive.writestr('_rels/.rels', (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'
        ))
        archive.writestr('xl/workbook.xml', (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="OPEN POSITION HISTORY" sheetId="1" r:id="rId1"/></sheets>'
            '</workbook>'
        ))
        archive.writestr('xl/_rels/workbook.xml.rels', (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>'
        ))
        xml_rows = []
        for row_number, row in enumerate(rows, start=1):
            cells = ''.join(
                _xlsx_inline_string_cell(f'{chr(65 + index)}{row_number}', value)
                for index, value in enumerate(row)
            )
            xml_rows.append(f'<row r="{row_number}">{cells}</row>')
        archive.writestr('xl/worksheets/sheet1.xml', (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(xml_rows)}</sheetData>'
            '</worksheet>'
        ))
    return buffer.getvalue()


def _xtb_upload():
    content = _minimal_xtb_xlsx([
        ['Position', 'Symbol', 'Type', 'Volume', 'Open time', 'Open price', 'Market price', 'Commission'],
        ['123456', 'KRU.PL', 'BUY', '3.0000', '2026-01-02 10:30:00', '100.00', '110.00', '5.00'],
    ])
    return SimpleUploadedFile(
        'xtb.xlsx',
        content,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


class BrokerageMarketDataTests(TestCase):
    @override_settings(OPENFIGI_API_KEY="openfigi-key", ALPHA_VANTAGE_API_KEY="alpha-key", STOOQ_API_KEY="stooq-key")
    @patch("finance.market_data._request_json")
    def test_latest_price_resolves_isin_with_openfigi_and_fetches_quote_from_alpha_vantage(self, mock_request_json):
        mock_request_json.side_effect = [
            [{
                "data": [{
                    "ticker": "AAPL",
                    "name": "Apple Inc.",
                    "marketSector": "Equity",
                    "securityType2": "Common Stock",
                    "exchCode": "US",
                    "micCode": "XNAS",
                }]
            }],
            {"Global Quote": {"05. price": "189.1200"}},
        ]

        result = fetch_latest_market_price(isin="US0378331005", exchange="NASDAQ", currency="USD")

        self.assertEqual(result["symbol"], "AAPL")
        self.assertEqual(result["price"], Decimal("189.1200"))
        self.assertEqual(result["source"], "Alpha Vantage")
        self.assertEqual(result["exchange"], "XNAS")

    @override_settings(OPENFIGI_API_KEY="openfigi-key", ALPHA_VANTAGE_API_KEY="alpha-key", STOOQ_API_KEY="stooq-key")
    @patch("finance.market_data._read_csv_url")
    @patch("finance.market_data._request_json")
    def test_gpw_transaction_price_uses_stooq_instead_of_alpha_vantage(self, mock_request_json, mock_read_csv):
        mock_request_json.return_value = [{
            "data": [{
                "ticker": "KRU",
                "name": "KRUK S.A.",
                "marketSector": "Equity",
                "securityType2": "Common Stock",
                "exchCode": "PW",
                "micCode": "XWAR",
            }]
        }]
        mock_read_csv.return_value = [{"Date": "2026-04-10", "Close": "481.00"}]

        result = fetch_transaction_market_price("PLKRK0000010", date(2026, 4, 10), time(12, 55), "GPW", "PLN")

        self.assertEqual(result["symbol"], "KRU")
        self.assertEqual(result["price"], Decimal("481.00"))
        self.assertEqual(result["source"], "Stooq dzienne zamknięcie")
        self.assertIn("kru.pl", mock_read_csv.call_args[0][0])

    @override_settings(OPENFIGI_API_KEY="openfigi-key", ALPHA_VANTAGE_API_KEY="alpha-key", STOOQ_API_KEY="stooq-key")
    @patch("finance.market_data._read_csv_url")
    @patch("finance.market_data._request_json")
    def test_gpw_transaction_price_falls_back_to_latest_stooq_quote(self, mock_request_json, mock_read_csv):
        mock_request_json.return_value = [{
            "data": [{
                "ticker": "KRU",
                "name": "KRUK S.A.",
                "marketSector": "Equity",
                "securityType2": "Common Stock",
                "exchCode": "PW",
                "micCode": "XWAR",
            }]
        }]
        mock_read_csv.side_effect = [
            [],
            [],
            [],
            [],
            [],
            [],
            [{"Symbol": "KRU.PL", "Close": "484.20"}],
        ]

        result = fetch_transaction_market_price("PLKRK0000010", date(2026, 4, 10), time(12, 55), "GPW", "PLN")

        self.assertEqual(result["price"], Decimal("484.20"))
        self.assertEqual(result["source"], "Stooq najnowsza cena")
        self.assertIn("kru.pl", mock_read_csv.call_args_list[-1][0][0])

    @override_settings(OPENFIGI_API_KEY="openfigi-key", ALPHA_VANTAGE_API_KEY="alpha-key")
    @patch("finance.market_data._read_csv_url")
    @patch("finance.market_data._request_json")
    def test_gpw_latest_price_keeps_stooq_quote(self, mock_request_json, mock_read_csv):
        mock_request_json.return_value = [{
            "data": [{
                "ticker": "KRU",
                "name": "KRUK S.A.",
                "marketSector": "Equity",
                "securityType2": "Common Stock",
                "exchCode": "PW",
                "micCode": "XWAR",
            }]
        }]
        mock_read_csv.return_value = [{"Symbol": "KRU.PL", "Close": "484.20"}]

        result = fetch_latest_market_price(isin="PLKRK0000010", exchange="GPW", currency="PLN")

        self.assertEqual(result["price"], Decimal("484.20"))
        self.assertEqual(result["source"], "Stooq")

    @override_settings(OPENFIGI_API_KEY="openfigi-key", ALPHA_VANTAGE_API_KEY="alpha-key")
    @patch("finance.market_data._read_csv_url")
    @patch("finance.market_data._request_json")
    def test_gpw_latest_price_tries_stooq_symbol_variants(self, mock_request_json, mock_read_csv):
        mock_request_json.return_value = [{
            "data": [{
                "ticker": "KRU",
                "name": "KRUK S.A.",
                "marketSector": "Equity",
                "securityType2": "Common Stock",
                "exchCode": "PW",
                "micCode": "XWAR",
            }]
        }]
        mock_read_csv.side_effect = [
            [{"Symbol": "KRU.PL", "Close": "N/D"}],
            [{"Symbol": "KRU", "Kurs": "484.20"}],
        ]

        result = fetch_latest_market_price(isin="PLKRK0000010", exchange="GPW", currency="PLN")

        self.assertEqual(result["price"], Decimal("484.20"))
        self.assertIn("kru.pl", mock_read_csv.call_args_list[0][0][0])
        self.assertIn("kru", mock_read_csv.call_args_list[1][0][0])

    @override_settings(OPENFIGI_API_KEY="openfigi-key", ALPHA_VANTAGE_API_KEY="alpha-key")
    @patch("finance.market_data._read_csv_url")
    @patch("finance.market_data._request_json")
    def test_gpw_latest_price_error_includes_stooq_response_preview(self, mock_request_json, mock_read_csv):
        mock_request_json.return_value = [{
            "data": [{
                "ticker": "KRU",
                "name": "KRUK S.A.",
                "marketSector": "Equity",
                "securityType2": "Common Stock",
                "exchCode": "PW",
                "micCode": "XWAR",
            }]
        }]
        mock_read_csv.return_value = [{"Symbol": "KRU.PL", "Close": "N/D"}]

        with self.assertRaisesRegex(MarketDataError, "Close=N/D") as error:
            fetch_latest_market_price(isin="PLKRK0000010", exchange="GPW", currency="PLN")
        self.assertIn("OpenFIGI rozpoznało KRU", str(error.exception))
        self.assertNotIn("wpisz znaleziony symbol", str(error.exception))

    @override_settings(OPENFIGI_API_KEY="openfigi-key", ALPHA_VANTAGE_API_KEY="alpha-key")
    @patch("finance.market_data._read_csv_url")
    @patch("finance.market_data._request_json")
    def test_refresh_market_data_updates_gpw_instrument_with_latest_stooq_quote(self, mock_request_json, mock_read_csv):
        user = User.objects.create_user(username="market-user", password="pass123")
        instrument = BrokerageInstrument.objects.create(
            user=user,
            ticker="KRU",
            name="Kruk",
            isin="PLKRK0000010",
            exchange="XWAR",
            asset_type=BrokerageInstrument.STOCK,
            currency="PLN",
        )
        mock_request_json.return_value = [{
            "data": [{
                "ticker": "KRU",
                "name": "KRUK S.A.",
                "marketSector": "Equity",
                "securityType2": "Common Stock",
                "exchCode": "PW",
                "micCode": "XWAR",
            }]
        }]
        mock_read_csv.return_value = [{"Symbol": "KRU.PL", "Close": "484.20"}]

        result = refresh_market_data_for_user(user)

        instrument.refresh_from_db()
        self.assertEqual(result["updated_quotes"], 1)
        self.assertEqual(result["failed_quotes"], [])
        self.assertEqual(instrument.last_price, Decimal("484.2000"))
        self.assertEqual(instrument.market_data_source, "Stooq")

    @override_settings(OPENFIGI_API_KEY="openfigi-key", ALPHA_VANTAGE_API_KEY="alpha-key")
    @patch("finance.market_data._read_csv_url")
    @patch("finance.market_data._request_json")
    def test_refresh_market_data_merges_duplicate_instruments_after_isin_resolution(self, mock_request_json, mock_read_csv):
        user = User.objects.create_user(username="duplicate-market-user", password="pass123")
        account_one = BrokerageAccount.objects.create(
            user=user,
            name="XTB PLN",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="PLN",
        )
        account_two = BrokerageAccount.objects.create(
            user=user,
            name="mBank",
            broker=BrokerageAccount.BROKER_MBANK,
            account_type=BrokerageAccount.STANDARD,
            currency="PLN",
        )
        canonical = BrokerageInstrument.objects.create(
            user=user,
            ticker="PZU",
            name="PZU",
            isin="PLPZU0000011",
            exchange="XWAR",
            asset_type=BrokerageInstrument.STOCK,
            currency="PLN",
        )
        duplicate = BrokerageInstrument.objects.create(
            user=user,
            ticker="PLPZU0000011",
            name="PZU",
            isin="PLPZU0000011",
            exchange="GPW",
            asset_type=BrokerageInstrument.STOCK,
            currency="PLN",
        )
        BrokerageTransaction.objects.create(
            account=account_one,
            instrument=canonical,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 2),
            quantity="2",
            price="50.00",
        )
        moved_transaction = BrokerageTransaction.objects.create(
            account=account_two,
            instrument=duplicate,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 3),
            quantity="3",
            price="51.00",
        )
        mock_request_json.return_value = [{
            "data": [{
                "ticker": "PZU",
                "name": "PZU S.A.",
                "marketSector": "Equity",
                "securityType2": "Common Stock",
                "exchCode": "PW",
                "micCode": "XWAR",
            }]
        }]
        mock_read_csv.return_value = [{"Symbol": "PZU.PL", "Close": "52.40"}]

        result = refresh_market_data_for_user(user)

        moved_transaction.refresh_from_db()
        canonical.refresh_from_db()
        self.assertEqual(result["merged_instruments"], 1)
        self.assertEqual(moved_transaction.instrument, canonical)
        self.assertFalse(BrokerageInstrument.objects.filter(id=duplicate.id).exists())
        self.assertEqual(canonical.last_price, Decimal("52.4000"))

    @override_settings(OPENFIGI_API_KEY="openfigi-key", ALPHA_VANTAGE_API_KEY="alpha-key")
    @patch("finance.market_data._read_csv_url")
    @patch("finance.market_data._request_json")
    def test_refresh_market_data_uses_manual_price_symbol(self, mock_request_json, mock_read_csv):
        user = User.objects.create_user(username="manual-symbol-user", password="pass123")
        instrument = BrokerageInstrument.objects.create(
            user=user,
            ticker="UBI",
            price_symbol="UBI.FR",
            name="Ubisoft",
            isin="FR0000054470",
            exchange="XPAR",
            asset_type=BrokerageInstrument.STOCK,
            currency="EUR",
        )
        mock_request_json.return_value = {
            "spark": {
                "result": [{
                    "symbol": "UBI.PA",
                    "response": [{
                        "meta": {
                            "symbol": "UBI.PA",
                            "currency": "EUR",
                            "exchangeName": "PAR",
                            "regularMarketPrice": 11.42,
                            "regularMarketTime": 1786462514,
                        },
                        "timestamp": [1786345200, 1786431600],
                        "indicators": {"quote": [{"close": [11.10, 11.42]}]},
                    }],
                }],
                "error": None,
            },
        }

        result = refresh_market_data_for_user(user)

        instrument.refresh_from_db()
        self.assertEqual(result["updated_quotes"], 1)
        self.assertEqual(instrument.ticker, "UBI")
        self.assertEqual(instrument.price_symbol, "UBI.PA")
        self.assertEqual(instrument.last_price, Decimal("11.4200"))
        self.assertEqual(instrument.market_data_source, "Yahoo Finance")
        self.assertEqual(result["updated_dividends"], 0)
        mock_read_csv.assert_not_called()

    @override_settings(OPENFIGI_API_KEY="openfigi-key", ALPHA_VANTAGE_API_KEY="alpha-key")
    @patch("finance.market_data._read_csv_url")
    @patch("finance.market_data._request_json")
    def test_partial_yahoo_bulk_response_falls_back_to_chart(self, mock_request_json, mock_read_csv):
        user = User.objects.create_user(username="manual-stooq-symbol-user", password="pass123")
        instrument = BrokerageInstrument.objects.create(
            user=user,
            ticker="UBI",
            price_symbol="UBI.FR",
            name="Ubisoft",
            isin="FR0000054470",
            exchange="XPAR",
            asset_type=BrokerageInstrument.STOCK,
            currency="EUR",
        )
        chart = {
            "meta": {
                "symbol": "UBI.PA",
                "currency": "EUR",
                "exchangeName": "PAR",
                "regularMarketPrice": 11.42,
                "regularMarketTime": 1786462514,
            },
            "timestamp": [1786431600],
            "indicators": {"quote": [{"close": [11.42]}]},
        }
        mock_request_json.side_effect = [
            {"spark": {"result": [], "error": None}},
            {"chart": {"result": [chart], "error": None}},
        ]

        result = refresh_market_data_for_user(user)

        instrument.refresh_from_db()
        self.assertEqual(result["updated_quotes"], 1)
        self.assertEqual(result["failed_quotes"], [])
        self.assertEqual(instrument.last_price, Decimal("11.4200"))
        self.assertEqual(instrument.market_data_source, "Yahoo Finance")
        self.assertEqual(instrument.price_symbol, "UBI.PA")
        mock_read_csv.assert_not_called()

    @override_settings(OPENFIGI_API_KEY="openfigi-key", ALPHA_VANTAGE_API_KEY="alpha-key")
    @patch("finance.market_data.urlopen")
    def test_provider_http_error_is_returned_as_market_data_error(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            url="https://api.openfigi.com/v3/mapping",
            code=402,
            msg="Payment Required",
            hdrs=None,
            fp=None,
        )

        with self.assertRaisesRegex(MarketDataError, "Payment Required"):
            fetch_latest_market_price(isin="PLKRK0000010")

    @override_settings(OPENFIGI_API_KEY="", ALPHA_VANTAGE_API_KEY="alpha-key")
    @patch("finance.market_data._request_json")
    def test_fetch_historical_prices_uses_alpha_vantage_daily_series(self, mock_request_json):
        mock_request_json.return_value = {
            "Time Series (Daily)": {
                "2026-05-15": {
                    "1. open": "190.00",
                    "2. high": "195.00",
                    "3. low": "188.00",
                    "4. close": "194.50",
                    "5. volume": "12345",
                },
                "2026-05-14": {
                    "1. open": "185.00",
                    "2. high": "191.00",
                    "3. low": "184.00",
                    "4. close": "189.25",
                    "5. volume": "10000",
                },
            }
        }

        result = fetch_historical_market_prices(symbol="AAPL", exchange="XNAS", currency="USD")

        self.assertEqual(result["source"], "Alpha Vantage")
        self.assertEqual([point["date"] for point in result["points"]], [date(2026, 5, 14), date(2026, 5, 15)])
        self.assertEqual(result["points"][-1]["close"], Decimal("194.50"))

    @override_settings(STOOQ_API_KEY="stooq-key")
    @patch("finance.market_data._read_csv_url")
    def test_gpw_historical_prices_try_instrument_name_candidates(self, mock_read_csv):
        def response_for_url(url, **kwargs):
            if "s=kruk.pl" in url:
                return [
                    {"Date": "2026-05-14", "Open": "480.00", "High": "486.00", "Low": "478.00", "Close": "484.00", "Volume": "1000"},
                    {"Date": "2026-05-15", "Open": "484.00", "High": "490.00", "Low": "482.00", "Close": "488.00", "Volume": "1200"},
                ]
            return []

        mock_read_csv.side_effect = response_for_url

        result = fetch_historical_market_prices(symbol="KRU", exchange="XWAR", currency="PLN", name="Kruk S.A.")

        requested_urls = [call_args[0][0] for call_args in mock_read_csv.call_args_list]
        self.assertEqual(result["source"], "Stooq")
        self.assertEqual(result["points"][-1]["close"], Decimal("488.00"))
        self.assertTrue(any("s=kru.pl" in url for url in requested_urls))
        self.assertTrue(any("s=kruk.pl" in url for url in requested_urls))

    @override_settings(STOOQ_API_KEY="")
    def test_gpw_historical_prices_explain_missing_stooq_api_key(self):
        with self.assertRaisesRegex(MarketDataError, "STOOQ_API_KEY"):
            fetch_historical_market_prices(symbol="KRU", exchange="XWAR", currency="PLN", name="Kruk S.A.")

    @patch("finance.market_data._request_json")
    def test_latest_fx_rates_to_pln_uses_nbp_table_a(self, mock_request_json):
        mock_request_json.return_value = [{
            "no": "100/A/NBP/2026",
            "effectiveDate": "2026-05-15",
            "rates": [
                {"code": "EUR", "mid": "4.2500"},
                {"code": "USD", "mid": "3.9000"},
            ],
        }]

        result = fetch_latest_fx_rates_to_pln({"PLN", "EUR"})

        self.assertEqual(result["rates"]["PLN"], Decimal("1"))
        self.assertEqual(result["rates"]["EUR"], Decimal("4.2500"))
        self.assertEqual(result["source"], "NBP tabela 100/A/NBP/2026")


class FinanceCoreTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="pass123")
        self.personal_account = self.user.owned_finance_accounts.get(account_type='personal')
        self.client.login(username="u1", password="pass123")

    def test_add_expense_creates_monthly_and_updates_total(self):
        resp = self.client.post(reverse("finance:add_expense"), {
            "date": "2025-06-15",
            "title": "Lunch",
            "category": "Jedzenie",
            "store": "Cafe",
            "cost": "12.50",
        }, follow=True)
        self.assertEqual(resp.status_code, 200)

        monthly = Monthly.objects.get(account=self.personal_account, date=date(2025, 6, 1))
        self.assertEqual(float(monthly.total_expense), 12.50)

        exp = Daily.objects.get(account=self.personal_account, title="Lunch")
        self.assertEqual(float(exp.cost), 12.50)

    def test_add_expense_suggests_existing_stores_for_active_account(self):
        month = Monthly.objects.create(
            user=self.user,
            account=self.personal_account,
            date=date(2025, 6, 1),
            total_income=0,
            total_expense=0,
        )
        Daily.objects.create(
            user=self.user,
            account=self.personal_account,
            date=date(2025, 6, 10),
            title="Zakupy",
            category="Zakupy spozywcze",
            store="Biedronka",
            cost=25,
            month=month,
        )
        Daily.objects.create(
            user=self.user,
            account=self.personal_account,
            date=date(2025, 6, 11),
            title="Zakupy",
            category="Zakupy spozywcze",
            store="  Lidl  ",
            cost=30,
            month=month,
        )
        Daily.objects.create(
            user=self.user,
            account=self.personal_account,
            date=date(2025, 6, 12),
            title="Brak sklepu",
            category="Inne",
            store="",
            cost=10,
            month=month,
        )
        other_user = User.objects.create_user(username="u2", password="pass123")
        other_account = other_user.owned_finance_accounts.get(account_type='personal')
        other_month = Monthly.objects.create(
            user=other_user,
            account=other_account,
            date=date(2025, 6, 1),
            total_income=0,
            total_expense=0,
        )
        Daily.objects.create(
            user=other_user,
            account=other_account,
            date=date(2025, 6, 10),
            title="Inne konto",
            category="Inne",
            store="Rossmann",
            cost=20,
            month=other_month,
        )

        resp = self.client.get(reverse("finance:add_expense"))

        self.assertEqual(resp.context["store_suggestions"], ["Biedronka", "Lidl"])
        self.assertContains(resp, 'data-autocomplete-source="category-suggestions-data"')
        self.assertContains(resp, 'id="category-suggestions-menu"')
        self.assertContains(resp, 'id="category-suggestions-data"')
        self.assertContains(resp, 'data-autocomplete-source="store-suggestions-data"')
        self.assertContains(resp, 'id="store-suggestions-menu"')
        self.assertContains(resp, 'id="store-suggestions-data"')
        self.assertContains(resp, '"Biedronka"')
        self.assertContains(resp, '"Lidl"')
        self.assertNotContains(resp, "Rossmann")

    def test_edit_expense_move_to_other_month_recalculates_both(self):
        # start in May
        may = Monthly.objects.create(user=self.user, account=self.personal_account, date=date(2025, 5, 1), total_income=0, total_expense=0)
        exp = Daily.objects.create(
            user=self.user, account=self.personal_account, date=date(2025, 5, 10), title="Ticket", category="Transport",
            store="", cost=100.0, month=may
        )
        may.total_expense = 100
        may.save()

        # move to June
        resp = self.client.post(reverse("finance:edit_expense", args=[exp.id]), {
            "date": "2025-06-02",
            "title": "Ticket",
            "category": "Transport",
            "store": "",
            "cost": "100",
        }, follow=True)
        self.assertEqual(resp.status_code, 200)

        may.refresh_from_db()
        june = Monthly.objects.get(account=self.personal_account, date=date(2025, 6, 1))
        exp.refresh_from_db()

        self.assertEqual(exp.month, june)
        self.assertEqual(float(may.total_expense), 0.0)
        self.assertEqual(float(june.total_expense), 100.0)

    def test_delete_expense_recalculates_month(self):
        m = Monthly.objects.create(user=self.user, account=self.personal_account, date=date(2025, 7, 1), total_income=0, total_expense=0)
        e1 = Daily.objects.create(user=self.user, account=self.personal_account, date=date(2025, 7, 1), title="A", category="Inne", store="", cost=10, month=m)
        Daily.objects.create(user=self.user, account=self.personal_account, date=date(2025, 7, 2), title="B", category="Inne", store="", cost=5, month=m)
        m.total_expense = 15
        m.save()

        resp = self.client.post(reverse("finance:delete_expense", args=[e1.id]), follow=True)
        self.assertEqual(resp.status_code, 200)

        m.refresh_from_db()
        self.assertEqual(float(m.total_expense), 5.0)
        self.assertFalse(Daily.objects.filter(id=e1.id).exists())

    def test_add_income_updates_month_total(self):
        resp = self.client.post(reverse("finance:add_income"), {
            "date": "2025-06-20",
            "title": "Salary",
            "amount": "3000",
            "source": "Pensja",
        }, follow=True)
        self.assertEqual(resp.status_code, 200)

        monthly = Monthly.objects.get(account=self.personal_account, date=date(2025, 6, 1))
        self.assertEqual(float(monthly.total_income), 3000.0)
        inc = Income.objects.get(account=self.personal_account, title="Salary")
        self.assertEqual(float(inc.amount), 3000.0)

    def test_monthly_serializer_exposes_totals_and_net_savings(self):
        monthly = Monthly.objects.create(
            user=self.user,
            account=self.personal_account,
            date=date(2025, 6, 1),
            total_income=1000,
            total_expense=350,
        )

        data = MonthlySerializer(monthly).data

        self.assertEqual(data["total_income"], "1000.00")
        self.assertEqual(data["total_expense"], "350.00")
        self.assertEqual(str(data["net_savings"]), "650.00")

    def test_edit_income_move_to_other_month_recalculates_both(self):
        may = Monthly.objects.create(user=self.user, account=self.personal_account, date=date(2025, 5, 1), total_income=0, total_expense=0)
        inc = Income.objects.create(user=self.user, account=self.personal_account, date=date(2025, 5, 15), title="Bonus", source="Premia", amount=200.0, month=may)
        may.total_income = 200
        may.save()

        resp = self.client.post(reverse("finance:edit_income", args=[inc.id]), {
            "date": "2025-06-01",
            "title": "Bonus",
            "source": "Premia",
            "amount": "200",
        }, follow=True)
        self.assertEqual(resp.status_code, 200)

        may.refresh_from_db()
        june = Monthly.objects.get(account=self.personal_account, date=date(2025, 6, 1))
        inc.refresh_from_db()

        # Expected: moved and both months recalculated
        self.assertEqual(inc.month, june)
        self.assertEqual(float(may.total_income), 0.0)
        self.assertEqual(float(june.total_income), 200.0)

    def test_dashboard_sets_context(self):
        today = timezone.now().date()
        Monthly.objects.get_or_create(
            account=self.personal_account,
            date=today.replace(day=1),
            defaults={"user": self.user, "total_income": 0, "total_expense": 0},
        )
        resp = self.client.get(reverse("finance:dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("total_income", resp.context)
        self.assertIn("total_expense", resp.context)
        self.assertIn("balance", resp.context)

    def test_dashboard_uses_selected_cost_categories(self):
        today = timezone.now().date()
        month = Monthly.objects.create(user=self.user, account=self.personal_account, date=today.replace(day=1), total_income=0, total_expense=0)
        Daily.objects.create(user=self.user, account=self.personal_account, date=today, title="Paliwo", category="Paliwo", store="", cost=100, month=month)
        Daily.objects.create(user=self.user, account=self.personal_account, date=today, title="Sport", category="Sport", store="", cost=50, month=month)
        Daily.objects.create(user=self.user, account=self.personal_account, date=today, title="Rachunki", category="Rachunki", store="", cost=200, month=month)

        resp = self.client.get(reverse("finance:dashboard"), {"cost_category": ["Sport", "Paliwo"]})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["selected_cost_categories"], ["Sport", "Paliwo"])
        self.assertEqual(float(resp.context["selected_category_total"]), 150.0)

    def test_dashboard_separates_investments_from_expenses(self):
        today = timezone.now().date()
        month = Monthly.objects.create(user=self.user, account=self.personal_account, date=today.replace(day=1), total_income=0, total_expense=300)
        Daily.objects.create(user=self.user, account=self.personal_account, date=today, title="ETF", category="Inwestycje", store="", cost=120, month=month)
        Daily.objects.create(user=self.user, account=self.personal_account, date=today, title="Zakupy", category="Zakupy spozywcze", store="", cost=180, month=month)

        resp = self.client.get(reverse("finance:dashboard"))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(float(resp.context["investment_total"]), 120.0)
        self.assertEqual(float(resp.context["spending_total"]), 180.0)
        self.assertEqual(resp.context["recent_expenses"][0].category, "Zakupy spozywcze")
        self.assertEqual(resp.context["recent_investments"][0].category, "Inwestycje")

    def test_reports_keep_investments_in_balance_but_show_separately(self):
        month = Monthly.objects.create(user=self.user, account=self.personal_account, date=date(2025, 6, 1), total_income=1000, total_expense=400)
        Daily.objects.create(user=self.user, account=self.personal_account, date=date(2025, 6, 10), title="ETF", category="Inwestycje", store="", cost=150, month=month)
        Daily.objects.create(user=self.user, account=self.personal_account, date=date(2025, 6, 11), title="Rachunek", category="Rachunki", store="", cost=250, month=month)

        resp = self.client.get(reverse("finance:reports"))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(float(resp.context["total_investment_all"]), 150.0)
        self.assertEqual(float(resp.context["total_spending_all"]), 250.0)
        self.assertEqual(float(resp.context["balance_all"]), 600.0)

    def test_expense_list_shows_investments_separately(self):
        month = Monthly.objects.create(user=self.user, account=self.personal_account, date=date(2025, 6, 1), total_income=0, total_expense=200)
        Daily.objects.create(user=self.user, account=self.personal_account, date=date(2025, 6, 10), title="ETF", category="Inwestycje", store="", cost=150, month=month)
        Daily.objects.create(user=self.user, account=self.personal_account, date=date(2025, 6, 11), title="Obiad", category="Jedzenie na miescie", store="", cost=50, month=month)

        resp = self.client.get(reverse("finance:expense_list"))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(float(resp.context["total_filtered"]), 50.0)
        self.assertEqual(float(resp.context["investment_total_filtered"]), 150.0)
        self.assertEqual(list(resp.context["investments"].values_list("category", flat=True)), ["Inwestycje"])

    def test_transfer_to_shared_account_creates_linked_income(self):
        partner = User.objects.create_user(username="u2", password="pass123")
        shared_account = partner.finance_accounts.filter(account_type='shared').first()
        self.assertIsNone(shared_account)

        resp = self.client.post(reverse("profile"), {
            "form_name": "shared_account",
            "name": "Dom",
            "partner_username": "u2",
        }, follow=True)
        self.assertEqual(resp.status_code, 200)

        shared_account = self.user.finance_accounts.get(account_type='shared')

        resp = self.client.post(reverse("finance:add_expense"), {
            "date": "2025-06-15",
            "title": "Wpłata majowa",
            "category": "Wpłata do wspólnego z mBank",
            "store": "",
            "cost": "250.00",
            "transfer_target_account": str(shared_account.id),
        }, follow=True)
        self.assertEqual(resp.status_code, 200)

        shared_month = Monthly.objects.get(account=shared_account, date=date(2025, 6, 1))
        linked_income = Income.objects.get(account=shared_account, linked_expense__title="Wpłata majowa")

        self.assertEqual(float(shared_month.total_income), 250.0)
        self.assertEqual(float(linked_income.amount), 250.0)

    def test_brokerage_view_shows_positions_dividends_and_tax(self):
        account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB USD",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="USD",
        )
        ike_account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB IKE",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.IKE,
            currency="PLN",
        )
        instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker="AAPL",
            name="Apple",
            asset_type=BrokerageInstrument.STOCK,
            currency="USD",
            last_price="12.00",
        )
        polish_instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker="ETFPL",
            name="ETF Polska",
            asset_type=BrokerageInstrument.ETF,
            currency="PLN",
            last_price="110.00",
        )
        BrokerageTransaction.objects.create(
            account=account,
            instrument=instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 2),
            quantity="10",
            price="10.00",
            fees="0.00",
            fx_rate_to_pln="4.000000",
        )
        BrokerageTransaction.objects.create(
            account=account,
            instrument=instrument,
            transaction_type=BrokerageTransaction.SELL,
            trade_date=date(2026, 1, 10),
            quantity="2",
            price="15.00",
            fees="0.00",
            fx_rate_to_pln="4.000000",
        )
        BrokerageTransaction.objects.create(
            account=ike_account,
            instrument=polish_instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 3),
            quantity="5",
            price="100.00",
            fees="0.00",
        )
        dividend_ex_date = timezone.localdate() + timedelta(days=30)
        dividend_payment_date = dividend_ex_date + timedelta(days=14)
        BrokerageDividend.objects.create(
            account=account,
            instrument=instrument,
            ex_dividend_date=dividend_ex_date,
            payment_date=dividend_payment_date,
            gross_amount_per_share="1.00",
            currency="USD",
        )
        BrokerageDividend.objects.create(
            account=ike_account,
            instrument=polish_instrument,
            ex_dividend_date=dividend_ex_date,
            payment_date=dividend_payment_date,
            gross_amount_per_share="2.00",
            currency="PLN",
        )

        response = self.client.get(reverse("finance:brokerage"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Konta maklerskie")
        currency_totals = {item["currency"]: item for item in response.context["currency_totals"]}
        self.assertEqual(currency_totals["USD"]["value"], Decimal("96.00"))
        self.assertEqual(currency_totals["USD"]["cost"], Decimal("80.00"))
        self.assertEqual(currency_totals["USD"]["unrealized"], Decimal("16.00"))
        self.assertEqual(currency_totals["USD"]["estimated_sell_tax"], Decimal("1.90"))
        self.assertEqual(currency_totals["USD"]["planned_dividend_net"], Decimal("6.48"))
        self.assertEqual(currency_totals["PLN"]["value"], Decimal("550.00"))
        self.assertEqual(currency_totals["PLN"]["cost"], Decimal("500.00"))
        self.assertEqual(currency_totals["PLN"]["unrealized"], Decimal("50.00"))
        self.assertEqual(currency_totals["PLN"]["planned_dividend_net"], Decimal("10.00"))
        self.assertEqual(len(response.context["positions"]), 2)

        response = self.client.get(reverse("finance:brokerage"), {"account": account.id})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_brokerage_account"], account)
        currency_totals = {item["currency"]: item for item in response.context["currency_totals"]}
        self.assertEqual(set(currency_totals), {"USD"})
        self.assertEqual(currency_totals["USD"]["value"], Decimal("96.00"))
        self.assertEqual(currency_totals["USD"]["planned_dividend_net"], Decimal("6.48"))
        self.assertEqual(len(response.context["positions"]), 1)
        self.assertEqual(len(response.context["upcoming_dividends"]), 1)
        self.assertEqual(len(response.context["brokerage_instruments"]), 1)
        self.assertEqual(
            response.context["brokerage_initial_portfolio_history"]["scope"]["account_id"],
            account.id,
        )
        self.assertContains(response, 'id="brokeragePortfolioAccountSelect"')
        self.assertContains(response, f'data-portfolio-account="{account.id}"')
        self.assertContains(response, f'Wartość portfela · {account.name}')
        self.assertContains(response, '<h1 class="u-page-title">Centrum inwestora</h1>', html=True)
        self.assertContains(response, 'aria-label="Wybierz konto maklerskie"')
        self.assertContains(response, 'aria-label="Filtruj otwarte pozycje"')
        self.assertContains(response, 'aria-labelledby="brokeragePortfolioChartTitle"')
        self.assertContains(response, 'id="brokeragePortfolioValueChartEmpty" role="status"')
        self.assertContains(response, 'id="brokeragePortfolioValueWarning" role="alert"')

    def test_brokerage_transaction_form_filters_accounts_by_user(self):
        other_user = User.objects.create_user(username="u2", password="pass123")
        other_account = BrokerageAccount.objects.create(
            user=other_user,
            name="mBank",
            broker=BrokerageAccount.BROKER_MBANK,
            account_type=BrokerageAccount.STANDARD,
            currency="PLN",
        )
        account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB EUR",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="EUR",
        )

        response = self.client.get(reverse("finance:add_brokerage_transaction"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, account.name)
        self.assertContains(response, "ISIN")
        self.assertNotContains(response, "Giełda")
        self.assertNotContains(response, other_account.name)

    def test_brokerage_recent_transactions_show_current_month_only(self):
        account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB PLN",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="PLN",
        )
        instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker="KRU",
            name="Kruk",
            isin="PLKRK0000010",
            asset_type=BrokerageInstrument.STOCK,
            currency="PLN",
        )
        current_month_date = timezone.localdate().replace(day=1)
        previous_month_date = (current_month_date - timedelta(days=1)).replace(day=1)
        current_transaction = BrokerageTransaction.objects.create(
            account=account,
            instrument=instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=current_month_date,
            quantity="1",
            price="400.00",
        )
        BrokerageTransaction.objects.create(
            account=account,
            instrument=instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=previous_month_date,
            quantity="1",
            price="390.00",
        )

        response = self.client.get(reverse("finance:brokerage"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["brokerage_transactions"]), [current_transaction])

    def test_import_brokerage_transactions_from_xtb_xlsx_creates_transaction(self):
        account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB PLN",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="PLN",
        )

        response = self.client.post(
            reverse("finance:import_brokerage_transactions"),
            {"account": str(account.id), "file": _xtb_upload()},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        transaction = BrokerageTransaction.objects.get()
        instrument = BrokerageInstrument.objects.get()
        self.assertEqual(instrument.ticker, "KRU")
        self.assertEqual(instrument.price_symbol, "KRU.PL")
        self.assertEqual(instrument.exchange, "XWAR")
        self.assertEqual(transaction.account, account)
        self.assertEqual(transaction.transaction_type, BrokerageTransaction.BUY)
        self.assertEqual(transaction.trade_date, date(2026, 1, 2))
        self.assertEqual(transaction.trade_time.strftime("%H:%M:%S"), "10:30:00")
        self.assertEqual(transaction.quantity, Decimal("3.000000"))
        self.assertEqual(transaction.price, Decimal("100.0000"))
        self.assertEqual(transaction.fees, Decimal("5.00"))
        self.assertEqual(transaction.import_source, "xtb")
        self.assertEqual(transaction.external_id, f"xtb:{account.id}:position:123456:open")

    def test_import_brokerage_transactions_from_xtb_xlsx_skips_duplicates(self):
        account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB PLN",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="PLN",
        )

        self.client.post(
            reverse("finance:import_brokerage_transactions"),
            {"account": str(account.id), "file": _xtb_upload()},
            follow=True,
        )
        self.client.post(
            reverse("finance:import_brokerage_transactions"),
            {"account": str(account.id), "file": _xtb_upload()},
            follow=True,
        )

        self.assertEqual(BrokerageTransaction.objects.count(), 1)

    @patch("finance.views.timezone.localdate")
    @patch("finance.views.sync_price_history_for_user")
    def test_brokerage_instrument_detail_data_returns_history_for_chart(self, mock_sync_history, mock_localdate):
        mock_localdate.return_value = date(2026, 5, 17)
        mock_sync_history.return_value = {
            "instruments_synced": 0,
            "points_created": 0,
            "points_updated": 0,
            "already_current": 1,
            "failed": [],
        }
        account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB PLN",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="PLN",
        )
        instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker="KRU",
            name="Kruk",
            asset_type=BrokerageInstrument.STOCK,
            currency="PLN",
            exchange="XWAR",
            last_price="110.00",
            market_data_source="Stooq",
        )
        BrokerageTransaction.objects.create(
            account=account,
            instrument=instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 5, 1),
            quantity="3",
            price="100.00",
        )
        BrokerageTransaction.objects.create(
            account=account,
            instrument=instrument,
            transaction_type=BrokerageTransaction.SELL,
            trade_date=date(2026, 5, 2),
            quantity="1",
            price="108.00",
        )
        BrokerageDailyPrice.objects.create(
            instrument=instrument,
            trading_date=date(2026, 5, 1),
            open="100.00",
            high="105.00",
            low="99.00",
            close="104.00",
            adjusted_close="104.00",
            volume=1000,
            currency="PLN",
            provider_symbol="KRU.WA",
            source="Stooq",
        )
        BrokerageDailyPrice.objects.create(
            instrument=instrument,
            trading_date=date(2026, 5, 2),
            open="104.00",
            high="112.00",
            low="103.00",
            close="110.00",
            adjusted_close="110.00",
            volume=1200,
            currency="PLN",
            provider_symbol="KRU.WA",
            source="Stooq",
        )

        response = self.client.get(reverse("finance:brokerage_instrument_detail_data", args=[instrument.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["instrument"]["ticker"], "KRU")
        self.assertEqual(payload["history_source"], "Stooq · zapis lokalny")
        self.assertEqual(len(payload["history"]), 2)
        self.assertEqual(Decimal(payload["history"][-1]["close"]), Decimal("110.00"))
        self.assertEqual(Decimal(payload["history_summary"]["change"]), Decimal("6.00"))
        self.assertEqual(payload["accounts"][0]["quantity"], "2.000000")
        self.assertEqual(len(payload["chart_transactions"]), 2)
        self.assertEqual(payload["chart_transactions"][0]["type"], "buy")
        self.assertEqual(payload["chart_transactions"][0]["price"], "100.0000")
        self.assertEqual(payload["chart_transactions"][1]["type"], "sell")
        self.assertEqual(payload["chart_transactions"][1]["price"], "108.0000")
        self.assertEqual(payload["history_range"]["mode"], "since_purchase")
        self.assertEqual(payload["first_purchase"]["date"], "2026-05-01")
        self.assertEqual(payload["market_statistics"]["sessions"], 2)
        self.assertEqual(len(payload["actual_profit"]), 1)
        mock_sync_history.assert_called_once_with(self.user, instrument_ids=[instrument.id])

    def test_brokerage_instrument_detail_data_is_limited_to_owner(self):
        other_user = User.objects.create_user(username="u2", password="pass123")
        instrument = BrokerageInstrument.objects.create(
            user=other_user,
            ticker="AAPL",
            name="Apple",
            asset_type=BrokerageInstrument.STOCK,
            currency="USD",
        )

        response = self.client.get(reverse("finance:brokerage_instrument_detail_data", args=[instrument.id]))

        self.assertEqual(response.status_code, 404)

    @patch("finance.views.sync_price_history_for_user")
    def test_brokerage_instrument_detail_data_accepts_custom_history_range(self, mock_sync_history):
        mock_sync_history.return_value = {
            "instruments_synced": 0,
            "points_created": 0,
            "points_updated": 0,
            "already_current": 1,
            "failed": [],
        }
        account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB USD",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="USD",
        )
        instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker="AAPL",
            name="Apple",
            asset_type=BrokerageInstrument.STOCK,
            currency="USD",
            exchange="XNAS",
        )
        BrokerageTransaction.objects.create(
            account=account,
            instrument=instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 15),
            quantity="5",
            price="190.00",
        )
        BrokerageTransaction.objects.create(
            account=account,
            instrument=instrument,
            transaction_type=BrokerageTransaction.SELL,
            trade_date=date(2026, 2, 2),
            quantity="1",
            price="200.00",
        )
        BrokerageDailyPrice.objects.create(
            instrument=instrument,
            trading_date=date(2026, 1, 15),
            open="190.00",
            high="195.00",
            low="188.00",
            close="194.50",
            adjusted_close="194.50",
            volume=12345,
            currency="USD",
            provider_symbol="AAPL",
            source="Test",
        )

        response = self.client.get(
            reverse("finance:brokerage_instrument_detail_data", args=[instrument.id]),
            {"start_date": "2026-01-01", "end_date": "2026-01-31"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["history_range"]["mode"], "custom")
        self.assertEqual(payload["history_range"]["start_date"], "2026-01-01")
        self.assertEqual(payload["history_range"]["end_date"], "2026-01-31")
        self.assertEqual(len(payload["chart_transactions"]), 1)
        self.assertEqual(payload["chart_transactions"][0]["date"], "2026-01-15")
        self.assertEqual(payload["chart_transactions"][0]["type"], "buy")
        mock_sync_history.assert_called_once_with(self.user, instrument_ids=[instrument.id])

    def test_brokerage_instrument_detail_data_rejects_invalid_history_range(self):
        instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker="AAPL",
            name="Apple",
            asset_type=BrokerageInstrument.STOCK,
            currency="USD",
            exchange="XNAS",
        )

        response = self.client.get(
            reverse("finance:brokerage_instrument_detail_data", args=[instrument.id]),
            {"start_date": "2026-02-01", "end_date": "2026-01-01"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Data początku", response.json()["error"])

    @patch("finance.portfolio_history.fetch_latest_fx_rates_to_pln")
    def test_brokerage_portfolio_history_all_accounts_converts_to_pln(self, mock_fx_rates):
        mock_fx_rates.return_value = {
            "rates": {"PLN": Decimal("1"), "EUR": Decimal("4.5")},
            "source": "NBP tabela testowa",
            "table_date": "2026-01-02",
        }
        pln_account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB PLN",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="PLN",
        )
        eur_account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB EUR",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="EUR",
        )
        pln_instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker="KRU",
            name="Kruk",
            asset_type=BrokerageInstrument.STOCK,
            currency="PLN",
            exchange="XWAR",
            last_price="110.00",
        )
        eur_instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker="SAP",
            name="SAP",
            asset_type=BrokerageInstrument.STOCK,
            currency="EUR",
            exchange="XETR",
            last_price="12.00",
        )
        BrokerageTransaction.objects.create(
            account=pln_account,
            instrument=pln_instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 1),
            quantity="1",
            price="100.00",
        )
        BrokerageTransaction.objects.create(
            account=eur_account,
            instrument=eur_instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 1),
            quantity="2",
            price="10.00",
            fx_rate_to_pln="4.500000",
        )

        response = self.client.get(
            reverse("finance:brokerage_value_history_data"),
            {"start_date": "2026-01-01", "end_date": "2026-01-02"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["currency"], "PLN")
        self.assertEqual(payload["points"][0]["value"], "190.00")
        self.assertEqual(payload["points"][1]["value"], "218.00")
        self.assertEqual(payload["summary"]["latest_value"], "218.00")
        self.assertEqual(payload["fx_source"], "NBP tabela testowa")
        self.assertEqual(payload["price_sources"], ["Dane lokalne"])
        self.assertEqual(payload["warnings"], [])

    def test_brokerage_portfolio_history_selected_account_keeps_account_currency(self):
        pln_account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB PLN",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="PLN",
        )
        eur_account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB EUR",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="EUR",
        )
        pln_instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker="KRU",
            name="Kruk",
            asset_type=BrokerageInstrument.STOCK,
            currency="PLN",
            exchange="XWAR",
        )
        eur_instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker="SAP",
            name="SAP",
            asset_type=BrokerageInstrument.STOCK,
            currency="EUR",
            exchange="XETR",
            last_price="12.00",
        )
        BrokerageTransaction.objects.create(
            account=pln_account,
            instrument=pln_instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 1),
            quantity="1",
            price="100.00",
        )
        BrokerageTransaction.objects.create(
            account=eur_account,
            instrument=eur_instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 1),
            quantity="2",
            price="10.00",
        )

        response = self.client.get(
            reverse("finance:brokerage_value_history_data"),
            {"account": eur_account.id, "start_date": "2026-01-01", "end_date": "2026-01-02"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["currency"], "EUR")
        self.assertEqual(payload["points"][0]["value"], "20.00")
        self.assertEqual(payload["points"][1]["value"], "24.00")
        self.assertEqual(payload["scope"]["account_name"], "XTB EUR")
        self.assertEqual(payload["fx_source"], "")
        self.assertEqual(payload["price_sources"], ["Dane lokalne"])
        self.assertEqual(payload["warnings"], [])

    def test_brokerage_portfolio_history_selected_account_isolated_from_cached_all_scope(self):
        first_account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB PLN pierwsze",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="PLN",
        )
        second_account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB PLN drugie",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="PLN",
        )
        instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker="SHARED",
            name="Wspólny instrument",
            asset_type=BrokerageInstrument.STOCK,
            currency="PLN",
            last_price="20.00",
        )
        BrokerageTransaction.objects.create(
            account=first_account,
            instrument=instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 1),
            quantity="1",
            price="10.00",
        )
        BrokerageTransaction.objects.create(
            account=second_account,
            instrument=instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 1),
            quantity="3",
            price="10.00",
        )
        url = reverse("finance:brokerage_value_history_data")
        history_range = {"start_date": "2026-01-01", "end_date": "2026-01-02"}

        all_payload = self.client.get(url, history_range).json()
        selected_payload = self.client.get(
            url,
            {**history_range, "account": first_account.id},
        ).json()

        self.assertEqual(all_payload["points"][-1]["value"], "80.00")
        self.assertTrue(all_payload["scope"]["is_all"])
        self.assertEqual(selected_payload["points"][-1]["value"], "20.00")
        self.assertEqual(selected_payload["scope"]["account_id"], first_account.id)
        self.assertEqual(selected_payload["scope"]["account_name"], first_account.name)

    @patch("finance.portfolio_history.fetch_latest_fx_rates_to_pln")
    def test_brokerage_portfolio_history_uses_nbp_instead_of_default_foreign_fx(self, mock_fx_rates):
        mock_fx_rates.return_value = {
            "rates": {"PLN": Decimal("1"), "EUR": Decimal("4.2")},
            "source": "NBP tabela testowa",
            "table_date": "2026-01-02",
        }
        pln_account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB PLN history",
            broker=BrokerageAccount.BROKER_XTB,
            currency="PLN",
        )
        eur_account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB EUR history",
            broker=BrokerageAccount.BROKER_XTB,
            currency="EUR",
        )
        pln_instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker="PLN-HISTORY",
            name="PLN history",
            currency="PLN",
            last_price="100.00",
        )
        eur_instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker="EUR-HISTORY",
            name="EUR history",
            currency="EUR",
            last_price="50.00",
        )
        BrokerageTransaction.objects.create(
            account=pln_account,
            instrument=pln_instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 1),
            quantity="1",
            price="100.00",
        )
        BrokerageTransaction.objects.create(
            account=eur_account,
            instrument=eur_instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 1, 1),
            quantity="1",
            price="50.00",
            fx_rate_to_pln="1.000000",
        )

        response = self.client.get(
            reverse("finance:brokerage_value_history_data"),
            {"start_date": "2026-01-01", "end_date": "2026-01-02"},
        )

        payload = response.json()
        self.assertEqual(payload["points"][-1]["value"], "310.00")
        self.assertEqual(payload["fx_source"], "NBP tabela testowa")
        self.assertEqual(payload["warnings"], [])

    def test_brokerage_transaction_creates_instrument_from_typed_data_and_manual_price(self):
        account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB PLN",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="PLN",
        )

        response = self.client.post(reverse("finance:add_brokerage_transaction"), {
            "account": str(account.id),
            "transaction_type": BrokerageTransaction.BUY,
            "instrument_name": "Kruk",
            "isin": "plkrk0000010",
            "asset_type": BrokerageInstrument.STOCK,
            "currency": "PLN",
            "trade_date": "2026-05-12",
            "trade_time": "10:30",
            "quantity": "3",
            "price": "412.50",
            "fees": "5.00",
            "fx_rate_to_pln": "1.000000",
            "notes": "",
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        instrument = BrokerageInstrument.objects.get(user=self.user, ticker="PLKRK0000010")
        transaction = BrokerageTransaction.objects.get(account=account, instrument=instrument)
        self.assertEqual(instrument.name, "Kruk")
        self.assertEqual(instrument.isin, "PLKRK0000010")
        self.assertEqual(transaction.trade_time, time(10, 30))
        self.assertEqual(transaction.price, Decimal("412.5000"))

    @patch("finance.forms.fetch_transaction_market_price")
    def test_brokerage_transaction_fetches_market_price_before_save(self, mock_fetch_price):
        mock_fetch_price.return_value = {
            "price": Decimal("410.2500"),
            "source": "Test market",
            "symbol": "KRU.WA",
            "isin": "PLKRK0000010",
            "exchange": "WSE",
            "currency": "PLN",
        }
        account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB PLN",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="PLN",
        )

        response = self.client.post(reverse("finance:add_brokerage_transaction"), {
            "account": str(account.id),
            "transaction_type": BrokerageTransaction.BUY,
            "instrument_name": "Kruk",
            "isin": "PLKRK0000010",
            "asset_type": BrokerageInstrument.STOCK,
            "currency": "PLN",
            "trade_date": "2026-05-12",
            "trade_time": "10:30",
            "quantity": "3",
            "price": "",
            "fees": "5.00",
            "fx_rate_to_pln": "1.000000",
            "notes": "",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "410.2500")
        self.assertFalse(BrokerageTransaction.objects.exists())

        response = self.client.post(reverse("finance:add_brokerage_transaction"), {
            "account": str(account.id),
            "transaction_type": BrokerageTransaction.BUY,
            "instrument_name": "Kruk",
            "isin": "PLKRK0000010",
            "asset_type": BrokerageInstrument.STOCK,
            "currency": "PLN",
            "trade_date": "2026-05-12",
            "trade_time": "10:30",
            "quantity": "3",
            "price": "411.00",
            "fees": "5.00",
            "fx_rate_to_pln": "1.000000",
            "notes": "",
            "market_price_confirmed": "on",
            "market_price_value": "410.2500",
            "market_price_source_value": "Test market",
            "market_symbol_value": "KRU.WA",
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        transaction = BrokerageTransaction.objects.get()
        self.assertEqual(transaction.instrument.ticker, "KRU.WA")
        self.assertEqual(transaction.instrument.isin, "PLKRK0000010")
        self.assertEqual(transaction.price, Decimal("411.0000"))
        self.assertEqual(transaction.market_price, Decimal("410.2500"))
        self.assertEqual(transaction.market_price_source, "Test market")

    def test_brokerage_transaction_can_be_edited_and_deleted(self):
        account = BrokerageAccount.objects.create(
            user=self.user,
            name="XTB PLN",
            broker=BrokerageAccount.BROKER_XTB,
            account_type=BrokerageAccount.STANDARD,
            currency="PLN",
        )
        instrument = BrokerageInstrument.objects.create(
            user=self.user,
            ticker="KRU",
            name="Kruk",
            isin="PLKRK0000010",
            asset_type=BrokerageInstrument.STOCK,
            currency="PLN",
        )
        transaction = BrokerageTransaction.objects.create(
            account=account,
            instrument=instrument,
            transaction_type=BrokerageTransaction.BUY,
            trade_date=date(2026, 4, 10),
            trade_time=time(12, 55),
            quantity="2",
            price="481.00",
            fees="0.00",
        )

        response = self.client.post(reverse("finance:edit_brokerage_transaction", args=[transaction.id]), {
            "account": str(account.id),
            "transaction_type": BrokerageTransaction.BUY,
            "instrument_name": "Kruk",
            "isin": "PLKRK0000010",
            "asset_type": BrokerageInstrument.STOCK,
            "currency": "PLN",
            "trade_date": "2026-04-10",
            "trade_time": "12:55",
            "quantity": "3",
            "price": "482.00",
            "fees": "1.00",
            "fx_rate_to_pln": "1.000000",
            "notes": "korekta",
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        transaction.refresh_from_db()
        self.assertEqual(transaction.quantity, Decimal("3.000000"))
        self.assertEqual(transaction.price, Decimal("482.0000"))
        self.assertEqual(transaction.notes, "korekta")

        response = self.client.post(reverse("finance:delete_brokerage_transaction", args=[transaction.id]), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(BrokerageTransaction.objects.filter(id=transaction.id).exists())

    def test_user_cannot_delete_other_users_brokerage_instrument(self):
        other_user = User.objects.create_user(username="broker2", password="pass123")
        instrument = BrokerageInstrument.objects.create(
            user=other_user,
            ticker="AAPL",
            name="Apple",
            isin="US0378331005",
            asset_type=BrokerageInstrument.STOCK,
            currency="USD",
        )

        response = self.client.post(reverse("finance:delete_brokerage_instrument", args=[instrument.id]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(BrokerageInstrument.objects.filter(id=instrument.id).exists())
