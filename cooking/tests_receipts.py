"""Paragony: parser, słownik domu i zatwierdzanie do spiżarni."""
import io
import json
import shutil
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from .models import (
    PantryMovement, PantryProduct, ProductGroup, Receipt, ReceiptAlias, ReceiptLine,
    ShoppingList, ShoppingListItem,
)
from .services.receipt_matching import name_score
from .services.receipt_parser import normalize_text, parse_receipt, price_per_base_unit
from .services.receipts import (
    ACTION_ADD, ACTION_IGNORE, LineDecision, apply_ocr_text, confirm_receipt,
)

User = get_user_model()

BIEDRONKA = """Jeronimo Martins Polska S.A.
Biedronka nr 1234
ul. Morska 100, 81-225 Gdynia
NIP 779-10-11-327
2026-10-17 nr wydr.4521
PARAGON FISKALNY
Mleko UHT 3,2% 1L        C 2 x3,49   6,98C
Jogurt nat. Pilos 400g   C 1 x2,99   2,99C
  Rabat                       -1,00C
Banany luz               C 0,754 x5,99 4,52C
Chleb pszenno-zytni krojony 500g
                         C 1 x4,99   4,99C
Woda Zywiec 6x1,5L       A 1 x12,99 12,99A
SPRZEDAZ OPODATK. A      12,99
PTU A 23%                2,43
SUMA PTU                 3,30
SUMA PLN                31,47
12:34 Kasa 3
"""

LIDL = """LIDL sp. z o.o. sp.k.
ul. Poznańska 48, Jankowice
NIP 781-18-97-358
PARAGON FISKALNY
Mleko 1,5% 1l        1 * 2,79  2,79 C
Ser Gouda plastry 150g 2 * 4,49 8,98 C
Lidl Plus kupon        -2,00
Pomidory luz      0,512 * 9,99  5,11 C
Płyn do naczyń 1L    1 * 6,99  6,99 A
SUMA PTU A 1,31
SUMA PLN 21,87
17.10.2026 18:05  #0123
"""


class ReceiptParserTests(SimpleTestCase):
    def test_biedronka_receipt(self):
        receipt = parse_receipt(BIEDRONKA)
        self.assertEqual(receipt.store_name, 'Biedronka')
        self.assertEqual(receipt.nip, '7791011327')
        self.assertEqual(receipt.purchased_at, datetime(2026, 10, 17, 12, 34))
        self.assertEqual(receipt.receipt_no, '4521')
        self.assertEqual(receipt.total, Decimal('31.47'))
        self.assertTrue(receipt.total_matches)
        names = [line.name for line in receipt.lines]
        self.assertEqual(names, [
            'Mleko UHT 3,2% 1L', 'Jogurt nat. Pilos 400g', 'Banany luz',
            'Chleb pszenno-zytni krojony 500g', 'Woda Zywiec 6x1,5L',
        ])
        jogurt, banany = receipt.lines[1], receipt.lines[2]
        self.assertEqual(jogurt.discount, Decimal('1.00'))
        self.assertEqual(jogurt.paid_price, Decimal('1.99'))
        self.assertTrue(banany.is_weighted)
        self.assertEqual(banany.quantity, Decimal('0.754'))

    def test_lidl_receipt(self):
        receipt = parse_receipt(LIDL)
        self.assertEqual(receipt.store_name, 'Lidl')
        self.assertEqual(receipt.purchased_at, datetime(2026, 10, 17, 18, 5))
        self.assertEqual(receipt.receipt_no, '0123')
        self.assertEqual(len(receipt.lines), 4)
        self.assertEqual(receipt.lines[1].quantity, Decimal('2.000'))
        self.assertEqual(receipt.lines[1].discount, Decimal('2.00'))
        self.assertTrue(receipt.total_matches)

    def test_price_per_base_unit(self):
        lines = parse_receipt(BIEDRONKA).lines
        self.assertEqual(price_per_base_unit(lines[1]), (Decimal('7.48'), 'kg'))   # regularna: 2,99 zł / 0,4 kg
        self.assertEqual(price_per_base_unit(lines[1], paid=True), (Decimal('4.98'), 'kg'))  # po rabacie
        self.assertEqual(price_per_base_unit(lines[2]), (Decimal('5.99'), 'kg'))   # ważone
        self.assertEqual(price_per_base_unit(lines[4]), (Decimal('1.44'), 'l'))    # 12,99 / 9 l

    def test_normalize_and_abbreviations(self):
        self.assertEqual(normalize_text('Jog.nat. Piątnica 400g'), 'JOG NAT PIATNICA 400G')
        self.assertEqual(normalize_text('Łosoś wędz. 100g'), 'LOSOS WEDZ 100G')
        self.assertGreater(name_score('JOG NAT PILOS 400G', 'Jogurt naturalny Pilos'), 0.8)
        self.assertLess(name_score('JOG NAT PILOS 400G', 'Papier toaletowy'), 0.4)

    def test_text_without_items(self):
        receipt = parse_receipt('jakis tekst\nbez cen')
        self.assertEqual(receipt.lines, [])
        self.assertIsNone(receipt.total)


class ReceiptFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='paragon', password='pass12345')
        self.client.force_login(self.user)
        self.milk = PantryProduct.objects.create(
            created_by=self.user, name='Mleko UHT 3,2%', category='Nabiał', unit=PantryProduct.UNIT_PIECE,
        )
        self.yogurt = PantryProduct.objects.create(
            created_by=self.user, name='Jogurt naturalny Pilos', category='Nabiał', barcode='5900000000001',
            unit=PantryProduct.UNIT_GRAM, quantity_per_scan=Decimal('400.00'),
        )
        self.bananas = PantryProduct.objects.create(
            created_by=self.user, name='Banany', category='Warzywa i owoce', unit=PantryProduct.UNIT_GRAM,
        )

    def _receipt(self, text=BIEDRONKA):
        receipt = Receipt.objects.create(created_by=self.user)
        return apply_ocr_text(receipt, text)

    def _decide(self, receipt, mapping):
        decisions = {}
        for line in receipt.lines.all():
            target = mapping.get(line.name)
            if target == 'ignore':
                decisions[line.pk] = LineDecision(action=ACTION_IGNORE)
            elif target:
                decisions[line.pk] = LineDecision(action=ACTION_ADD, target=target[0], quantity=Decimal(target[1]))
        return decisions

    def test_review_suggests_products_and_confirm_adds_stock(self):
        receipt = self._receipt()
        self.assertEqual(receipt.status, Receipt.STATUS_REVIEW)
        self.assertEqual(receipt.store.name, 'Biedronka')
        yogurt_line = receipt.lines.get(name='Jogurt nat. Pilos 400g')
        self.assertEqual(yogurt_line.product, self.yogurt)
        self.assertEqual(yogurt_line.match_status, ReceiptLine.MATCH_UNKNOWN)

        added, linked, _ticked, warnings = confirm_receipt(receipt, self._decide(receipt, {
            'Mleko UHT 3,2% 1L': ('Mleko UHT 3,2%', '2'),
            'Jogurt nat. Pilos 400g': ('Jogurt naturalny Pilos', '1'),
            'Banany luz': ('Banany', '0.754'),
            'Woda Zywiec 6x1,5L': ('Woda Żywiec 1,5 l', '6'),
            'Chleb pszenno-zytni krojony 500g': 'ignore',
        }), user=self.user)
        self.assertEqual((added, linked, warnings), (4, 0, []))
        self.milk.refresh_from_db(); self.yogurt.refresh_from_db(); self.bananas.refresh_from_db()
        self.assertEqual(self.milk.current_quantity, Decimal('2.00'))
        self.assertEqual(self.yogurt.current_quantity, Decimal('400.00'))
        self.assertEqual(self.bananas.current_quantity, Decimal('754.00'))
        water = PantryProduct.objects.get(name='Woda Żywiec 1,5 l')
        self.assertEqual(water.current_quantity, Decimal('6.00'))
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, Receipt.STATUS_DONE)
        self.assertEqual(
            ReceiptAlias.objects.get(normalized_text='WODA ZYWIEC 6X1,5L').multiplier, 6,
        )
        self.assertTrue(ReceiptAlias.objects.get(normalized_text__startswith='CHLEB').ignore)

    def test_second_receipt_uses_household_dictionary(self):
        first = self._receipt()
        confirm_receipt(first, self._decide(first, {
            'Mleko UHT 3,2% 1L': ('Mleko UHT 3,2%', '2'),
            'Chleb pszenno-zytni krojony 500g': 'ignore',
        }), user=self.user)
        second = self._receipt(BIEDRONKA.replace('nr wydr.4521', 'nr wydr.4600'))
        milk_line = second.lines.get(name='Mleko UHT 3,2% 1L')
        self.assertEqual(milk_line.match_status, ReceiptLine.MATCH_AUTO)
        self.assertEqual(milk_line.product, self.milk)
        self.assertEqual(
            second.lines.get(name='Chleb pszenno-zytni krojony 500g').match_status, ReceiptLine.MATCH_IGNORED,
        )

    def test_same_day_scan_is_linked_not_added_twice(self):
        receipt = self._receipt()
        self.yogurt.current_quantity = Decimal('400.00')
        self.yogurt.save()
        scanned = PantryMovement.objects.create(
            product=self.yogurt, movement_type=PantryMovement.PURCHASE, quantity=Decimal('400.00'),
            occurred_on=receipt.purchased_on,
        )
        added, linked, _ticked, _warnings = confirm_receipt(receipt, self._decide(receipt, {
            'Jogurt nat. Pilos 400g': ('Jogurt naturalny Pilos', '1'),
        }), user=self.user)
        self.assertEqual((added, linked), (0, 1))
        self.yogurt.refresh_from_db()
        self.assertEqual(self.yogurt.current_quantity, Decimal('400.00'))
        self.assertEqual(receipt.lines.get(name='Jogurt nat. Pilos 400g').movement, scanned)

    def test_shopping_list_item_is_ticked_without_double_stock(self):
        shopping_list = ShoppingList.objects.create(created_by=self.user, title='Zakupy')
        item = ShoppingListItem.objects.create(
            shopping_list=shopping_list, name='Mleko UHT 3,2%', pantry_product=self.milk, quantity=Decimal('2'),
        )
        receipt = self._receipt()
        confirm_receipt(receipt, self._decide(receipt, {'Mleko UHT 3,2% 1L': ('Mleko UHT 3,2%', '2')}), user=self.user)
        item.refresh_from_db(); self.milk.refresh_from_db()
        self.assertTrue(item.is_purchased)
        self.assertIsNotNone(item.pantry_movement_id)
        self.assertEqual(self.milk.current_quantity, Decimal('2.00'))

    def test_group_target_adds_to_brand(self):
        group = ProductGroup.objects.create(name='Jogurt naturalny', category='Nabiał')
        self.yogurt.group = group
        self.yogurt.save()
        receipt = self._receipt()
        confirm_receipt(receipt, self._decide(receipt, {'Jogurt nat. Pilos 400g': ('Jogurt naturalny', '2')}), user=self.user)
        self.yogurt.refresh_from_db()
        self.assertEqual(self.yogurt.current_quantity, Decimal('800.00'))
        self.assertEqual(ReceiptAlias.objects.get(normalized_text='JOGURT NAT PILOS 400G').group, group)

    def test_duplicate_receipt_is_flagged(self):
        first = self._receipt()
        second = self._receipt()
        self.assertTrue(first.fingerprint)
        self.assertEqual(second.duplicate_of, first)
        self.assertEqual(second.fingerprint, '')

    def test_confirm_twice_is_rejected(self):
        receipt = self._receipt()
        confirm_receipt(receipt, {}, user=self.user)
        with self.assertRaises(ValueError):
            confirm_receipt(receipt, {}, user=self.user)


class ReceiptViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='widok', password='pass12345')
        self.client.force_login(self.user)
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        self.outbox = Path(self.media) / 'outbox'

    def _photo(self):
        buffer = io.BytesIO()
        Image.new('RGB', (400, 900), 'white').save(buffer, format='JPEG')
        return SimpleUploadedFile('paragon.jpg', buffer.getvalue(), content_type='image/jpeg')

    def test_pasted_text_goes_to_review(self):
        response = self.client.post(reverse('cooking:receipts'), {'text': LIDL})
        receipt = Receipt.objects.get()
        self.assertRedirects(response, reverse('cooking:receipt-detail', args=[receipt.uuid]))
        page = self.client.get(reverse('cooking:receipt-detail', args=[receipt.uuid]))
        self.assertContains(page, 'Sumy się zgadzają')
        self.assertContains(page, 'Ser Gouda plastry 150g')

    def test_photo_upload_ocr_confirm_and_archive(self):
        with override_settings(PRIVATE_MEDIA_ROOT=Path(self.media), RECEIPT_ARCHIVE_DIR=str(self.outbox)), \
                patch('cooking.services.receipts.read_text', return_value=LIDL) as ocr, \
                self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('cooking:receipts'), {'photos': [self._photo()]})
            receipt = Receipt.objects.get()
            self.assertEqual(ocr.call_count, 1)
            self.assertEqual(receipt.status, Receipt.STATUS_REVIEW)
            photo = receipt.photos.get()
            self.assertEqual(self.client.get(reverse('cooking:receipt-photo', args=[receipt.uuid, photo.pk])).status_code, 200)

            data = {}
            for line in receipt.lines.all():
                data[f'action_{line.pk}'] = 'add'
                data[f'target_{line.pk}'] = line.name
                data[f'qty_{line.pk}'] = str(line.quantity)
            response = self.client.post(reverse('cooking:receipt-detail', args=[receipt.uuid]), data, follow=True)
        self.assertContains(response, 'Dodano do spiżarni: 4 pozycje')
        photo.refresh_from_db()
        self.assertFalse(photo.image)
        self.assertTrue((self.outbox / photo.archived_name).exists())
        self.assertTrue(photo.archived_name.startswith('2026-10-17_lidl_'))
        self.assertEqual(PantryProduct.objects.get(name='Pomidory luz').unit, PantryProduct.UNIT_KILOGRAM)

    def test_failed_ocr_shows_error(self):
        from .services.receipt_ocr import ReceiptOcrError

        with override_settings(PRIVATE_MEDIA_ROOT=Path(self.media)), \
                patch('cooking.services.receipts.read_text', side_effect=ReceiptOcrError('Brak programu tesseract na serwerze.')):
            self.client.post(reverse('cooking:receipts'), {'photos': [self._photo()]})
        receipt = Receipt.objects.get()
        self.assertEqual(receipt.status, Receipt.STATUS_FAILED)
        self.assertContains(self.client.get(reverse('cooking:receipt-detail', args=[receipt.uuid])), 'Brak programu tesseract')

    def test_delete_unconfirmed_receipt(self):
        self.client.post(reverse('cooking:receipts'), {'text': LIDL})
        receipt = Receipt.objects.get()
        self.client.post(reverse('cooking:receipt-delete', args=[receipt.uuid]))
        self.assertFalse(Receipt.objects.exists())

    def test_list_page(self):
        self.client.post(reverse('cooking:receipts'), {'text': LIDL})
        response = self.client.get(reverse('cooking:receipts'))
        self.assertContains(response, 'Lidl')
        self.assertContains(response, 'Do sprawdzenia')


# Prawdziwy e-paragon z aplikacji Lidl Plus (09.10.2026) po odczycie Tesseractem.
# Czcionka ma przekreślone zero, więc OCR czyta „0” jako „8” (2826-10-09, 08,614kg),
# a minus przy kuponie jako „=” albo „"”.
LIDL_E_RECEIPT_OCR = '''L9DL.

Adres siedziby: Poznańska 48, Jankowice
62-888 Tarnowo
Podgórne nr rej: BDO 080002265 Lidl sp.
Z 6. 6... ŚPix IG.
ul. Małokacka 31, 81-654 Gdynia
2826-10-09

Somersby 8% piwo
4 * 5.99 23.96 C
Lidl Plus kupon            -12,12
Piw.Somer .8%Cher/Ap
4 * 5.99 23.96 A
Lidl Plus kupon            =12,12
Piw.Somer .8%Cher/Ap
4 * 5.99 23.96 A
Lidl Plus kupon            "12, 12
Lech Free 8% L84M 20%
4 * 3.99 15.96 C
Lidl Plus kupon             -8,08
Mutti Pomidory polpa
2 * 6.29 12.58 C
Taniej za 2                =|,26
Martini As. Prosecco
1 * 39.99 39.99 A
Mionetto Prosecco
1 * 34.99 34.99 A
Mrożone maliny
1 * 16.99 16.99 C
Cytryny luz
1,546kg x 5.99 9.26 C
Pomarańcze deser. luz
08,614kg x 5.99 3.68 C

PTU A                          98,66
Kwota A 23,08%                   18,45
PTU C                           60,97
Kwota C 5,00%                     2,90
Suma                            21.,89
Suma PLN     159,63
85 10845      nr:   241826        18:37
Suma         159,63
Płatność        Karta płatnicza 159,63

'''


