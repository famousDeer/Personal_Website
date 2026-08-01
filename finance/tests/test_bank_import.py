import csv
from datetime import date
from io import StringIO
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from finance.bank_import import parse_bank_csv, parse_millennium_csv
from finance.models import Daily, Income, Monthly


User = get_user_model()


HEADERS = [
    'Numer rachunku/karty',
    'Data transakcji',
    'Data rozliczenia',
    'Rodzaj transakcji',
    'Na konto/Z konta',
    'Odbiorca/Zleceniodawca',
    'Opis',
    'Obciążenia',
    'Uznania',
    'Saldo',
    'Waluta',
]


def _millennium_upload(rows):
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADERS)
    writer.writerows(rows)
    return SimpleUploadedFile(
        'historia.csv',
        buffer.getvalue().encode('utf-8-sig'),
        content_type='text/csv',
    )


ING_HEADERS = [
    'Data transakcji',
    'Data księgowania',
    'Dane kontrahenta',
    'Tytuł',
    'Nr rachunku',
    'Nazwa banku',
    'Szczegóły',
    'Nr transakcji',
    'Kwota transakcji (waluta rachunku)',
    'Waluta',
    'Kwota blokady/zwolnienie blokady',
    'Waluta',
    'Kwota płatności w walucie',
    'Waluta',
    'Konto',
    'Saldo po transakcji',
    'Waluta',
]


def _ing_upload():
    content = '\n'.join([
        '"Lista transakcji";;;;;"ING Bank Śląski S.A."',
        '"Dokument nr 0243670896_010826";',
        '',
        ';'.join(f'"{header}"' for header in ING_HEADERS),
        '2026-07-31;2026-07-31;" MOBILE-TRAFFIC-DATA SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ" DRUŻBICKIEGO 11";"Bilet parkingowy";;;"TR.KART";\'202621297223654546\';-2,38;PLN;;;;;"KONTO Mobi";257,63;PLN',
        '2026-07-09;2026-07-09;" RANDSTAD POLSKA SP. Z O.O. ";"Wynagrodzenie 06.2026";;;"PRZELEW";\'202619064001317871\';3402,70;PLN;;;;;"KONTO Mobi";1234,56;PLN',
        '2026-07-09;2026-07-09;" GNYP ZUZANNA MARIA ";"Przelew własny";;;"PRZELEW";\'202619097201217785\';1200,00;PLN;;;;;"KONTO Mobi";1234,56;PLN',
        '2026-07-09;2026-07-09;" GNYP ZUZANNA MARIA ";"Przelew własny";;;"PRZELEW";\'202619097201217785\';-1200,00;PLN;;;;;"Otwarte Konto Oszczędnościowe";1234,56;PLN',
    ])
    return SimpleUploadedFile(
        'historia-ing.csv',
        content.encode('cp1250'),
        content_type='text/csv',
    )


def _expense_row():
    return [
        'PL26 1160 2202 0000 0003 1125 8495',
        '2026-07-28',
        '2026-07-28',
        'ZAKUP - FIZ. UŻYCIE KARTY',
        '',
        '',
        'LIDL NIEPODLEGLOSCI 01  Sopot POL 2026-07-25',
        '-7.49',
        '',
        '10631.12',
        'PLN',
    ]


def _income_row():
    return [
        'PL26 1160 2202 0000 0003 1125 8495',
        '2026-07-27',
        '2026-07-27',
        'PRZELEW WEWNĘTRZNY PRZYCHODZĄCY',
        '50 11 6022 0200 0000 0206 3698 29',
        'TRZCIŃSKA MAŁGORZATA UL NOWA 41 84-240 REDA',
        'Netflix',
        '',
        '45.00',
        '10638.61',
        'PLN',
    ]


def _post_data_from_preview(preview):
    data = {
        'confirm_import': '1',
        'row_count': str(len(preview.candidates)),
    }
    for candidate in preview.candidates:
        prefix = f'row_{candidate.index}'
        data[f'{prefix}_selected'] = 'on'
        data[f'{prefix}_kind'] = candidate.kind
        data[f'{prefix}_external_id'] = candidate.external_id
        data[f'{prefix}_date'] = candidate.date_display
        data[f'{prefix}_amount'] = candidate.amount_display
        data[f'{prefix}_title'] = candidate.title
        data[f'{prefix}_category'] = candidate.category
        data[f'{prefix}_source'] = candidate.source
        data[f'{prefix}_store'] = candidate.store
        data[f'{prefix}_counterparty'] = candidate.counterparty
    return data


class MillenniumBankImportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='bank-user', password='pass123')
        self.account = self.user.owned_finance_accounts.get(account_type='personal')

    def test_parser_reads_millennium_csv_and_uses_existing_store_category(self):
        month = Monthly.objects.create(
            user=self.user,
            account=self.account,
            date=date(2026, 7, 1),
            total_income=Decimal('0.00'),
            total_expense=Decimal('0.00'),
        )
        Daily.objects.create(
            user=self.user,
            account=self.account,
            date=date(2026, 7, 1),
            title='Zakupy spożywcze',
            category='Zakupy spozywcze',
            store='Lidl',
            cost=Decimal('12.34'),
            month=month,
        )

        preview = parse_millennium_csv(_millennium_upload([_expense_row(), _income_row()]), self.account)

        self.assertEqual(len(preview.candidates), 2)
        expense = preview.candidates[0]
        income = preview.candidates[1]
        self.assertTrue(expense.is_expense)
        self.assertEqual(expense.store, 'Lidl')
        self.assertEqual(expense.category, 'Zakupy spozywcze')
        self.assertIn('historia', expense.suggestion_reason)
        self.assertTrue(income.is_income)
        self.assertEqual(income.source, 'Rodzina')
        self.assertEqual(income.amount, Decimal('45.00'))

    def test_confirmed_import_creates_expenses_incomes_and_skips_reimport(self):
        self.client.login(username='bank-user', password='pass123')
        upload = _millennium_upload([_expense_row(), _income_row()])

        preview_response = self.client.post(reverse('finance:import_bank_transactions'), {'file': upload})

        self.assertEqual(preview_response.status_code, 200)
        self.assertEqual(len(preview_response.context['preview'].candidates), 2)

        preview = parse_millennium_csv(_millennium_upload([_expense_row(), _income_row()]), self.account)
        import_response = self.client.post(
            reverse('finance:import_bank_transactions'),
            _post_data_from_preview(preview),
        )

        self.assertEqual(import_response.status_code, 302)
        self.assertEqual(import_response.url, reverse('finance:dashboard'))
        self.assertEqual(Daily.objects.filter(account=self.account, import_source='millennium').count(), 1)
        self.assertEqual(Income.objects.filter(account=self.account, import_source='millennium').count(), 1)
        self.assertEqual(
            Income.objects.get(account=self.account, import_source='millennium').counterparty,
            'TRZCIŃSKA MAŁGORZATA UL NOWA 41 84-240 REDA',
        )
        monthly = Monthly.objects.get(account=self.account, date=date(2026, 7, 1))
        self.assertEqual(monthly.total_expense, Decimal('7.49'))
        self.assertEqual(monthly.total_income, Decimal('45.00'))

        duplicate_response = self.client.post(
            reverse('finance:import_bank_transactions'),
            _post_data_from_preview(preview),
        )

        self.assertEqual(duplicate_response.status_code, 302)
        self.assertEqual(Daily.objects.filter(account=self.account, import_source='millennium').count(), 1)
        self.assertEqual(Income.objects.filter(account=self.account, import_source='millennium').count(), 1)

    def test_preview_displays_a_source_suggested_from_history(self):
        month = Monthly.objects.create(
            user=self.user,
            account=self.account,
            date=date(2026, 7, 1),
            total_income=Decimal('0.00'),
            total_expense=Decimal('0.00'),
        )
        Income.objects.create(
            user=self.user,
            account=self.account,
            date=date(2026, 7, 1),
            title='Netflix',
            amount=Decimal('45.00'),
            source='Przelew własny',
            month=month,
        )

        self.client.login(username='bank-user', password='pass123')
        response = self.client.post(
            reverse('finance:import_bank_transactions'),
            {'file': _millennium_upload([_expense_row(), _income_row()])},
        )

        self.assertContains(response, 'name="row_0_category"')
        self.assertContains(response, 'value="Zakupy spozywcze"')
        self.assertContains(response, 'name="row_1_source"')
        self.assertContains(response, 'value="Przelew własny"')

    def test_confirmed_import_accepts_a_preview_larger_than_django_default_field_limit(self):
        self.client.login(username='bank-user', password='pass123')
        data = {
            'confirm_import': '1',
            'row_count': '0',
        }
        data.update({f'preview_field_{index}': 'value' for index in range(1100)})

        response = self.client.post(
            reverse('finance:import_bank_transactions'),
            urlencode(data),
            content_type='application/x-www-form-urlencoded',
        )

        self.assertEqual(response.status_code, 302)


class INGBankImportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='ing-user', password='pass123')
        self.account = self.user.owned_finance_accounts.get(account_type='personal')

    def test_parser_reads_ing_csv_with_preamble_and_distinguishes_internal_transfers(self):
        preview = parse_bank_csv(_ing_upload(), self.account)

        self.assertEqual(len(preview.candidates), 4)
        expense, salary, incoming_transfer, outgoing_transfer = preview.candidates
        self.assertTrue(expense.is_expense)
        self.assertIn('Mobile-Traffic-Data', expense.store)
        self.assertEqual(expense.category, 'Transport miejski')
        self.assertTrue(salary.is_income)
        self.assertEqual(salary.source, 'Pensja')
        self.assertTrue(incoming_transfer.is_income)
        self.assertTrue(outgoing_transfer.is_expense)
        self.assertNotEqual(incoming_transfer.external_id, outgoing_transfer.external_id)

    def test_confirmed_ing_import_sets_source_and_skips_reimport(self):
        self.client.login(username='ing-user', password='pass123')
        preview = parse_bank_csv(_ing_upload(), self.account)

        preview_response = self.client.post(reverse('finance:import_bank_transactions'), {'file': _ing_upload()})
        self.assertEqual(preview_response.status_code, 200)
        self.assertEqual(len(preview_response.context['preview'].candidates), 4)

        import_response = self.client.post(
            reverse('finance:import_bank_transactions'),
            _post_data_from_preview(preview),
        )

        self.assertEqual(import_response.status_code, 302)
        self.assertEqual(Daily.objects.filter(account=self.account, import_source='ing').count(), 2)
        self.assertEqual(Income.objects.filter(account=self.account, import_source='ing').count(), 2)
        monthly = Monthly.objects.get(account=self.account, date=date(2026, 7, 1))
        self.assertEqual(monthly.total_expense, Decimal('1202.38'))
        self.assertEqual(monthly.total_income, Decimal('4602.70'))

        duplicate_response = self.client.post(
            reverse('finance:import_bank_transactions'),
            _post_data_from_preview(preview),
        )

        self.assertEqual(duplicate_response.status_code, 302)
        self.assertEqual(Daily.objects.filter(account=self.account, import_source='ing').count(), 2)
        self.assertEqual(Income.objects.filter(account=self.account, import_source='ing').count(), 2)
