"""Firma (marka) produktu spiżarni: „Masło” Łaciate i „Masło” Mlekovita obok siebie."""
import json
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from .models import PantryProduct, ProductCatalogEntry, Receipt, ShoppingList
from .services.pantry_merge import find_product_by_name, merge_products
from .services.pantry_names import split_label
from .services.product_catalog import remember_household_product
from .services.receipts import ACTION_ADD, LineDecision, apply_ocr_text, confirm_receipt, scan_receipt_barcode
from .services.recipe_pantry import pantry_index, resolve_pantry_product
from .tests_receipts import LIDL_E_RECEIPT_OCR


class PantryBrandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='ola', password='pass12345')
        self.client.force_login(self.user)
        self.laciate = PantryProduct.objects.create(
            created_by=self.user, name='Masło', brand='Łaciate', current_quantity=Decimal('1'),
        )

    def _add(self, **data):
        payload = {'name': 'Masło', 'unit': 'szt', 'quantity_per_scan': '1', 'current_quantity': '0',
                   'minimum_quantity': '0', 'restock_lead_days': '3'}
        payload.update(data)
        return self.client.post(reverse('cooking:add-pantry-product'), payload)

    # --- model i formularze -------------------------------------------------

    def test_same_name_with_other_brand_is_a_separate_product(self):
        response = self._add(brand='Mlekovita')
        self.assertRedirects(response, reverse('cooking:pantry'), fetch_redirect_response=False)
        self.assertEqual(
            sorted(PantryProduct.objects.values_list('brand', flat=True)), ['Mlekovita', 'Łaciate'],
        )
        self.assertEqual(PantryProduct.objects.get(brand='Mlekovita').label, 'Masło (Mlekovita)')

    def test_same_name_and_brand_ignoring_case_is_rejected(self):
        response = self._add(name='MASŁO', brand='łaciate')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'jest już produkt „MASŁO (łaciate)”')
        self.assertEqual(PantryProduct.objects.count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PantryProduct.objects.create(name='masło', brand='ŁACIATE')

    def test_brand_with_parentheses_is_rejected(self):
        self._add(brand='Mlekovita (Polska)')
        self.assertEqual(PantryProduct.objects.count(), 1)

    def test_edit_changes_brand_and_refuses_a_taken_pair(self):
        other = PantryProduct.objects.create(created_by=self.user, name='Masło')
        url = reverse('cooking:edit-pantry-product', args=[other.pk])
        response = self.client.post(url, {'name': 'Masło', 'brand': 'Łaciate'})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'jest już produkt „Masło (Łaciate)”', status_code=400)

        response = self.client.post(url, {'name': 'Masło', 'brand': 'Mlekovita'})
        self.assertEqual(response.status_code, 302)
        other.refresh_from_db()
        self.assertEqual(other.brand, 'Mlekovita')

    def test_pantry_shows_brand_and_searches_by_it(self):
        response = self.client.get(reverse('cooking:pantry'))
        self.assertContains(response, '<span class="pt-brand">Łaciate</span>')
        self.assertContains(response, '<span class="pantry-product-brand">Łaciate</span>')
        response = self.client.get(reverse('cooking:pantry'), {'q': 'łaciate'})
        self.assertContains(response, f'data-pantry-row="{self.laciate.pk}"')

    # --- skaner ------------------------------------------------------------

    def _register(self, **data):
        payload = {'barcode': '5900820000011', 'scan_id': str(uuid4()), 'name': 'Masło',
                   'quantity_per_scan': '1', 'unit': 'szt', 'count': 1}
        payload.update(data)
        return self.client.post(reverse('cooking:pantry-barcode-register'), data=json.dumps(payload),
                                content_type='application/json')

    def test_scanner_creates_second_brand_and_remembers_it(self):
        response = self._register(brand='Mlekovita')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['product']['label'], 'Masło (Mlekovita)')
        self.laciate.refresh_from_db()
        self.assertEqual(self.laciate.barcode, '')
        entry = ProductCatalogEntry.objects.get(source=ProductCatalogEntry.SOURCE_HOUSEHOLD,
                                                lookup_barcode='5900820000011')
        self.assertEqual(entry.brand, 'Mlekovita')

        lookup = self.client.get(reverse('cooking:pantry-barcode-lookup'), {'barcode': '5900820000011'})
        self.assertEqual(lookup.json()['catalog']['brand'], 'Mlekovita')

    def test_scanner_gives_brand_to_product_without_brand_and_barcode(self):
        plain = PantryProduct.objects.create(created_by=self.user, name='Jogurt')
        response = self._register(name='Jogurt', brand='Pilos')
        self.assertEqual(response.status_code, 200)
        plain.refresh_from_db()
        self.assertEqual((plain.brand, plain.barcode), ('Pilos', '5900820000011'))

    # --- wyszukiwanie po nazwie ---------------------------------------------

    def test_find_by_name_and_label(self):
        mlekovita = PantryProduct.objects.create(name='Masło', brand='Mlekovita', current_quantity=Decimal('3'))
        self.assertEqual(split_label('Masło (Łaciate)'), ('Masło', 'Łaciate'))
        self.assertEqual(find_product_by_name('maslo (laciate)'), self.laciate)
        self.assertEqual(find_product_by_name('Masło (Mlekovita)'), mlekovita)
        self.assertIsNone(find_product_by_name('Masło', brand='Président'))
        # Sama nazwa: najwięcej na stanie.
        self.assertEqual(find_product_by_name('Masło'), mlekovita)
        self.assertEqual(resolve_pantry_product('masło'), mlekovita)
        names = [entry['name'] for entry in pantry_index()]
        self.assertIn('Masło', names)
        self.assertIn('Masło (Łaciate)', names)
        self.assertIn('Masło (Mlekovita)', names)

    def test_shopping_list_keeps_brands_apart(self):
        mlekovita = PantryProduct.objects.create(name='Masło', brand='Mlekovita')
        shopping_list = ShoppingList.objects.create(created_by=self.user, title='Sobota')
        for product in [self.laciate, mlekovita]:
            self.client.post(reverse('cooking:add-pantry-product-to-shopping-list', args=[shopping_list.id, product.id]))
        items = {item.name: item.pantry_product_id for item in shopping_list.items.all()}
        self.assertEqual(items, {'Masło (Łaciate)': self.laciate.pk, 'Masło (Mlekovita)': mlekovita.pk})

    def test_merge_gives_brand_to_product_without_brand(self):
        plain = PantryProduct.objects.create(name='Ser żółty', barcode='5900000000999')
        branded = PantryProduct.objects.create(name='Ser z paragonu', brand='Hochland')
        merge_products(branded, plain)
        plain.refresh_from_db()
        self.assertEqual(plain.brand, 'Hochland')

    def test_remember_household_product_stores_brand(self):
        entry = remember_household_product('123', name='Masło', brand='Łaciate', category='',
                                           unit='szt', quantity_per_scan=Decimal('1'))
        self.assertEqual(entry.brand, 'Łaciate')


