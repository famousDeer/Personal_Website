from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from datetime import date
from finance.models import Monthly, Daily, Income

User = get_user_model()

# Adjust these if your URLs differ
API_URL_DAILY = "/api/daily/"
API_URL_MONTHLY = "/api/monthly/"

class APITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="apiu", password="pass123")
        self.client = APIClient()
        self.client.login(username="apiu", password="pass123")

    def test_daily_list_and_create_scoped_to_user(self):
        m = Monthly.objects.create(user=self.user, date=date(2025, 6, 1), total_income=0, total_expense=0)
        Daily.objects.create(user=self.user, date=date(2025, 6, 2), title="Food", category="Jedzenie", store="", cost=10, month=m)

        # List
        r = self.client.get(API_URL_DAILY)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any(item["title"] == "Food" for item in r.json()))

        # Create
        r = self.client.post(API_URL_DAILY, {
            "date": "2025-06-03",
            "title": "Bus",
            "category": "Transport",
            "store": "",
            "cost": "5.00"
        }, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Daily.objects.filter(user=self.user, title="Bus").exists())

    def test_monthly_list_scoped_to_user(self):
        Monthly.objects.create(user=self.user, date=date(2025, 6, 1), total_income=0, total_expense=0)
        r = self.client.get(API_URL_MONTHLY)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(len(data) >= 1)