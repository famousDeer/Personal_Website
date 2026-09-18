"""Zgodność mapy podróży z polityką użycia kafelków OpenStreetMap.

OSM blokuje aplikacje łamiące politykę, zwracając kafelek 403 "Access blocked".
Mapa złamała ją na trzy sposoby naraz: przestarzały adres z subdomenami `{s}`,
brak nagłówka Referer (przez SECURE_REFERRER_POLICY=same-origin) i niepełną
atrybucję. Te testy pilnują, żeby żaden z tych warunków nie wrócił.

Źródło wymagań: https://operations.osmfoundation.org/policies/tiles/
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from finance.models import TravelDestinations

User = get_user_model()

# Wartości akceptowane przez OSM - każda z nich wysyła Referer przy żądaniu
# na inny host. Celowo NIE ma tu 'same-origin' ani 'no-referrer'.
ALLOWED_REFERRER_POLICIES = {
    'no-referrer-when-downgrade',
    'origin',
    'origin-when-cross-origin',
    'strict-origin',
    'strict-origin-when-cross-origin',
    'unsafe-url',
}


@override_settings(TRAVEL_GEOCODING_ENABLED=False)
class TravelMapTilePolicyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='podrozny', password='haslo123')
        self.client.login(username='podrozny', password='haslo123')
        TravelDestinations.objects.create(
            user=self.user,
            country='PL',
            city='Warszawa',
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 10),
            budget=1000.00,
            latitude=52.2297,
            longitude=21.0122,
        )
        self.html = self.client.get(reverse('finance:travels')).content.decode()

    def test_uses_canonical_tile_url(self):
        self.assertIn('https://tile.openstreetmap.org/{z}/{x}/{y}.png', self.html)

    def test_does_not_use_retired_subdomain_form(self):
        # a./b./c.tile.openstreetmap.org jest wycofane i kończy się kafelkiem 403.
        self.assertNotIn('{s}.tile.openstreetmap.org', self.html)

    def test_does_not_use_plain_http_tiles(self):
        self.assertNotIn('http://tile.openstreetmap.org', self.html)

    def test_tiles_send_a_referrer(self):
        # Atrybut na elemencie ma pierwszeństwo przed polityką dokumentu, więc
        # kafelki niosą Referer nawet przy SECURE_REFERRER_POLICY=same-origin.
        self.assertIn('referrerPolicy', self.html)
        used = [policy for policy in ALLOWED_REFERRER_POLICIES if f"'{policy}'" in self.html]
        self.assertTrue(
            used,
            'Warstwa kafelków musi ustawiać referrerPolicy na wartość, która wysyła '
            f'Referer przy żądaniu cross-origin. Dozwolone: {sorted(ALLOWED_REFERRER_POLICIES)}',
        )

    def test_attribution_names_contributors_and_links_to_licence(self):
        self.assertIn('OpenStreetMap', self.html)
        self.assertIn('contributors', self.html)
        self.assertIn('https://www.openstreetmap.org/copyright', self.html)