class LidlEReceiptOcrTests(SimpleTestCase):
    def test_real_lidl_e_receipt(self):
        receipt = parse_receipt(LIDL_E_RECEIPT_OCR)
        self.assertEqual(receipt.store_name, 'Lidl')
        self.assertEqual(receipt.purchased_at, datetime(2026, 10, 9, 18, 37))
        self.assertEqual(receipt.receipt_no, '241826')
        self.assertEqual(receipt.total, Decimal('159.63'))
        self.assertEqual(len(receipt.lines), 10)
        self.assertEqual(
            [line.discount for line in receipt.lines[:5]],
            [Decimal('12.12')] * 3 + [Decimal('8.08'), Decimal('1.26')],
        )
        oranges = receipt.lines[-1]
        self.assertEqual(oranges.name, 'Pomarańcze deser. luz')
        self.assertEqual(oranges.quantity, Decimal('0.614'))
        self.assertTrue(oranges.is_weighted)
        self.assertEqual(receipt.lines_total, Decimal('159.63'))
        self.assertTrue(receipt.total_matches)


class ReceiptRegularPriceTests(TestCase):
    """Historia cen opiera się na cenie regularnej; rabat zapisany osobno z rodzajem."""

    def test_regular_and_paid_price_with_discount_kinds(self):
        receipt = apply_ocr_text(Receipt.objects.create(), LIDL_E_RECEIPT_OCR)
        somersby = receipt.lines.get(position=1)
        self.assertEqual(somersby.discount_kind, ReceiptLine.DISCOUNT_APP)
        self.assertEqual(somersby.discount_label, 'Lidl Plus kupon')
        self.assertEqual((somersby.price_per_base_unit, somersby.base_unit), (Decimal('5.99'), 'szt'))
        self.assertEqual(somersby.paid_per_base_unit, Decimal('2.96'))   # 11,84 / 4

        mutti = receipt.lines.get(name='Mutti Pomidory polpa')
        self.assertEqual(mutti.discount_kind, ReceiptLine.DISCOUNT_MULTIBUY)
        self.assertEqual(mutti.price_per_base_unit, Decimal('6.29'))
        self.assertEqual(mutti.paid_per_base_unit, Decimal('5.66'))      # 11,32 / 2

        jogurt = apply_ocr_text(Receipt.objects.create(), BIEDRONKA).lines.get(name='Jogurt nat. Pilos 400g')
        self.assertEqual(jogurt.discount_kind, ReceiptLine.DISCOUNT_PROMO)
        self.assertEqual(jogurt.price_per_base_unit, Decimal('7.48'))    # 2,99 / 0,4 kg
        self.assertEqual(jogurt.paid_per_base_unit, Decimal('4.98'))

    def test_review_page_shows_regular_price_and_discount(self):
        user = User.objects.create_user(username='ceny', password='pass12345')
        self.client.force_login(user)
        self.client.post(reverse('cooking:receipts'), {'text': LIDL_E_RECEIPT_OCR})
        receipt = Receipt.objects.get()
        page = self.client.get(reverse('cooking:receipt-detail', args=[receipt.uuid]))
        self.assertContains(page, 'Kupon z aplikacji')
        self.assertContains(page, 'Promocja za kilka sztuk')
        self.assertContains(page, 'cena regularna')


# Prawdziwy e-paragon PDF z aplikacji Biedronki (08.10.2026), strony jako obrazki,
# po odczycie Tesseractem. Szum OCR: litery PTU („Cc”, „Ć”, „e”), nazwa zawinięta
# do drugiej linii (Wedel90g), „1.000” dla sztuk i nieczytelna linia płatków.
BIEDRONKA_E_RECEIPT_OCR = '''Sklep 5343 BEMA 22/24
Jeronimo Martins Polska S. A.
ul. Żniwna 5, 62-025 Kostrzyn
NIP 779-10-11-327
08.10.2026 21:51
NIEFISKALNY
Nazwa                  PTU      Ilość        Cena Wartość
NapBlackMix0,5l        A   1.000 x     5,49      5,49
Puszka Kaucja        brak    1.000 x     0,50      0,50
Pom Paprycz Luz          Cc    0.428 x     27,99      11,98
Rabat                                                 -5,14
6,84
CzekGorzk64          A   3.000 x     9,99     29,97
Wedel90g
Rabat                                                 -9,99
19,98
CzekBiałaWedel80g     A    3.000 x     7,99     23,97
Rabat                                                 -7,99
15,98
Cuk Krówka Luz           A    0.298 x     19,90       5,93
Pł CornFlakes 250g       Ć    1.000 x      5,99       5,99
Rabat                                                 -1,81
4,18
PłatkiCiniMin375g       e   1.000 x    14,29     1. l,28
KREM GŁADKI            B    1.000 x     16,99      16,99
NaklejkaAkcja2026      A   3.000 x     1,00      3,00
Rabat                                                 -3,00
0,00
Sprzedaż opodatkowana A                                47,38
PTU A 23%                                                  8,86
Sprzedaż opodatkowana brak                              0,50
PTU brak 0%                                                0,00
Sprzedaż opodatkowana C                                22,31
PTU C5%                                                   1,06
Sprzedaż opodatkowana B                                16,99
PTU B 8%                                                   1,26
5343/2/142/08.10.2026                              Strona lz 2
Suma PTU                                           11,18
Suma PLN                          87,18
Kasa 2 Kasjer
0053430142247202
Karta płatnicza                                            87,18
Numer transakcji                                            142
'''
CINI_NOISY = 'PłatkiCiniMin375g       e   1.000 x    14,29     1. l,28'
CINI_CLEAN = 'PłatkiCiniMin375g       C    1.000 x    11,29     11,29'


