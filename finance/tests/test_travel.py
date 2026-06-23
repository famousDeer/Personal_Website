from django.test import TestCase
from django.test import override_settings
from django.urls import reverse
from django.contrib.auth import get_user_model
from finance.models import TravelDestinations
from datetime import date, timedelta
from decimal import Decimal
from django_countries.fields import Country
from unittest.mock import patch

User = get_user_model()

@override_settings(TRAVEL_GEOCODING_ENABLED=False)
class TravelDestinationsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.user2 = User.objects.create_user(username='otheruser', password='otherpass')
        self.client.login(username='testuser', password='testpass')
        self.travel1 = TravelDestinations.objects.create(
            user=self.user,
            country='PL',
            city='Warszawa',
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 10),
            budget=1000.00
        )
        self.travel2 = TravelDestinations.objects.create(
            user=self.user,
            country='DE',
            city='Berlin',
            start_date=date(2025, 2, 1),
            end_date=date(2025, 2, 5),
            budget=500.00
        )
        self.travel_other = TravelDestinations.objects.create(
            user=self.user2,
            country='FR',
            city='Paris',
            start_date=date(2025, 3, 1),
            end_date=date(2025, 3, 5),
            budget=800.00
        )

    def _listed_destinations(self, response):
        return [
            destination
            for group in response.context['location_groups'].object_list
            for destination in group['destinations']
        ]

    def test_travel_list_view_shows_only_user_travels(self):
        url = reverse('finance:travels')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        destinations = self._listed_destinations(response)
        self.assertIn(self.travel1, destinations)
        self.assertIn(self.travel2, destinations)
        self.assertNotIn(self.travel_other, destinations)

    def test_travel_country_filter(self):
        url = reverse('finance:travels')
        response = self.client.get(url, {'country': 'PL'})
        destinations = self._listed_destinations(response)
        self.assertIn(self.travel1, destinations)
        self.assertNotIn(self.travel2, destinations)

    def test_travel_type_filter(self):
        self.travel2.travel_type = TravelDestinations.BUSINESS
        self.travel2.save(update_fields=['travel_type'])

        url = reverse('finance:travels')
        response = self.client.get(url, {'travel_type': TravelDestinations.BUSINESS})
        destinations = self._listed_destinations(response)

        self.assertIn(self.travel2, destinations)
        self.assertNotIn(self.travel1, destinations)

    def test_travel_pagination(self):
        # Create 12 travels for pagination
        for i in range(12):
            TravelDestinations.objects.create(
                user=self.user,
                country='IT',
                city=f'City{i}',
                start_date=date(2025, 4, 1) + timedelta(days=i),
                end_date=date(2025, 4, 2) + timedelta(days=i),
                budget=100 + i
            )
        url = reverse('finance:travels')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['location_groups'].paginator.num_pages >= 2)
        # Test page 2
        response2 = self.client.get(url, {'page': 2})
        self.assertEqual(response2.status_code, 200)

    def test_add_travel_view_and_form(self):
        url = reverse('finance:add_travel')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # Post valid data
        data = {
            'country': 'ES',
            'travel_type': TravelDestinations.LEISURE,
            'city': 'Madryt',
            'start_date': '2025-05-01',
            'end_date': '2025-05-10',
            'budget': 1200.00
        }
        response = self.client.post(url, data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(TravelDestinations.objects.filter(user=self.user, country='ES', city='Madryt').exists())

    @override_settings(TRAVEL_GEOCODING_ENABLED=True)
    def test_add_travel_autofills_coordinates_without_form_fields(self):
        url = reverse('finance:add_travel')
        response = self.client.get(url)
        form = response.context['form']
        self.assertNotIn('latitude', form.fields)
        self.assertNotIn('longitude', form.fields)
        self.assertIn('travel_type', form.fields)

        def set_coordinates(destination, force=False):
            destination.latitude = Decimal('40.416775')
            destination.longitude = Decimal('-3.703790')
            return True

        data = {
            'country': 'ES',
            'travel_type': TravelDestinations.LEISURE,
            'city': 'Madryt',
            'start_date': '2025-05-01',
            'end_date': '2025-05-10',
            'budget': 1200.00
        }
        with patch('finance.views.populate_destination_coordinates', side_effect=set_coordinates):
            response = self.client.post(url, data, follow=True)

        self.assertEqual(response.status_code, 200)
        travel = TravelDestinations.objects.get(user=self.user, country='ES', city='Madryt')
        self.assertEqual(travel.latitude, Decimal('40.416775'))
        self.assertEqual(travel.longitude, Decimal('-3.703790'))

    def test_add_travel_reuses_coordinates_for_existing_city_country(self):
        self.travel1.latitude = Decimal('52.229676')
        self.travel1.longitude = Decimal('21.012229')
        self.travel1.save(update_fields=['latitude', 'longitude'])

        data = {
            'country': 'PL',
            'travel_type': TravelDestinations.BUSINESS,
            'city': 'Warszawa',
            'start_date': '2025-06-01',
            'end_date': '2025-06-03',
            'budget': 300.00
        }
        url = reverse('finance:add_travel')
        with patch('finance.views.populate_destination_coordinates') as geocode_mock:
            response = self.client.post(url, data, follow=True)

        self.assertEqual(response.status_code, 200)
        geocode_mock.assert_not_called()
        travel = TravelDestinations.objects.get(user=self.user, city='Warszawa', start_date=date(2025, 6, 1))
        self.assertEqual(travel.latitude, Decimal('52.229676'))
        self.assertEqual(travel.longitude, Decimal('21.012229'))
        self.assertEqual(travel.travel_type, TravelDestinations.BUSINESS)

    def test_edit_travel_view_prefills_form(self):
        url = reverse('finance:edit_travel', args=[self.travel1.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Warszawa')
        self.assertContains(response, '01.01.2025')
        self.assertContains(response, '10.01.2025')
        # Post update
        data = {
            'country': 'PL',
            'travel_type': TravelDestinations.LEISURE,
            'city': 'Kraków',
            'start_date': '2025-01-01',
            'end_date': '2025-01-15',
            'budget': 1100.00
        }
        response = self.client.post(url, data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.travel1.refresh_from_db()
        self.assertEqual(self.travel1.city, 'Kraków')
        self.assertEqual(self.travel1.end_date, date(2025, 1, 15))

    @override_settings(TRAVEL_GEOCODING_ENABLED=True)
    def test_edit_travel_refreshes_coordinates_when_city_changes(self):
        self.travel1.latitude = Decimal('52.229676')
        self.travel1.longitude = Decimal('21.012229')
        self.travel1.save(update_fields=['latitude', 'longitude'])

        def set_coordinates(destination, force=False):
            destination.latitude = Decimal('50.064650')
            destination.longitude = Decimal('19.944980')
            return True

        url = reverse('finance:edit_travel', args=[self.travel1.id])
        data = {
            'country': 'PL',
            'travel_type': TravelDestinations.LEISURE,
            'city': 'Kraków',
            'start_date': '2025-01-01',
            'end_date': '2025-01-10',
            'budget': 1000.00
        }
        with patch('finance.views.populate_destination_coordinates', side_effect=set_coordinates):
            response = self.client.post(url, data, follow=True)

        self.assertEqual(response.status_code, 200)
        self.travel1.refresh_from_db()
        self.assertEqual(self.travel1.latitude, Decimal('50.064650'))
        self.assertEqual(self.travel1.longitude, Decimal('19.944980'))

    def test_delete_travel_view(self):
        url = reverse('finance:delete_travel', args=[self.travel2.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # Confirm delete
        response = self.client.post(url, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TravelDestinations.objects.filter(id=self.travel2.id).exists())

    def test_permissions_user_cannot_edit_or_delete_others_travel(self):
        url_edit = reverse('finance:edit_travel', args=[self.travel_other.id])
        url_delete = reverse('finance:delete_travel', args=[self.travel_other.id])
        response = self.client.get(url_edit)
        self.assertEqual(response.status_code, 404)
        response = self.client.get(url_delete)
        self.assertEqual(response.status_code, 404)
        # Try POST as well
        response = self.client.post(url_edit, {'country': 'FR', 'travel_type': TravelDestinations.LEISURE, 'city': 'Nice', 'start_date': '2025-03-01', 'end_date': '2025-03-05'})
        self.assertEqual(response.status_code, 404)
        response = self.client.post(url_delete)
        self.assertEqual(response.status_code, 404)

    def test_travel_list_country_names_and_flags(self):
        url = reverse('finance:travels')
        response = self.client.get(url)
        self.assertContains(response, Country('PL').name)
        self.assertContains(response, Country('DE').name)
        self.assertContains(response, 'img')  # flag icon

    def test_days_between_filter_in_template(self):
        url = reverse('finance:travels')
        response = self.client.get(url)
        self.assertContains(response, '10')  # travel1: 10 days
        self.assertContains(response, '5')   # travel2: 5 days

    def test_travel_map_points_context_uses_saved_coordinates(self):
        self.travel1.latitude = Decimal('52.229676')
        self.travel1.longitude = Decimal('21.012229')
        self.travel1.save(update_fields=['latitude', 'longitude'])

        url = reverse('finance:travels')
        response = self.client.get(url)
        points = response.context['travel_map_points']

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]['label'], 'Warszawa')
        self.assertEqual(points[0]['lat'], 52.229676)
        self.assertEqual(points[0]['lng'], 21.012229)

    def test_travel_map_groups_same_city_country_into_one_pin(self):
        self.travel1.latitude = Decimal('52.229676')
        self.travel1.longitude = Decimal('21.012229')
        self.travel1.save(update_fields=['latitude', 'longitude'])
        TravelDestinations.objects.create(
            user=self.user,
            country='PL',
            city='Warszawa',
            start_date=date(2025, 6, 1),
            end_date=date(2025, 6, 3),
            budget=300.00,
            travel_type=TravelDestinations.BUSINESS,
            latitude=Decimal('52.229676'),
            longitude=Decimal('21.012229'),
        )

        url = reverse('finance:travels')
        response = self.client.get(url, {'country': 'PL'})
        points = response.context['travel_map_points']
        groups = response.context['location_groups'].object_list

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]['tripCount'], 2)
        self.assertEqual(len(points[0]['intervals']), 2)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['trip_count'], 2)

    def test_delegation_year_stats_count_unique_days_and_percent(self):
        self.travel1.travel_type = TravelDestinations.BUSINESS
        self.travel1.start_date = date(2025, 1, 1)
        self.travel1.end_date = date(2025, 1, 10)
        self.travel1.save(update_fields=['travel_type', 'start_date', 'end_date'])
        TravelDestinations.objects.create(
            user=self.user,
            country='PL',
            city='Warszawa',
            start_date=date(2025, 1, 5),
            end_date=date(2025, 1, 12),
            budget=300.00,
            travel_type=TravelDestinations.BUSINESS,
        )

        url = reverse('finance:travels')
        response = self.client.get(url)
        stats = {item['year']: item for item in response.context['delegation_year_stats']}

        self.assertEqual(stats[2025]['days'], 12)
        self.assertEqual(stats[2025]['year_days'], 365)
        self.assertEqual(stats[2025]['percent'], Decimal('3.3'))
        self.assertEqual(response.context['travel_stats']['business_days'], 12)
        self.assertIn('<strong>2025</strong>', response.content.decode())

    def test_delegation_year_stats_current_year_uses_days_elapsed_until_today(self):
        self.travel1.travel_type = TravelDestinations.BUSINESS
        self.travel1.start_date = date(2025, 1, 5)
        self.travel1.end_date = date(2025, 1, 25)
        self.travel1.save(update_fields=['travel_type', 'start_date', 'end_date'])
        TravelDestinations.objects.create(
            user=self.user,
            country='PL',
            city='Warszawa',
            start_date=date(2025, 2, 1),
            end_date=date(2025, 2, 3),
            budget=300.00,
            travel_type=TravelDestinations.BUSINESS,
        )

        url = reverse('finance:travels')
        with patch('finance.views.timezone.localdate', return_value=date(2025, 1, 20)):
            response = self.client.get(url)
        stats = {item['year']: item for item in response.context['delegation_year_stats']}

        self.assertEqual(stats[2025]['days'], 16)
        self.assertEqual(stats[2025]['year_days'], 20)
        self.assertEqual(stats[2025]['percent'], Decimal('80.0'))
