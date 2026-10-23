"""Oceny produktów spiżarni: zapis z panelu, widoczność w wierszu i kafelku, łączenie duplikatów."""
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import PantryProduct, PantryRating
from .services.pantry_merge import merge_products
from .templatetags.pantry_extras import stars


class PantryRatingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='ola', first_name='Ola', password='pass12345')
        self.other = User.objects.create_user(username='kuba', first_name='Kuba', password='pass12345')
        self.client.force_login(self.user)
        self.chips = PantryProduct.objects.create(
            created_by=self.user, name='Chipsy paprykowe', barcode='5900000000222',
            current_quantity=Decimal('1'), current_package_count=1,
        )
        self.url = reverse('cooking:pantry-rating', args=[self.chips.pk])

    def _flags(self, response, product):
        match = re.search(r'data-pantry-row="%d"\s+data-flags="([^"]*)"' % product.pk, response.content.decode())
        return match.group(1).split()

    def test_rating_is_saved_and_updated_per_user(self):
        response = self.client.post(self.url, {'stars': '4', 'note': '  za słone  '})
        self.assertRedirects(response, reverse('cooking:pantry') + f'#product-{self.chips.pk}', fetch_redirect_response=False)
        rating = PantryRating.objects.get(product=self.chips, user=self.user)
        self.assertEqual((rating.stars, rating.note), (4, 'za słone'))

        self.client.post(self.url, {'stars': '2', 'note': ''})
        self.assertEqual(PantryRating.objects.filter(product=self.chips).count(), 1)
        rating.refresh_from_db()
        self.assertEqual((rating.stars, rating.note), (2, ''))

    def test_invalid_stars_are_rejected(self):
        for value in ['0', '6', 'abc', '']:
            response = self.client.post(self.url, {'stars': value}, follow=True)
            self.assertContains(response, 'Wybierz od 1 do 5 gwiazdek.')
        self.assertFalse(PantryRating.objects.exists())

    def test_remove_own_rating_only(self):
        PantryRating.objects.create(product=self.chips, user=self.user, stars=5)
        PantryRating.objects.create(product=self.chips, user=self.other, stars=1)
        self.client.post(self.url, {'remove': '1'})
        self.assertEqual(list(PantryRating.objects.values_list('user__username', flat=True)), ['kuba'])

    def test_requires_login(self):
        self.client.logout()
        self.client.post(self.url, {'stars': '5'})
        self.assertFalse(PantryRating.objects.exists())

    def test_list_shows_average_and_household_ratings(self):
        PantryRating.objects.create(product=self.chips, user=self.user, stars=5, note='najlepsze')
        PantryRating.objects.create(product=self.chips, user=self.other, stars=4)
        page = self.client.get(reverse('cooking:pantry'))
        html = page.content.decode()
        # Wiersz: średnia (format pl) i klucz sortowania.
        self.assertRegex(html, r'id="pantry-row-%d"[^>]*data-rating="4.5"' % self.chips.pk)
        self.assertIn('★</span> 4,5', html)
        # Kafelek: oceny domowników z notatką, własna jako „Ty”, wybrane gwiazdki.
        self.assertContains(page, 'id="rating-%d"' % self.chips.pk)
        self.assertContains(page, 'najlepsze')
        self.assertContains(page, '<strong>Ty</strong>', html=True)
        self.assertContains(page, '<strong>Kuba</strong>', html=True)
        self.assertContains(page, '2 oceny')
        self.assertEqual(re.findall(r'id="rate-%d-(\d)"[^>]*aria-pressed="true"' % self.chips.pk, html), ['5'])

    def test_empty_unrated_product_prompts_for_rating(self):
        page = self.client.get(reverse('cooking:pantry'))
        self.assertNotIn('rate', self._flags(page, self.chips))

        # Zużycie ostatniego opakowania „−” w wierszu → wiersz proponuje „Oceń”.
        self.client.post(reverse('cooking:pantry-movement', args=[self.chips.pk]), {
            'movement_type': 'consume', 'package_count': '1',
        })
        page = self.client.get(reverse('cooking:pantry'))
        self.assertIn('rate', self._flags(page, self.chips))
        self.assertContains(page, 'href="#rating-%d"' % self.chips.pk)
        self.assertContains(page, 'Skończył się. Jak wypadł?')

        # Ocena innego domownika nie zwalnia z pytania - po własnej ocenie znika.
        PantryRating.objects.create(product=self.chips, user=self.other, stars=3)
        page = self.client.get(reverse('cooking:pantry'))
        self.assertIn('rate', self._flags(page, self.chips))
        self.client.post(self.url, {'stars': '3'})
        page = self.client.get(reverse('cooking:pantry'))
        self.assertNotIn('rate', self._flags(page, self.chips))
        self.assertNotContains(page, 'href="#rating-%d"' % self.chips.pk)

    def test_merge_moves_ratings_and_keeps_newer(self):
        duplicate = PantryProduct.objects.create(created_by=self.user, name='Chipsy z paragonu')
        older = PantryRating.objects.create(product=self.chips, user=self.user, stars=2)
        PantryRating.objects.filter(pk=older.pk).update(updated_at=timezone.now() - timedelta(days=5))
        PantryRating.objects.create(product=duplicate, user=self.user, stars=5, note='jednak dobre')
        PantryRating.objects.create(product=duplicate, user=self.other, stars=4)

        merge_products(duplicate, self.chips)

        ratings = {r.user.username: (r.stars, r.note) for r in PantryRating.objects.filter(product=self.chips)}
        self.assertEqual(ratings, {'ola': (5, 'jednak dobre'), 'kuba': (4, '')})
        self.assertEqual(PantryRating.objects.count(), 2)

    def test_stars_filter(self):
        self.assertEqual(stars(4), '★★★★☆')
        self.assertEqual(stars(Decimal('3.5')), '★★★★☆')
        self.assertEqual(stars(Decimal('3.4')), '★★★☆☆')
        self.assertEqual(stars(None), '')