class BiedronkaEReceiptTests(TestCase):
    def test_noisy_ocr_is_parsed(self):
        receipt = parse_receipt(BIEDRONKA_E_RECEIPT_OCR)
        self.assertEqual(receipt.store_name, 'Biedronka')
        self.assertEqual(receipt.nip, '7791011327')
        self.assertEqual(receipt.purchased_at, datetime(2026, 10, 8, 21, 51))
        self.assertEqual(receipt.receipt_no, '142')
        self.assertEqual(receipt.total, Decimal('87.18'))
        names = [line.name for line in receipt.lines]
        self.assertIn('CzekGorzk64 Wedel90g', names)
        self.assertIn('Pom Paprycz Luz', names)
        self.assertNotIn('PłatkiCiniMin375g', names)          # śmieci w kwocie - brak pozycji
        self.assertFalse(receipt.total_matches)
        by_name = {line.name: line for line in receipt.lines}
        self.assertFalse(by_name['NapBlackMix0,5l'].is_weighted)   # „1.000” to sztuka
        self.assertTrue(by_name['Pom Paprycz Luz'].is_weighted)
        self.assertEqual(by_name['CzekGorzk64 Wedel90g'].discount_kind, 'multibuy')   # 3 za 2
        self.assertEqual(by_name['CzekGorzk64 Wedel90g'].paid_price, Decimal('19.98'))

    def test_second_ocr_attempt_when_sums_differ(self):
        clean = BIEDRONKA_E_RECEIPT_OCR.replace(CINI_NOISY, CINI_CLEAN)
        receipt = Receipt.objects.create()
        receipt.photos.create(position=1, image='receipts/strona-1.jpg')
        with patch('cooking.services.receipts.read_text', side_effect=[BIEDRONKA_E_RECEIPT_OCR, clean]) as ocr:
            from .services.receipts import process_receipt

            process_receipt(receipt.pk)
        receipt.refresh_from_db()
        self.assertEqual(ocr.call_count, 2)
        self.assertEqual(ocr.call_args_list[1].args[1], 1)
        self.assertTrue(receipt.total_matches)
        self.assertEqual(receipt.lines.count(), 10)

    def test_deposit_and_free_items_default_to_skip(self):
        user = User.objects.create_user(username='kaucja', password='pass12345')
        self.client.force_login(user)
        self.client.post(reverse('cooking:receipts'), {'text': BIEDRONKA_E_RECEIPT_OCR.replace(CINI_NOISY, CINI_CLEAN)})
        receipt = Receipt.objects.get()
        page = self.client.get(reverse('cooking:receipt-detail', args=[receipt.uuid])).content.decode()
        for name in ('Puszka Kaucja', 'NaklejkaAkcja2026'):
            line = receipt.lines.get(name=name)
            block = page[page.index(f'name="action_{line.pk}"'):][:400]
            self.assertIn('value="skip" selected', block)

    @skipUnless(shutil.which('pdftoppm'), 'brak poppler-utils')
    def test_pdf_upload_is_rendered_to_pages(self):
        user = User.objects.create_user(username='pdf', password='pass12345')
        self.client.force_login(user)
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        buffer = io.BytesIO()
        pages = [Image.new('RGB', (600, 1400), 'white'), Image.new('RGB', (600, 1400), 'white')]
        pages[0].save(buffer, format='PDF', save_all=True, append_images=pages[1:])
        upload = SimpleUploadedFile('receipt.pdf', buffer.getvalue(), content_type='application/pdf')
        clean = BIEDRONKA_E_RECEIPT_OCR.replace(CINI_NOISY, CINI_CLEAN)
        with override_settings(PRIVATE_MEDIA_ROOT=Path(media)), \
                patch('cooking.services.receipts.read_text', side_effect=[clean, '']) as ocr:
            self.client.post(reverse('cooking:receipts'), {'photos': [upload]})
        receipt = Receipt.objects.get()
        self.assertEqual(receipt.photos.count(), 2)
        self.assertEqual(ocr.call_count, 2)                     # obie strony, jedna próba
        self.assertEqual(receipt.status, Receipt.STATUS_REVIEW)
        self.assertTrue(receipt.total_matches)


# Prawdziwy papierowy paragon z Biedronki (17.10.2026), zdjęcie z telefonu, odczyt Tesseractem.
BIEDRONKA_PAPER_OCR = '''BIEDRONKA "CODZIENNIE NISKIE CENY" 5343

81-386 GDYNIA UL. BEHA 22/24

LANE CS?
NIP 7781011327                            nr:263558

PARAGON FISKALNY

Bazyl iaXLUF           C             1 «7,99 7,990
ChipsyLay" sPapr190g                   1 x9,49 9,490
SerRicottGalb 250g                   2 x5,99 11,980
Zottómietanka30 2009                  2 x4,79 9,580
SerMozzarellat25g                    1 «2,89 2,99C

42,03
PTU: (5422,00                     SUHA PTU=2,00

SUHA PLN

R        ć
KARTA VISA 07 1                          42,03 PLN
00080 Kasa 14 Kasjer nr 19          2026-10-17 14:04
3600E944EBE649CA130075190E/BA944DC06DB39
JE? EAZ 2202469796
NE „A ansakc j ji:

III Jad

10600534316302481
Nr sys. (650.
NIP 7794011327                         nr:263559
IEFISKALNY

MOJE  ZAKUPY
MOJE OSZCZĘDNOŚCI

SE
Biędr  onka

NIEFTSKALNY
z” "BAŁ zZcDEE1EO8G00RZOGCH FAY       falta
(FB
EAZ_2202169796  Ali

DZIEKUJENY ZAPRASZANY PONOWNIE
BDO 000004585   Aa:

'''

