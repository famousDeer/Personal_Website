from unittest.mock import patch
from urllib.error import HTTPError

from django.test import TestCase, override_settings
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.utils import timezone
from decimal import Decimal
from finance.models import (
    BrokerageAccount,
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
        self.assertIn("https://stooq.pl/q/?s=KRUK+S.A.", str(error.exception))
        self.assertIn("Symbol ceny", str(error.exception))

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
        mock_request_json.side_effect = [
            [{
                "data": [{
                    "ticker": "UBI",
                    "name": "UBISOFT ENTERTAINMENT",
                    "marketSector": "Equity",
                    "securityType2": "Common Stock",
                    "exchCode": "FP",
                    "micCode": "XPAR",
                }]
            }],
            {"Global Quote": {"05. price": "11.4200"}},
            {"data": []},
        ]

        result = refresh_market_data_for_user(user)

        instrument.refresh_from_db()
        self.assertEqual(result["updated_quotes"], 1)
        self.assertEqual(instrument.ticker, "UBI")
        self.assertEqual(instrument.price_symbol, "UBI.FR")
        self.assertEqual(instrument.last_price, Decimal("11.4200"))

    @override_settings(OPENFIGI_API_KEY="openfigi-key", ALPHA_VANTAGE_API_KEY="alpha-key")
    @patch("finance.market_data._read_csv_url")
    @patch("finance.market_data._request_json")
    def test_manual_price_symbol_falls_back_to_stooq_when_alpha_vantage_has_no_quote(self, mock_request_json, mock_read_csv):
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
        mock_request_json.side_effect = [
            [{
                "data": [{
                    "ticker": "UBI",
                    "name": "UBISOFT ENTERTAINMENT",
                    "marketSector": "Equity",
                    "securityType2": "Common Stock",
                    "exchCode": "FP",
                    "micCode": "XPAR",
                }]
            }],
            {"Global Quote": {}},
            {"data": []},
        ]
        mock_read_csv.return_value = [{"Symbol": "UBI.FR", "Close": "11.4200"}]

        result = refresh_market_data_for_user(user)

        instrument.refresh_from_db()
        self.assertEqual(result["updated_quotes"], 1)
        self.assertEqual(result["failed_quotes"], [])
        self.assertEqual(instrument.last_price, Decimal("11.4200"))
        self.assertEqual(instrument.market_data_source, "Stooq")
        self.assertIn("ubi.fr", mock_read_csv.call_args[0][0])

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
        BrokerageDividend.objects.create(
            account=account,
            instrument=instrument,
            ex_dividend_date=date(2026, 6, 1),
            payment_date=date(2026, 6, 15),
            gross_amount_per_share="1.00",
            currency="USD",
        )
        BrokerageDividend.objects.create(
            account=ike_account,
            instrument=polish_instrument,
            ex_dividend_date=date(2026, 6, 1),
            payment_date=date(2026, 6, 15),
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

    @override_settings(STOOQ_API_KEY="stooq-key")
    @patch("finance.views.timezone.localdate")
    @patch("finance.market_data._read_csv_url")
    def test_brokerage_instrument_detail_data_returns_history_for_chart(self, mock_read_csv, mock_localdate):
        mock_localdate.return_value = date(2026, 5, 17)
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
        mock_read_csv.return_value = [
            {"Date": "2026-05-01", "Open": "100.00", "High": "105.00", "Low": "99.00", "Close": "104.00", "Volume": "1000"},
            {"Date": "2026-05-02", "Open": "104.00", "High": "112.00", "Low": "103.00", "Close": "110.00", "Volume": "1200"},
        ]

        response = self.client.get(reverse("finance:brokerage_instrument_detail_data", args=[instrument.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["instrument"]["ticker"], "KRU")
        self.assertEqual(payload["history_source"], "Stooq")
        self.assertEqual(len(payload["history"]), 2)
        self.assertEqual(payload["history"][-1]["close"], "110.00")
        self.assertEqual(payload["history_summary"]["change"], "6.00")
        self.assertEqual(payload["accounts"][0]["quantity"], "2.000000")
        self.assertEqual(len(payload["chart_transactions"]), 2)
        self.assertEqual(payload["chart_transactions"][0]["type"], "buy")
        self.assertEqual(payload["chart_transactions"][0]["price"], "100.0000")
        self.assertEqual(payload["chart_transactions"][1]["type"], "sell")
        self.assertEqual(payload["chart_transactions"][1]["price"], "108.0000")
        self.assertIn("kru.pl", mock_read_csv.call_args[0][0])

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

    @patch("finance.views.fetch_historical_market_prices")
    def test_brokerage_instrument_detail_data_accepts_custom_history_range(self, mock_fetch_history):
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
        mock_fetch_history.return_value = {
            "points": [{
                "date": date(2026, 1, 15),
                "open": Decimal("190.00"),
                "high": Decimal("195.00"),
                "low": Decimal("188.00"),
                "close": Decimal("194.50"),
                "volume": 12345,
            }],
            "source": "Test",
            "resolution_error": "",
        }

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
        mock_fetch_history.assert_called_once()
        self.assertEqual(mock_fetch_history.call_args.kwargs["start_date"], date(2026, 1, 1))
        self.assertEqual(mock_fetch_history.call_args.kwargs["end_date"], date(2026, 1, 31))

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
    @patch("finance.portfolio_history.fetch_historical_market_prices")
    def test_brokerage_portfolio_history_all_accounts_converts_to_pln(self, mock_fetch_history, mock_fetch_fx):
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

        def history_for_symbol(**kwargs):
            if kwargs["symbol"] == "KRU":
                return {
                    "points": [
                        {"date": date(2026, 1, 1), "close": Decimal("100.00")},
                        {"date": date(2026, 1, 2), "close": Decimal("110.00")},
                    ],
                    "source": "Stooq",
                }
            return {
                "points": [
                    {"date": date(2026, 1, 1), "close": Decimal("10.00")},
                    {"date": date(2026, 1, 2), "close": Decimal("12.00")},
                ],
                "source": "Alpha Vantage",
            }

        mock_fetch_history.side_effect = history_for_symbol
        mock_fetch_fx.return_value = {
            "rates": {"PLN": Decimal("1"), "EUR": Decimal("4.50")},
            "source": "NBP tabela A",
            "table_date": "2026-01-02",
        }

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
        self.assertEqual(payload["fx_source"], "NBP tabela A")

    @patch("finance.portfolio_history.fetch_latest_fx_rates_to_pln")
    @patch("finance.portfolio_history.fetch_historical_market_prices")
    def test_brokerage_portfolio_history_selected_account_keeps_account_currency(self, mock_fetch_history, mock_fetch_fx):
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
        mock_fetch_history.return_value = {
            "points": [
                {"date": date(2026, 1, 1), "close": Decimal("10.00")},
                {"date": date(2026, 1, 2), "close": Decimal("12.00")},
            ],
            "source": "Alpha Vantage",
        }

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
        mock_fetch_fx.assert_not_called()
        mock_fetch_history.assert_called_once()
        self.assertEqual(mock_fetch_history.call_args.kwargs["symbol"], "SAP")

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