class ReceiptBrandTests(TestCase):
    """Paragon: inna firma pod tą samą nazwą nie łączy się sama z produktem z kodem."""

    def setUp(self):
        self.user = User.objects.create_user(username='paragon', password='pass12345')
        self.owner = PantryProduct.objects.create(
            created_by=self.user, name='Pomidory krojone', brand='Mutti', barcode='8005110070600',
        )
        self.receipt = apply_ocr_text(Receipt.objects.create(), LIDL_E_RECEIPT_OCR)
        self.line = self.receipt.lines.get(name='Mutti Pomidory polpa')

    def _confirm(self, target):
        confirm_receipt(self.receipt, {self.line.pk: LineDecision(action=ACTION_ADD, target=target, quantity=Decimal('1'))},
                        user=self.user)

    def test_label_typed_at_line_creates_product_with_brand(self):
        self._confirm('Pomidory krojone (Pudliszki)')
        created = PantryProduct.objects.get(brand='Pudliszki')
        self.assertEqual(created.name, 'Pomidory krojone')
        self.assertEqual(PantryProduct.objects.count(), 2)

    def test_other_brand_asks_instead_of_merging(self):
        self._confirm('Pomidory krojone (Pudliszki)')
        result = scan_receipt_barcode(self.receipt, self.owner.barcode)
        self.assertEqual(result['status'], 'merge')
        self.assertEqual(result['product'], 'Pomidory krojone (Mutti)')
        self.assertEqual(PantryProduct.objects.count(), 2)

    def test_same_name_without_brand_merges(self):
        self._confirm('Passata')
        PantryProduct.objects.filter(name='Passata').update(name='Pomidory krojone ', brand='')
        result = scan_receipt_barcode(self.receipt, self.owner.barcode)
        self.assertEqual(result['status'], 'merged')
        self.assertEqual(PantryProduct.objects.count(), 1)