# Papierowy paragon z Aldka przepisany ze zdjęcia (pognieciony - Tesseract go nie czyta,
# ale tekst skopiowany np. Live Textem z iPhone'a ma taki układ). Litera PTU doklejona do nazwy.
ALDEK = '''ALDEK 34 M. Witulski S.J.
Żołnierzy I Armii Wojska Polskiego 3-11
81-380 Gdynia
BDO 000154919
NIP: 586-20-61-930                     W282369
PARAGON FISKALNY
CDS PAPRYKA JALAPENO ZIEL KROJ 670GC 1*11.69     11.69C
LIMINI SOK Z LIMONKI 200MLC 1*5.89               5.89C
MLEKOVITA ML W PROSZKU INST 250GC 1*8.89         8.89C
KONKOL GROSZEK PTYSIOWY 50GC 1*3.19              3.19C
LORENZ POPCORN 100G SOLONYC 1*5.39               5.39C
LOTUS BISCOFF 250G CIASTKAC 1*7.89               7.89C
KONKOL GROSZEK PTYSIOWY 50GC 1*3.19              3.19C
WEDEL CRUSH 25G PRECEL PEANUTSA 3*2.65           7.95A
KONKOL GROSZEK PTYSIOWY 50GC 1*3.19              3.19C
Sprzedaż opodatkowana A:                          7.95
Sprzedaż opodatkowana C:                         49.32
Kwota PTU A 23%                                   1.49
Kwota PTU C 5%                                    2.35
SUMA PTU                                          3.84
SUMA:                                       PLN 57.27
DO ZAPŁATY:                                     57.27
ROZLICZENIE PŁATNOŚCI
                                                57.27
Karta:
                                    09-10-2026 08:41
F274510 #1 5-Sylwia
'''


class PaperReceiptTests(SimpleTestCase):
    def test_biedronka_paper_photo(self):
        receipt = parse_receipt(BIEDRONKA_PAPER_OCR)
        self.assertEqual(receipt.store_name, 'Biedronka')
        self.assertEqual(receipt.purchased_at, datetime(2026, 10, 17, 14, 4))
        self.assertEqual(receipt.total, Decimal('42.03'))
        self.assertEqual(len(receipt.lines), 5)
        self.assertTrue(receipt.total_matches)
        self.assertEqual([line.quantity for line in receipt.lines], [Decimal('1.000'), 1, 2, 2, 1])

    def test_aldek_layout(self):
        receipt = parse_receipt(ALDEK)
        self.assertEqual(receipt.nip, '5862061930')
        self.assertEqual(receipt.purchased_at, datetime(2026, 10, 9, 8, 41))
        self.assertEqual(receipt.total, Decimal('57.27'))
        self.assertEqual(len(receipt.lines), 9)
        self.assertTrue(receipt.total_matches)
        names = [line.name for line in receipt.lines]
        self.assertEqual(names[0], 'CDS PAPRYKA JALAPENO ZIEL KROJ 670G')   # bez doklejonego „C”
        self.assertEqual(names[4], 'LORENZ POPCORN 100G SOLONY')
        self.assertEqual(names[7], 'WEDEL CRUSH 25G PRECEL PEANUTS')
        self.assertEqual(receipt.lines[7].quantity, Decimal('3.000'))
        self.assertEqual(price_per_base_unit(receipt.lines[0]), (Decimal('17.45'), 'kg'))   # 11,69 / 0,67 kg

    def test_glued_letter_kept_when_not_whole_receipt(self):
        receipt = parse_receipt(LIDL)
        self.assertEqual(receipt.lines[0].name, 'Mleko 1,5% 1l')


# Paragon z Aldka (pognieciony) - tekst skopiowany Live Textem z iPhone'a i wklejony
# na stronie (17.10.2026). Pozycja w dwóch liniach, „C” czytane jako „0”, „3. 190”,
# „1+3.19”, „SUMA :” i kwota w następnej linii, nagłówek przemieszany.
ALDEK_LIVE_TEXT = '''ÄLDEK 34 M. Witulski S.J.
Zotnierzy I Armii Wojska Polskiego 3-11
81-380 Goynia
BDO 000154919
DO ZAPŁATY:
ROZLICZENIE PŁATNOSCI
Karta:
F274510 #1 5-Sytwia
50534F9A3473318673EA188527B1136BAC45C61A
AE> EBT 2101692116
NIP: 586-20-61-930
W282369
PARAGON FISKALNY

CDS PAPRYKA JALAPENO ZIEL KROJ 670GC 1*11.69
11.690
LIMINI SOK Z LIMONKI 200MLC 1×5.89
5.89C
MLEKOVITA ML W PROSZKU INST 250GC 1*8.09
8.89C
KONKOL GROSZEK PTYSIOWY 50GC 1*3.19
3.19C
LORENZ POPCORN 100G SOLONYC 1*5.39
5.39C
LOTUS BISCOFF 250G CIASTKAC 1*7.89
7.89C
KONKOL GROSZEK PTYSIOWY 50GC 1+3.19
3. 190
WEDEL CRUSH 25G PRECEL PEANUTSA 3*2.65
7. 95A
KONKOL GROSZEK PTYSIOWY 50GC 1*3.19
3.19C
Sprzedaz opodatkowana A:
7.95
Sprzedaz opodatkowana C:
49.32
Kwota PTU A 23%
1.49
Kwota PTU C 5%
2.35
SUMA PTU
3.84
SUMA :
PLN 57.27

57.27
57.27
09-10-2026 08:41
'''


