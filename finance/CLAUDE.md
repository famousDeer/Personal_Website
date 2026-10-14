# finance – budżet, import wyciągów, maklerka, GEM, podróże

Modele (`models.py`): FinanceAccount (osobiste/wspólne, members), Monthly → Daily (wydatek) / Income, ImportRule (reguły importu per konto), Brokerage* (Account, Instrument, Transaction, Dividend, CashOperation, PositionSnapshot, PriceSnapshot, DailyPrice), InvestmentFunding (wpłata Inwestycje ↔ broker), StrategyDailyPrice (notowania GEM), TravelDestinations / TravelExpense.

Widoki: `views.py` (~2400 l., czytaj grepem) – Dashboard, Expense*/Income* CRUD, ImportBankTransactionsView, Reports, Brokerage* (portfel, historia, refresh, import XTB), GemRefreshView, Travel*, API DailyRecordAPI / MonthlyRecordAPI (DRF). `views_import_rules.py` – strona reguł importu.

Logika poza widokami:
- `account_utils.py` – aktywne konto (`get_active_finance_account`), przeliczanie Monthly, `TRANSFER_TO_SHARED_CATEGORY` (przelew na wspólne tworzy powiązany Income na koncie wspólnym). `signals.py` zakłada konto osobiste nowemu użytkownikowi.
- `bank_import.py` – parser CSV Millennium/ING, duplikaty, podpowiedzi kategorii. `import_rules.py` – reguły domownika. `text_utils.py` – `normalize_text`.
- `brokerage.py` – `build_portfolio_summary` (FIFO, podatek Belki, IKE zwolnione). `brokerage_import.py` – XLSX z XTB. `portfolio_history.py`, `investment_statistics.py`, `investment_funding.py`.
- `market_data.py` – Yahoo (główne), Stooq, Alpha Vantage, OpenFIGI, NBP. `sync_price_history_for_user`, `refresh_market_data_for_user`.
- `household.py` + `views_household.py` – analiza domowa (`/finance/analysis/`, szczegóły miesiąca `/finance/analysis/<rok>/<mies>/`: pozycje tylko z moich i wspólnych kont): moje konto + wspólne + konta osobiste domowników z kont wspólnych, tylko gdy właściciel włączył `FinanceAccount.share_in_household_analysis`; z cudzych kont tylko sumy. Rodzaje kategorii (niezbędne/pozostałe/oszczędności/pomijane) w `CategoryKind`, minimum na życie = średnia niezbędnych z pełnych miesięcy.
- `gem.py` – strategia GEM (CSPX/IWDA/EIMI/IB01, 12M, sygnał z końca miesiąca).
- `geocoding.py` – Nominatim dla podróży.

Komendy: `refresh_market_data`, `sync_price_history` (+ GEM, `--bez-gem`).
Testy: `finance/tests/test_*.py` (np. `test_bank_import*.py`, `test_gem.py`, `test_import_rules.py`).
Kategorie wydatków i źródła przychodów są wspólne dla domu: `get_available_expense_categories` i `get_available_income_sources` w `views.py` (wbudowane + użyte na wszystkich kontach).
Zależności: `utils` (undo, navigation, tools), `accounts.templatetags.ui` (`zl`). Kontekst `finance.context_processors.finance_accounts` dla wszystkich stron. Kategoria „Wpłata do wspólnego z mBank” jest wyłączona w imporcie.
