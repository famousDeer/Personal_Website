"""Import Millennium: data z opisu karty, czytelne nazwy sklepów, reguły kategorii."""
import csv
from datetime import date
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase

from finance.bank_import import (
    clean_counterparty_name,
    clean_store_name,
    parse_bank_csv,
    parse_card_description,
)
from finance.models import Daily, Monthly

from .test_bank_import import HEADERS, _post_data_from_preview

User = get_user_model()
ACCOUNT = 'PL00 0000 0000 0000 0000 0000 0000'


def card(description, booked='2026-10-05', amount='-10.00', kind='ZAKUP - FIZ. UŻYCIE KARTY'):
    return [ACCOUNT, booked, booked, kind, '', '', description, amount, '', '100.00', 'PLN']


def upload(rows):
    buffer = StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL)
    writer.writerow(HEADERS)
    writer.writerows(rows)
    return SimpleUploadedFile('historia.csv', buffer.getvalue().encode('utf-8'), content_type='text/csv')


class CardDescriptionTests(SimpleTestCase):
    def test_splits_merchant_city_country_and_date(self):
        parsed = parse_card_description('FRED SP.  Z O.O.  GDYNIA POL 2026-09-30')
        self.assertEqual((parsed.merchant, parsed.city, parsed.country, parsed.date),
                         ('FRED SP. Z O.O.', 'GDYNIA', 'POL', date(2026, 9, 30)))
        parsed = parse_card_description('Amazon.pl*AB12CD  AMAZON.PL LUX 2026-10-05')
        self.assertEqual((parsed.merchant, parsed.country), ('Amazon.pl*AB12CD', 'LUX'))
        self.assertIsNone(parse_card_description('Przelew BLIK na telefon'))

    def test_store_names(self):
        cases = {
            ('LEROY MERLIN GDYNIA', 'GDYNIA'): 'Leroy Merlin',
            ('MOL SF320 K.1', 'GDYNIA'): 'MOL',
            ('ING*mrcleaner.pl', 'KATOWICE'): 'Mrcleaner',
            ('FRED SP. Z O.O.', 'GDYNIA'): 'Fred',
            ('MALINOGROD GDYNIA90115', 'GDYNIA'): 'Malinogrod',
            ('254 GDYNIA', 'GDYNIA'): '254 Gdynia',
            ('www.helios.pl', 'Lodz'): 'Helios',
            ('HELIOS S.A.', 'Gdynia'): 'Helios',
            ('PIEROGARNIA POD LIPA', 'GDYNIA'): 'Pierogarnia Pod Lipa',
            ('Moze Kawy?', 'GDYNIA'): 'Moze Kawy?',
            ('PAYU*SKLEP ROWEROWY', 'POZNAN'): 'Sklep Rowerowy',
            ('BIEDRONKA 1234', 'SOPOT'): 'Biedronka',
        }
        for (merchant, city), expected in cases.items():
            with self.subTest(merchant=merchant):
                self.assertEqual(clean_store_name(merchant, city), expected)

    def test_counterparty_names(self):
        cases = {
            'eneba.com Gyneju st. 4-333 Vilnius': 'Eneba',
            'http://www.k4g.com PPRO Balderton Street  20 00-000 London': 'K4G',
            'Orange Orange Polska S.A. al. Jerozolimskie 160 02-326 Warszawa': 'Orange',
            'PRZYKŁADOWA FIRMA SP. Z O.O. ul. Długa 5 00-001 Warszawa': 'Przykładowa Firma',
            '" MOBILE-TRAFFIC-DATA SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ" DRUŻBICKIEGO 11': 'Mobile-Traffic-Data',
            'Mama': 'Mama',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(clean_counterparty_name(raw), expected)


class MillenniumCardImportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='karta', password='pass123')
        self.account = self.user.owned_finance_accounts.get(account_type='personal')

    def parse(self, rows):
        return parse_bank_csv(upload(rows), self.account).candidates

    def test_card_payment_uses_date_from_description(self):
        weekend, late, future, blik = self.parse([
            card('MOL SF320 K.2  GDYNIA POL 2026-10-03', booked='2026-10-05', amount='-292.75'),
            card('SKLEP  GDYNIA POL 2026-09-01', booked='2026-10-05'),        # za stara - zostaje księgowanie
            card('SKLEP  GDYNIA POL 2026-10-07', booked='2026-10-05'),        # późniejsza - zostaje księgowanie
            [ACCOUNT, '2026-10-05', '2026-10-05', 'PŁATNOŚĆ BLIK W INTERNECIE', '', 'eneba.com Vilnius',
             'abc via Eneba', '-13.39', '', '100.00', 'PLN'],
        ])
        self.assertEqual((weekend.date, weekend.booking_date), (date(2026, 10, 3), date(2026, 10, 5)))
        self.assertTrue(weekend.booked_later)
        self.assertEqual((weekend.store, weekend.category, weekend.title), ('MOL', 'Paliwo', 'Paliwo'))
        self.assertEqual(late.date, date(2026, 10, 5))
        self.assertFalse(late.booked_later)
        self.assertEqual(future.date, date(2026, 10, 5))
        self.assertEqual((blik.date, blik.store, blik.category), (date(2026, 10, 5), 'Eneba', 'Rozrywka'))

    def test_categories_for_common_places(self):
        rows = [
            card('www.helios.pl  Lodz POL 2026-10-04'),
            card('TENDUR KEBAP GRILL  Gdynia POL 2026-09-26'),
            card('PIEROGARNIA POD LIPA  GDYNIA POL 2026-09-27'),
            card('APCOA P AND C 2  WARSZAWA POL 2026-09-26'),
            card('BARBER LOUNGE  GDYNIA POL 2026-09-29'),
        ]
        self.assertEqual(
            [(c.store, c.category) for c in self.parse(rows)],
            [('Helios', 'Rozrywka'), ('Tendur Kebap Grill', 'Jedzenie na miescie'),
             ('Pierogarnia Pod Lipa', 'Jedzenie na miescie'), ('APCOA', 'Transport miejski'),
             ('Barber Lounge', 'Uroda')],
        )

    def test_phone_transfer_to_family(self):
        transfer, = self.parse([[ACCOUNT, '2026-10-05', '2026-10-05', 'PRZELEW NA TELEFON', '00 0000',
                                 'Mama', 'Przelew BLIK na telefon', '-200.00', '', '100.00', 'PLN']])
        self.assertEqual((transfer.title, transfer.store, transfer.category), ('Przelew: Mama', 'Mama', 'Rodzina'))

    def test_weekend_payment_lands_in_its_month_and_matches_manual_entry(self):
        month = Monthly.objects.create(user=self.user, account=self.account, date=date(2026, 9, 1),
                                       total_income=Decimal('0'), total_expense=Decimal('0'))
        # Wpisane ręcznie w dniu tankowania - podobna pozycja z wyciągu ma to wykryć.
        Daily.objects.create(user=self.user, account=self.account, month=month, date=date(2026, 9, 30),
                             title='Paliwo', category='Paliwo', store='MOL', cost=Decimal('292.75'))
        fuel, other = self.parse([
            card('MOL SF320 K.2  GDYNIA POL 2026-09-30', booked='2026-10-02', amount='-292.75'),
            card('IKEA Gdansk  Gdansk POL 2026-09-30', booked='2026-10-02', amount='-3.00'),
        ])
        self.assertTrue(fuel.possible_duplicate)

        self.client.force_login(self.user)
        data = _post_data_from_preview(parse_bank_csv(upload([
            card('IKEA Gdansk  Gdansk POL 2026-09-30', booked='2026-10-02', amount='-3.00'),
        ]), self.account))
        self.client.post('/finance/bank/import/', data)
        expense = Daily.objects.get(store='IKEA')
        self.assertEqual((expense.date, expense.month.date), (date(2026, 9, 30), date(2026, 9, 1)))

    def test_preview_shows_booking_date(self):
        self.client.force_login(self.user)
        response = self.client.post('/finance/bank/import/', {
            'file': upload([card('MOL SF320 K.2  GDYNIA POL 2026-10-03', booked='2026-10-05')]),
        })
        self.assertContains(response, 'value="2026-10-03"')
        self.assertContains(response, 'zaksięg. 05.10')

    def test_history_learns_clean_store_names_on_word_boundaries(self):
        month = Monthly.objects.create(user=self.user, account=self.account, date=date(2026, 9, 1),
                                       total_income=Decimal('0'), total_expense=Decimal('0'))
        for store, category in [('Fred', 'Wyposazenie domu'), ('Ola', 'Prezenty')]:
            Daily.objects.create(user=self.user, account=self.account, month=month, date=date(2026, 9, 2),
                                 title=store, category=category, store=store, cost=Decimal('1.00'))
        fred, dinner = self.parse([
            card('FRED SP.  Z O.O.  GDYNIA POL 2026-10-03'),
            card('KOLACJA BISTRO  GDYNIA POL 2026-10-03'),
        ])
        self.assertEqual((fred.store, fred.category), ('Fred', 'Wyposazenie domu'))
        self.assertIn('historia', fred.suggestion_reason)
        self.assertEqual(dinner.category, 'Jedzenie na miescie')  # nie „Prezenty” od sklepu „Ola”
