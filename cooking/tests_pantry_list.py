"""Lista spiżarni „na co dzień”: wiersze z data-*, pasek „dziś”, filtry stanu po stronie serwera."""
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import PantryBatch, PantryProduct, ReceiptAlias
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

    def test_change_batch_expiry_recomputes_eat_first(self):
        batch = self.yogurt.batches.get()
        url = reverse('cooking:pantry-batch-expiry', args=[batch.pk])
        page = self.client.get(reverse('cooking:pantry'))
        self.assertContains(page, url)
        self.assertEqual(page.context['today_counts']['expiring'], 1)

        response = self.client.post(url, {'expires_on': self.in_days(20).isoformat(), 'next': reverse('cooking:pantry')}, follow=True)
        batch.refresh_from_db()
        self.assertEqual(batch.expires_on, self.in_days(20))
        self.assertEqual(batch.quantity, Decimal('400'))
        self.assertContains(response, 'termin zmieniony')
        self.assertEqual(response.context['today_counts']['expiring'], 0)
        self.assertNotIn('expiring', self._rows(response)[self.yogurt.pk].split())
        self.assertContains(response, 'do %s' % self.in_days(20).strftime('%d.%m'))

    def test_change_batch_expiry_allows_past_date_and_rejects_bad_input(self):
        batch = self.yogurt.batches.get()
        url = reverse('cooking:pantry-batch-expiry', args=[batch.pk])
        for bad in ('', 'jutro', self.in_days(-400).isoformat(), self.in_days(4000).isoformat()):
            self.client.post(url, {'expires_on': bad})
            batch.refresh_from_db()
            self.assertEqual(batch.expires_on, self.in_days(1), bad)

        # Poprawka na termin, który już minął - produkt trafia do „po terminie”.
        self.client.post(url, {'expires_on': self.in_days(-3).isoformat()})
        batch.refresh_from_db()
        self.assertEqual(batch.expires_on, self.in_days(-3))
        page = self.client.get(reverse('cooking:pantry'))
        self.assertIn('expired', self._rows(page)[self.yogurt.pk].split())

    def test_change_batch_expiry_merges_with_same_date(self):
        self.yogurt.current_quantity = Decimal('800')
        self.yogurt.save(update_fields=['current_quantity'])
        later = add_batch(self.yogurt, Decimal('400'), self.in_days(9))
        first = self.yogurt.batches.get(expires_on=self.in_days(1))
        self.client.post(reverse('cooking:pantry-batch-expiry', args=[first.pk]), {'expires_on': self.in_days(9).isoformat()})
        self.assertEqual(list(self.yogurt.batches.values_list('pk', 'quantity')), [(later.pk, Decimal('800'))])

    def test_change_missing_batch_shows_message(self):
        response = self.client.post(
            reverse('cooking:pantry-batch-expiry', args=[999999]), {'expires_on': self.in_days(5).isoformat()}, follow=True,
        )
        self.assertContains(response, 'Tej partii już nie ma')
        self.assertEqual(PantryBatch.objects.count(), 1)

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
