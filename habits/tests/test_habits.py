from django.contrib.auth import get_user_model
from django.urls import reverse
from django.test import TestCase

User = get_user_model()

class HabitsCoreTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser',
                                             password='testpass123')
        self.client.login(username='testuser',
                          password='testpass123')
        
    def test_add_habit(self):
        response = self.client.post(reverse('habits:add'),
                                    {'habit_name': 'Test Habit',
                                     'description': 'Test Description',
                                     'start_date': '2025-01-01',
                                     'category': 'Zdrowie'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertRedirects(response, reverse('habits:index'))

    def test_add_habit_invalid_date(self):
        response = self.client.post(reverse('habits:add'),
                                    {'habit_name': 'Test Habit',
                                     'description': 'Test Description',
                                     'start_date': 'invalid-date',
                                     'category': 'Zdrowie'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Nieprawidłowy format. Uzyj YYYY-MM-DD.')