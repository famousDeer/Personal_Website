"""Skanowanie w trybie zakupów: "Kupiono" dopisuje albo odhacza pozycję listy."""
import json
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import PantryMovement, PantryProduct, ProductGroup, ShoppingList, ShoppingListItem

User = get_user_model()


class ScannerPurchaseSyncTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='skaner', password='pass12345')
        self.client.force_login(self.user)
        self.butter = PantryProduct.objects.create(
            created_by=self.user, name='Masło extra', category='Nabiał', barcode='5900000000201',
            unit=PantryProduct.UNIT_GRAM, quantity_per_scan=Decimal('200.00'),
        )
        self.group = ProductGroup.objects.create(name='Jogurt naturalny', category='Nabiał')
        self.pilos = PantryProduct.objects.create(
            created_by=self.user, name='Jogurt Pilos', category='Nabiał', barcode='5909991100001',
            unit=PantryProduct.UNIT_GRAM, quantity_per_scan=Decimal('400.00'), group=self.group,
        )
        self.bakoma = PantryProduct.objects.create(
            created_by=self.user, name='Jogurt Bakoma', category='Nabiał', barcode='5909991100003',
            unit=PantryProduct.UNIT_GRAM, quantity_per_scan=Decimal('400.00'), group=self.group,
        )
        # Pilos kupowany ostatnio - bez wskazania marki zapas trafiłby do niego.
        PantryMovement.objects.create(
            product=self.pilos, movement_type=PantryMovement.PURCHASE, quantity=Decimal('400.00'),
            occurred_on=timezone.localdate(),
        )
        self.list = ShoppingList.objects.create(created_by=self.user, title='Sobota')

    def op(self, op_type, **fields):
        return {'op_id': str(uuid4()), 'type': op_type, 'at': timezone.now().isoformat(), **fields}

    def sync(self, *ops):
        response = self.client.post(
            reverse('cooking:shopping-api-sync'), data=json.dumps({'ops': list(ops)}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        data = response.json()
        self.assertEqual([result['status'] for result in data['results']], ['applied'] * len(ops), data['results'])
        return data

    def test_snapshot_has_product_link_and_single_purchase(self):
        ShoppingListItem.objects.create(
            shopping_list=self.list, name='Masło extra', pantry_product=self.butter, unit='szt',
        )
        snapshot = self.client.get(reverse('cooking:shopping-api-snapshot')).json()['snapshot']
        butter = next(product for product in snapshot['products'] if product['id'] == self.butter.pk)
        self.assertEqual((butter['buy_quantity'], butter['buy_unit']), ('1.00', 'szt'))
        bakoma = next(product for product in snapshot['products'] if product['id'] == self.bakoma.pk)
        # Do listy jednym przyciskiem idzie grupa, a zakup ze skanera - ta marka.
        self.assertEqual(bakoma['add_name'], 'Jogurt naturalny')
        self.assertEqual((bakoma['buy_quantity'], bakoma['buy_unit']), ('1.00', 'szt'))
        self.assertEqual(snapshot['lists'][0]['items'][0]['product'], self.butter.pk)

    def test_scanned_product_missing_from_list_is_added_as_bought(self):
        item_uuid = str(uuid4())
        self.sync(
            self.op('item.add', list=str(self.list.uuid), data={
                'uuid': item_uuid, 'name': 'Masło extra', 'quantity': '1', 'unit': 'szt',
                'category': 'Nabiał', 'product': self.butter.pk,
            }),
            self.op('item.set_purchased', item=item_uuid, purchased=True, product=self.butter.pk),
        )
        item = ShoppingListItem.objects.get(uuid=item_uuid)
        self.assertTrue(item.is_purchased)
        self.assertEqual(item.pantry_product, self.butter)
        self.butter.refresh_from_db()
        self.assertEqual(self.butter.current_quantity, Decimal('200.00'))
        self.assertEqual(self.butter.current_package_count, 1)

    def test_item_add_prefers_explicit_product_over_name(self):
        item_uuid = str(uuid4())
        self.sync(self.op('item.add', list=str(self.list.uuid), data={
            'uuid': item_uuid, 'name': 'Masełko', 'quantity': '1', 'unit': 'szt', 'product': self.butter.pk,
        }))
        self.assertEqual(ShoppingListItem.objects.get(uuid=item_uuid).pantry_product, self.butter)

    def test_unknown_product_falls_back_to_name(self):
        item_uuid = str(uuid4())
        self.sync(self.op('item.add', list=str(self.list.uuid), data={
            'uuid': item_uuid, 'name': 'Masło extra', 'quantity': '1', 'unit': 'szt', 'product': 'xyz',
        }))
        self.assertEqual(ShoppingListItem.objects.get(uuid=item_uuid).pantry_product, self.butter)

    def test_group_item_checked_by_scan_restocks_scanned_brand(self):
        item = ShoppingListItem.objects.create(
            shopping_list=self.list, name='Jogurt naturalny', pantry_group=self.group,
            quantity=Decimal('1.00'), unit='szt', category='Nabiał',
        )
        self.sync(self.op('item.set_purchased', item=str(item.uuid), purchased=True, product=self.bakoma.pk))
        item.refresh_from_db()
        self.assertEqual(item.pantry_product, self.bakoma)
        self.bakoma.refresh_from_db()
        self.pilos.refresh_from_db()
        self.assertEqual(self.bakoma.current_quantity, Decimal('400.00'))
        self.assertEqual(self.pilos.current_quantity, Decimal('0.00'))

    def test_scan_does_not_relink_item_of_another_product(self):
        item = ShoppingListItem.objects.create(
            shopping_list=self.list, name='Masło extra', pantry_product=self.butter,
            quantity=Decimal('1.00'), unit='szt',
        )
        self.sync(self.op('item.set_purchased', item=str(item.uuid), purchased=True, product=self.bakoma.pk))
        item.refresh_from_db()
        self.assertEqual(item.pantry_product, self.butter)
        self.butter.refresh_from_db()
        self.assertEqual(self.butter.current_quantity, Decimal('200.00'))

    def test_brand_outside_group_is_ignored(self):
        item = ShoppingListItem.objects.create(
            shopping_list=self.list, name='Jogurt naturalny', pantry_group=self.group,
            quantity=Decimal('1.00'), unit='szt',
        )
        self.sync(self.op('item.set_purchased', item=str(item.uuid), purchased=True, product=self.butter.pk))
        item.refresh_from_db()
        self.assertEqual(item.pantry_product, self.pilos)  # ostatnio kupowana marka, jak dotąd

    def test_another_one_in_basket_increases_pantry(self):
        item = ShoppingListItem.objects.create(
            shopping_list=self.list, name='Masło extra', pantry_product=self.butter,
            quantity=Decimal('1.00'), unit='szt',
        )
        self.sync(self.op('item.set_purchased', item=str(item.uuid), purchased=True, product=self.butter.pk))
        self.sync(self.op('item.set_quantity', item=str(item.uuid), quantity='2', product=self.butter.pk))
        self.butter.refresh_from_db()
        self.assertEqual(self.butter.current_quantity, Decimal('400.00'))
        self.assertEqual(PantryMovement.objects.filter(product=self.butter).count(), 1)
