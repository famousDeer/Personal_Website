"""Import zestawienia operacji z mBanku (CSV, cp1250, preambuła i stopka).

Dane w testach są przykładowe - nie pochodzą z prawdziwego wyciągu.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from finance.bank_import import MBANK_SOURCE, SAVINGS_CATEGORY, BankImportError, parse_bank_csv
from finance.models import Daily, Income

from .test_bank_import import _post_data_from_preview

User = get_user_model()

MBANK_HEADER = '#Data księgowania;#Data operacji;#Opis operacji;#Tytuł;#Nadawca/Odbiorca;#Numer konta;#Kwota;#Saldo po operacji;'

MBANK_ROWS = [
    # Płatność kartą: zaksięgowana 2026-05-16, zrobiona 2026-05-15.
    '2026-05-16;2026-05-16;ZAKUP PRZY UŻYCIU KARTY;"BIEDRONKA PRZYKLAD  /Gdynia'
    '                                            DATA TRANSAKCJI: 2026-05-15";"  ";\'\';-67,31;1 932,69;',
    # Kwota z separatorem tysięcy.
    '2026-05-18;2026-05-18;ZAKUP PRZY UŻYCIU KARTY;"MEBLE TESTOWE SP.  /GDANSK'
    '                                            DATA TRANSAKCJI: 2026-05-17";"  ";\'\';-1 250,00;682,69;',
    # Zaokrąglenie zakupu odkładane na cel.
    '2026-05-18;2026-05-18;PRZELEW NA TWOJE CELE;"BIEDRONKA PRZYKLAD";"ZAKUP PRZY UŻYCIU KARTY  ";\'\';-2,69;680,00;',
    # Wpływ na konto.
    '2026-05-20;2026-05-20;PRZELEW PRZYCHODZĄCY;"Wynagrodzenie 05/2026";"FIRMA PRZYKLADOWA SA  ";'
    '\'11 1111 1111 1111 1111 1111 1111\';5 000,00;5 680,00;',
]


def _mbank_text(rows=None):
    lines = [
        'mBank S.A. Bankowość Detaliczna;',
        '\t\tSkrytka Pocztowa 0000;',
        '',
        '#Klient;',
        'JAN PRZYKŁADOWY;',
        '',
        'Elektroniczne zestawienie operacji;',
        '',
        MBANK_HEADER,
        *(MBANK_ROWS if rows is None else rows),
        '',
        '',
        ';;;;;;#Saldo końcowe;5 680,00 PLN;',
        '',
        'Niniejszy dokument sporządzono na podstawie art. 7 Ustawy Prawo Bankowe;',
    ]
    return '\r\n'.join(lines)


def _mbank_upload(rows=None, encoding='cp1250'):
    return SimpleUploadedFile('mbank.csv', _mbank_text(rows).encode(encoding), content_type='text/csv')


class MBankImportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='mbank-user', password='pass123')
        self.account = self.user.owned_finance_accounts.get(account_type='personal')

    def test_parser_detects_mbank_and_reads_cp1250_with_preamble_and_footer(self):
        preview = parse_bank_csv(_mbank_upload(), self.account)

        self.assertEqual(len(preview.candidates), 4)
        self.assertTrue(all(c.external_id.startswith('mbank:') for c in preview.candidates))
        self.assertEqual(len({c.external_id for c in preview.candidates}), 4)
        grocery, furniture, goal, salary = preview.candidates

        # Data płatności kartą z „DATA TRANSAKCJI”, nie z dnia księgowania.
        self.assertTrue(grocery.is_expense)
        self.assertEqual(grocery.date, date(2026, 5, 15))
        self.assertEqual(grocery.amount, Decimal('67.31'))
        self.assertEqual(grocery.store, 'Biedronka')
        self.assertEqual(grocery.category, 'Zakupy spozywcze')

        self.assertEqual(furniture.date, date(2026, 5, 17))
        self.assertEqual(furniture.amount, Decimal('1250.00'))

        self.assertTrue(salary.is_income)
        self.assertEqual(salary.amount, Decimal('5000.00'))

    def test_goal_roundups_are_savings_and_not_selected_by_default(self):
        preview = parse_bank_csv(_mbank_upload(), self.account)
        goal = preview.candidates[2]

        self.assertEqual(goal.category, SAVINGS_CATEGORY)
        self.assertEqual(goal.title, 'Twoje cele: Biedronka')
        self.assertEqual(goal.store, '')
        self.assertFalse(goal.selected_by_default)
        self.assertTrue(preview.candidates[0].selected_by_default)

    def test_utf8_export_is_also_accepted(self):
        preview = parse_bank_csv(_mbank_upload(encoding='utf-8'), self.account)
        self.assertEqual(len(preview.candidates), 4)

    def test_file_without_operations_raises_error(self):
        with self.assertRaises(BankImportError):
            parse_bank_csv(_mbank_upload(rows=[]), self.account)

    def test_confirmed_import_saves_rows_and_skips_reimport(self):
        self.client.login(username='mbank-user', password='pass123')
        url = reverse('finance:import_bank_transactions')

        preview_response = self.client.post(url, {'file': _mbank_upload()})
        self.assertEqual(preview_response.status_code, 200)
        preview = preview_response.context['preview']
        self.assertContains(preview_response, 'Twoje cele: Biedronka')

        post_data = _post_data_from_preview(preview)
        # Jak w formularzu: zaokrąglenie na cel zostaje odznaczone.
        post_data.pop(f'row_{preview.candidates[2].index}_selected')
        self.assertEqual(self.client.post(url, post_data).status_code, 302)

        self.assertEqual(Daily.objects.filter(account=self.account, import_source=MBANK_SOURCE).count(), 2)
        self.assertEqual(Income.objects.filter(account=self.account, import_source=MBANK_SOURCE).count(), 1)
        self.assertFalse(Daily.objects.filter(account=self.account, category=SAVINGS_CATEGORY).exists())

        reimport = parse_bank_csv(_mbank_upload(), self.account)
        duplicates = [c for c in reimport.candidates if c.duplicate_reason]
        self.assertEqual(len(duplicates), 3)

        self.client.post(url, _post_data_from_preview(preview))
        self.assertEqual(Daily.objects.filter(account=self.account, import_source=MBANK_SOURCE).count(), 3)
        self.assertEqual(Income.objects.filter(account=self.account, import_source=MBANK_SOURCE).count(), 1)
