"""Lista spiżarni „na co dzień”: wiersze z data-*, pasek „dziś”, filtry stanu po stronie serwera."""
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import PantryProduct, ReceiptAlias
from .services.pantry_batches import add_batch


class PantryListTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='lista', password='pass12345')
        self.client.force_login(self.user)
        today = timezone.localdate()
        self.in_days = lambda days: today + timedelta(days=days)
        self.yogurt = PantryProduct.objects.create(
            created_by=self.user, name='Jogurt Pilos', category='Nabiał', barcode='5900000000999',
            unit=PantryProduct.UNIT_GRAM, quantity_per_scan=Decimal('400'), current_quantity=Decimal('400'),
        )
        add_batch(self.yogurt, Decimal('400'), self.in_days(1))
        self.milk = PantryProduct.objects.create(
            created_by=self.user, name='Mleko', category='Nabiał', current_quantity=Decimal('2'),
        )
        self.rice = PantryProduct.objects.create(
            created_by=self.user, name='Ryż', category='Produkty suche', barcode='5900000000111',
            current_quantity=Decimal('3'),
        )

    def _rows(self, response):
        """Słownik id produktu → atrybut data-flags wiersza listy."""
        html = response.content.decode()
        return {int(pk): flags for pk, flags in re.findall(r'data-pantry-row="(\d+)"\s+data-flags="([^"]*)"', html)}

    def test_rows_carry_flags_for_client_filters(self):
        page = self.client.get(reverse('cooking:pantry'))
        rows = self._rows(page)
        self.assertEqual(set(rows), {self.yogurt.pk, self.milk.pk, self.rice.pk})
        self.assertIn('expiring', rows[self.yogurt.pk].split())
        self.assertIn('undated', rows[self.milk.pk].split())
        self.assertIn('nobarcode', rows[self.milk.pk].split())
        self.assertNotIn('nobarcode', rows[self.rice.pk].split())
        self.assertContains(page, 'termin: jutro')
        self.assertContains(page, 'id="pantry-row-%d"' % self.milk.pk)
        # Produkt bez terminu ma w wierszu krótki wybór daty.
        self.assertContains(page, 'id="pt-date-%d"' % self.milk.pk)
        self.assertNotContains(page, 'id="pt-date-%d"' % self.yogurt.pk)

    def test_today_bar_counts_and_links(self):
        page = self.client.get(reverse('cooking:pantry'))
        self.assertContains(page, 'id="pantry-today"')
        self.assertEqual(page.context['today_counts']['expiring'], 1)
        self.assertEqual(page.context['today_counts']['undated'], 1)
        self.assertEqual(page.context['today_counts']['nobarcode'], 1)
        self.assertContains(page, 'Zjedz najpierw')
        self.assertContains(page, 'Uzupełnij termin')

        self.client.post(reverse('cooking:pantry-expiry', args=[self.milk.pk]), {'expires_on': self.in_days(30).isoformat()})
        page = self.client.get(reverse('cooking:pantry'))
        self.assertEqual(page.context['today_counts']['undated'], 0)
        self.assertNotContains(page, 'Uzupełnij termin')

    def test_server_side_status_filters(self):
        url = reverse('cooking:pantry')
        self.assertEqual(set(self._rows(self.client.get(url, {'status': 'expiring'}))), {self.yogurt.pk})
        self.assertEqual(set(self._rows(self.client.get(url, {'status': 'undated'}))), {self.milk.pk})
        self.assertEqual(set(self._rows(self.client.get(url, {'status': 'nobarcode'}))), {self.milk.pk})

    def test_search_text_includes_receipt_alias(self):
        ReceiptAlias.objects.create(normalized_text='ryz bial 1kg', product=self.rice)
        page = self.client.get(reverse('cooking:pantry'))
        self.assertRegex(page.content.decode(), r'id="pantry-row-%d"[^>]*data-search="[^"]*ryz bial 1kg' % self.rice.pk)

    def test_groups_view_keeps_accordion(self):
        page = self.client.get(reverse('cooking:pantry'), {'widok': 'grupy'})
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'data-pantry-list')
        self.assertContains(page, 'Wróć do listy')
