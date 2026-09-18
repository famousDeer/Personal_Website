"""Zabezpieczenie przed powrotem zapytań N+1 w imporcie bankowym.

Dopasowywanie do historii i wykrywanie podobnych pozycji robiły po jednym
zapytaniu na wiersz wyciągu. Te testy pilnują, żeby liczba zapytań nie rosła
razem z liczbą wierszy w pliku.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from finance.account_utils import ensure_personal_finance_account
from finance.bank_import import BankHistoryIndex, _parse_millennium_csv_text
from finance.models import Daily, Income, Monthly

User = get_user_model()

STORES = [
    'Biedronka', 'Lidl', 'Żabka', 'Rossmann', 'Orlen', 'IKEA', 'Allegro',
    'Netflix', 'Spotify', 'Empik', 'Auchan', 'Shell', 'Apteka', 'Douglas',
]
CSV_HEADER = (
    'Data transakcji;Rodzaj transakcji;Odbiorca/Zleceniodawca;Opis;'
    'Obciążenia;Uznania;Waluta;Saldo\n'
)


def build_statement(row_count):
    rows = []
    for index in range(row_count):
        store = STORES[index % len(STORES)]
        rows.append(
            f'2026-02-{(index % 28) + 1:02d};Zakup kartą;{store};'
            f'PŁATNOŚĆ KARTĄ {store};-{(index % 50) + 5}.00;;PLN;1000.00\n'
        )
    return CSV_HEADER + ''.join(rows)


class BankImportQueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('importer', password='x')
        cls.account = ensure_personal_finance_account(cls.user)
        cls.month = Monthly.objects.create(user=cls.user, account=cls.account, date=date(2026, 1, 1))
        for index in range(300):
            Daily.objects.create(
                user=cls.user, account=cls.account, month=cls.month,
                date=date(2026, 1, 1) + timedelta(days=index % 28),
                title=f'Zakup {index}', cost=Decimal('10.00'),
                store=STORES[index % len(STORES)], category='Zakupy spozywcze',
            )
        for index in range(100):
            Income.objects.create(
                user=cls.user, account=cls.account, month=cls.month,
                date=date(2026, 1, 1) + timedelta(days=index % 28),
                title=f'Pensja {index}', amount=Decimal('100.00'), source='Pensja',
            )

    def test_query_count_does_not_grow_with_row_count(self):
        with self.assertNumQueries(6):
            small = _parse_millennium_csv_text(build_statement(10), self.account)
        with self.assertNumQueries(6):
            large = _parse_millennium_csv_text(build_statement(200), self.account)

        self.assertEqual(len(small.candidates), 10)
        self.assertEqual(len(large.candidates), 200)

    def test_history_index_deduplicates_repeated_keys(self):
        index = BankHistoryIndex(self.account)

        # 300 rekordów historii, ale tylko 14 różnych sklepów - przy dopasowaniu
        # "pierwszy wygrywa" powtórzone klucze nigdy nie mogłyby zostać zwrócone.
        self.assertEqual(len(index.expenses), len(STORES))
        self.assertEqual(len(index.incomes), 100)

    def test_history_matching_still_assigns_categories(self):
        preview = _parse_millennium_csv_text(build_statement(20), self.account)

        matched = [
            candidate for candidate in preview.candidates
            if candidate.suggestion_reason.startswith('historia')
        ]
        self.assertEqual(len(matched), 20)
        self.assertTrue(all(candidate.category == 'Zakupy spozywcze' for candidate in matched))

    def test_index_is_empty_without_account(self):
        index = BankHistoryIndex(None)

        self.assertEqual(index.expenses, [])
        self.assertEqual(index.incomes, [])
        self.assertIsNone(index.match_expense('Biedronka', {}))
        self.assertIsNone(index.match_income({}))