class AldekLiveTextTests(TestCase):
    def test_live_text_two_line_items(self):
        receipt = parse_receipt(ALDEK_LIVE_TEXT)
        self.assertEqual(receipt.store_name, 'Aldek')
        self.assertEqual(receipt.purchased_at, datetime(2026, 10, 9, 8, 41))
        self.assertEqual(receipt.total, Decimal('57.27'))
        self.assertEqual(len(receipt.lines), 9)
        self.assertTrue(receipt.total_matches)
        self.assertEqual(receipt.lines[0].name, 'CDS PAPRYKA JALAPENO ZIEL KROJ 670G')
        self.assertEqual(receipt.lines[2].unit_price, Decimal('8.89'))     # „1*8.09” poprawione do wartości 8.89
        self.assertEqual(receipt.lines[7].quantity, Decimal('3.000'))
        self.assertEqual(receipt.lines[7].paid_price, Decimal('7.95'))

    def test_pasted_live_text_goes_to_review(self):
        user = User.objects.create_user(username='livetext', password='pass12345')
        self.client.force_login(user)
        self.client.post(reverse('cooking:receipts'), {'text': ALDEK_LIVE_TEXT})
        receipt = Receipt.objects.get()
        self.assertEqual(receipt.status, Receipt.STATUS_REVIEW)
        self.assertEqual(receipt.lines.count(), 9)


class PriceHistoryAndBarcodeLinkTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='ceny2', password='pass12345')
        self.client.force_login(self.user)
        self.mutti = PantryProduct.objects.create(created_by=self.user, name='Passata Mutti', unit=PantryProduct.UNIT_PIECE)

    def _confirm(self, text):
        receipt = apply_ocr_text(Receipt.objects.create(), text)
        line = receipt.lines.get(name='Mutti Pomidory polpa')
        confirm_receipt(receipt, {line.pk: LineDecision(action=ACTION_ADD, target='Passata Mutti', quantity=Decimal('2'))})
        return receipt

    def test_price_change_uses_regular_price(self):
        self._confirm(LIDL_E_RECEIPT_OCR.replace('2826-10-09', '2026-09-01').replace('241826', '100001')
                      .replace('2 * 6.29 12.58 C', '2 * 5.99 11.98 C').replace('159,63', '159,03'))
        self._confirm(LIDL_E_RECEIPT_OCR)
        from .services.receipt_prices import price_summary

        summary = price_summary(self.mutti)
        self.assertEqual(len(summary.observations), 2)
        self.assertEqual(summary.last.regular, Decimal('6.29'))     # regularna, nie 5,66 po „taniej za 2”
        self.assertEqual(summary.last.paid, Decimal('5.66'))
        self.assertEqual(summary.change, Decimal('5.0'))            # 5,99 → 6,29
        self.assertEqual(summary.trend, 'up')

        pantry = self.client.get(reverse('cooking:pantry'))
        self.assertContains(pantry, '6,29 zł/szt')
        prices = self.client.get(reverse('cooking:prices'))
        self.assertContains(prices, 'Passata Mutti')
        self.assertContains(prices, '+5,0%')
        detail = self.client.get(reverse('cooking:product-prices', args=[self.mutti.pk]))
        self.assertContains(detail, 'price-chart-data')
        self.assertContains(detail, 'Promocja za kilka sztuk')

    def test_unknown_barcode_suggests_receipt_product_and_links_it(self):
        self._confirm(LIDL_E_RECEIPT_OCR)
        lookup = self.client.get(reverse('cooking:pantry-barcode-lookup'), {'barcode': '8005110070600'}).json()
        self.assertEqual(lookup['status'], 'unknown')
        self.assertEqual(lookup['link_candidates'][0]['id'], self.mutti.pk)
        self.assertIn('Lidl', lookup['link_candidates'][0]['hint'])

        response = self.client.post(
            reverse('cooking:pantry-barcode-link'),
            data=json.dumps({'barcode': '8005110070600', 'product_id': self.mutti.pk}),
            content_type='application/json',
        )
        self.assertEqual(response.json()['product']['name'], 'Passata Mutti')
        self.mutti.refresh_from_db()
        self.assertEqual(self.mutti.barcode, '8005110070600')
        again = self.client.get(reverse('cooking:pantry-barcode-lookup'), {'barcode': '8005110070600'}).json()
        self.assertEqual(again['status'], 'known')

        other = PantryProduct.objects.create(created_by=self.user, name='Inny')
        conflict = self.client.post(
            reverse('cooking:pantry-barcode-link'),
            data=json.dumps({'barcode': '8005110070600', 'product_id': other.pk}),
            content_type='application/json',
        )
        self.assertEqual(conflict.status_code, 409)


class ReceiptBarcodeScanModeTests(TestCase):
    """Tryb „Zeskanuj kody nowych produktów” po zatwierdzeniu paragonu."""

    def setUp(self):
        self.user = User.objects.create_user(username='kody', password='pass12345')
        self.client.force_login(self.user)
        self.receipt = apply_ocr_text(Receipt.objects.create(), LIDL_E_RECEIPT_OCR)
        decisions = {}
        for line in self.receipt.lines.all():
            if line.name in ('Mutti Pomidory polpa', 'Mrożone maliny', 'Cytryny luz'):
                decisions[line.pk] = LineDecision(action=ACTION_ADD, target=line.name, quantity=line.quantity)
        confirm_receipt(self.receipt, decisions, user=self.user)
        self.url = reverse('cooking:receipt-barcodes', args=[self.receipt.uuid])

    def _scan(self, barcode, product_id=None):
        return self.client.post(self.url, data=json.dumps({'barcode': barcode, 'product_id': product_id}),
                                content_type='application/json').json()

    def test_done_page_offers_scan_mode_without_weighted_items(self):
        page = self.client.get(reverse('cooking:receipt-detail', args=[self.receipt.uuid]))
        self.assertContains(page, 'Rozpakuj zakupy (2)')   # cytryny luzem bez kodu
        scan_page = self.client.get(self.url)
        self.assertContains(scan_page, 'Mrożone maliny')
        self.assertNotContains(scan_page, 'data-product-id="%d"' % PantryProduct.objects.get(name='Cytryny luz').pk)

    def test_ambiguous_scan_asks_then_last_one_links_itself(self):
        first = self._scan('5900000000017')
        self.assertEqual(first['status'], 'choose')
        maliny = PantryProduct.objects.get(name='Mrożone maliny')
        chosen = self._scan('5900000000017', maliny.pk)
        self.assertEqual(chosen['status'], 'linked')
        self.assertEqual([item['name'] for item in chosen['remaining']], ['Mutti Pomidory polpa'])
        last = self._scan('8005110070600')                                # jeden został - bez pytania
        self.assertEqual((last['status'], last['product']), ('linked', 'Mutti Pomidory polpa'))
        self.assertEqual(last['remaining'], [])
        self.assertEqual(self._scan('8005110070600')['status'], 'known')
        self.assertEqual(PantryProduct.objects.get(barcode='8005110070600').name, 'Mutti Pomidory polpa')

    def test_catalog_name_links_without_asking(self):
        with patch('cooking.services.receipts._catalog_name', return_value='Mutti Polpa pomidory'):
            result = self._scan('8005110070600')
        self.assertEqual((result['status'], result['product']), ('linked', 'Mutti Pomidory polpa'))


