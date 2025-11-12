from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from finance.models import TravelDestinations
from datetime import date, timedelta
from django_countries.fields import Country

User = get_user_model()

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

    def test_travel_list_view_shows_only_user_travels(self):
        url = reverse('finance:travels')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        destinations = response.context['destinations'].object_list
        self.assertIn(self.travel1, destinations)
        self.assertIn(self.travel2, destinations)
        self.assertNotIn(self.travel_other, destinations)

    def test_travel_country_filter(self):
        url = reverse('finance:travels')
        response = self.client.get(url, {'country': 'PL'})
        destinations = response.context['destinations'].object_list
        self.assertIn(self.travel1, destinations)
        self.assertNotIn(self.travel2, destinations)

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
        self.assertTrue(response.context['destinations'].paginator.num_pages >= 2)
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
            'city': 'Madryt',
            'start_date': '2025-05-01',
            'end_date': '2025-05-10',
        }
        response = self.client.post(url, data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(TravelDestinations.objects.filter(user=self.user, country='ES', city='Madryt').exists())

    def test_edit_travel_view_prefills_form(self):
        url = reverse('finance:edit_travel', args=[self.travel1.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Warszawa')
        self.assertContains(response, '2025-01-01')
        self.assertContains(response, '2025-01-10')
        # Post update
        data = {
            'country': 'PL',
            'city': 'Kraków',
            'start_date': '2025-01-01',
            'end_date': '2025-01-15',
        }
        response = self.client.post(url, data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.travel1.refresh_from_db()
        self.assertEqual(self.travel1.city, 'Kraków')
        self.assertEqual(self.travel1.end_date, date(2025, 1, 15))

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
        response = self.client.post(url_edit, {'country': 'FR', 'city': 'Nice', 'start_date': '2025-03-01', 'end_date': '2025-03-05'})
        self.assertEqual(response.status_code, 404)
        response = self.client.post(url_delete)
        self.assertEqual(response.status_code, 404)

    def test_travel_list_country_names_and_flags(self):
        url = reverse('finance:travels')
        response = self.client.get(url)
        self.assertContains(response, Country('PL').name)
        self.assertContains(response, Country('DE').name)
        self.assertContains(response, 'img')  # flag icon

    def test_travel_list_empty_state(self):
        TravelDestinations.objects.filter(user=self.user).delete()
        url = reverse('finance:travels')
        response = self.client.get(url)
        self.assertContains(response, 'Brak podrózy do wyświetlenia')
        self.assertContains(response, 'Dodaj pierwszą podróz')

    def test_days_between_filter_in_template(self):
        url = reverse('finance:travels')
        response = self.client.get(url)
        self.assertContains(response, '10')  # travel1: 10 days
        self.assertContains(response, '5')   # travel2: 5 days

    def test_optional_city_field(self):
        url = reverse('finance:add_travel')
        data = {
            'country': 'CZ',
            'city': '',
            'start_date': '2025-07-01',
            'end_date': '2025-07-05',
        }
        response = self.client.post(url, data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(TravelDestinations.objects.filter(user=self.user, country='CZ', city='').exists())