class ExpiryTests(TestCase):
    """Terminy przydatności: obowiązkowe dla nabiału, partie od najkrótszego terminu."""

    def setUp(self):
        from datetime import timedelta

        self.user = User.objects.create_user(username='terminy', password='pass12345')
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.in_days = lambda days: self.today + timedelta(days=days)
        self.yogurt = PantryProduct.objects.create(
            created_by=self.user, name='Jogurt Pilos', category='Nabiał', barcode='5900000000999',
            unit=PantryProduct.UNIT_GRAM, quantity_per_scan=Decimal('400'),
        )

    def _scan(self, action, expires_on=''):
        from uuid import uuid4

        return self.client.post(reverse('cooking:pantry-barcode-action'), data=json.dumps({
            'barcode': self.yogurt.barcode, 'scan_id': str(uuid4()), 'action': action, 'count': 1,
            'expires_on': expires_on,
        }), content_type='application/json')

    def test_dairy_needs_date_and_consumption_takes_shortest_date_first(self):
        from .models import PantryBatch
        from .services.pantry_batches import expiry_overview

        self.assertEqual(self._scan('purchase').status_code, 400)
        self.assertEqual(self._scan('purchase', self.in_days(10).isoformat()).status_code, 200)
        self.assertEqual(self._scan('purchase', self.in_days(2).isoformat()).status_code, 200)
        self.assertEqual(PantryBatch.objects.count(), 2)
        self.assertEqual(self._scan('consume').status_code, 200)              # bez pytania o partię
        self.yogurt.refresh_from_db()
        overview = expiry_overview()
        remaining = list(PantryBatch.objects.values_list('expires_on', flat=True))
        self.assertEqual(remaining, [self.in_days(10)])                        # zeszła partia z krótszym terminem
        self.assertEqual(overview.eat_first, [])
        self.assertEqual(overview.missing, [])

    def test_pantry_page_eat_first_and_missing_date(self):
        from .services.pantry_batches import add_batch

        milk = PantryProduct.objects.create(created_by=self.user, name='Mleko', category='Nabiał',
                                            current_quantity=Decimal('2'))
        self.yogurt.current_quantity = Decimal('400')
        self.yogurt.save()
        add_batch(self.yogurt, Decimal('400'), self.in_days(1))
        page = self.client.get(reverse('cooking:pantry'))
        self.assertContains(page, 'Zjedz najpierw')
        self.assertContains(page, 'jutro')
        self.assertContains(page, 'Uzupełnij termin')
        self.assertContains(page, reverse('cooking:pantry-expiry', args=[milk.pk]))

        self.client.post(reverse('cooking:pantry-expiry', args=[milk.pk]), {'expires_on': self.in_days(7).isoformat()})
        self.assertEqual(milk.batches.get().expires_on, self.in_days(7))
        self.assertNotContains(self.client.get(reverse('cooking:pantry')), 'Uzupełnij termin')

    def test_unpack_mode_asks_date_for_dairy_from_receipt(self):
        receipt = apply_ocr_text(Receipt.objects.create(), BIEDRONKA)
        line = receipt.lines.get(name='Jogurt nat. Pilos 400g')
        confirm_receipt(receipt, {line.pk: LineDecision(action=ACTION_ADD, target='Jogurt Pilos', quantity=Decimal('1'))})
        url = reverse('cooking:receipt-barcodes', args=[receipt.uuid])
        page = self.client.get(url)
        self.assertContains(page, 'data-needs-date="1"')
        self.assertContains(self.client.get(reverse('cooking:receipt-detail', args=[receipt.uuid])), 'Rozpakuj zakupy (1)')

        scan = self.client.post(url, data=json.dumps({'barcode': self.yogurt.barcode}), content_type='application/json').json()
        self.assertEqual((scan['status'], scan['product_id']), ('date', self.yogurt.pk))
        dated = self.client.post(url, data=json.dumps({
            'action': 'date', 'product_id': self.yogurt.pk, 'expires_on': self.in_days(14).isoformat(),
        }), content_type='application/json').json()
        self.assertEqual(dated['status'], 'dated')
        self.assertEqual(dated['remaining'], [])
        self.assertEqual(self.yogurt.batches.get().expires_on, self.in_days(14))

    def test_category_switch(self):
        from .models import PantryCategory

        bread = PantryCategory.objects.get(code='pieczywo')
        self.client.post(reverse('cooking:pantry-category-edit', args=[bread.pk]),
                         {'name': bread.name, 'group': bread.group, 'requires_expiry': 'on'})
        bread.refresh_from_db()
        self.assertTrue(bread.requires_expiry)
        self.assertTrue(PantryCategory.objects.get(code='nabial').requires_expiry)
