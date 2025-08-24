from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from datetime import date
from finance.models import Monthly, Daily, Income

User = get_user_model()

class PermissionsAndFiltersTests(TestCase):
    def setUp(self):
        self.u1 = User.objects.create_user(username="u1", password="pass123")
        self.u2 = User.objects.create_user(username="u2", password="pass123")
        self.client.login(username="u1", password="pass123")

    def test_user_cannot_edit_or_delete_others_expense(self):
        m = Monthly.objects.create(user=self.u2, date=date(2025, 6, 1), total_income=0, total_expense=0)
        exp = Daily.objects.create(user=self.u2, date=date(2025, 6, 2), title="X", category="Inne", store="", cost=10, month=m)

        # u1 próbuje edytować cudzy rekord -> 404
        resp = self.client.get(reverse("finance:edit_expense", args=[exp.id]))
        self.assertEqual(resp.status_code, 404)

        resp = self.client.post(reverse("finance:delete_expense", args=[exp.id]))
        self.assertEqual(resp.status_code, 404)

    def test_expense_list_filters_by_month_and_category(self):
        m6 = Monthly.objects.create(user=self.u1, date=date(2025, 6, 1), total_income=0, total_expense=0)
        m7 = Monthly.objects.create(user=self.u1, date=date(2025, 7, 1), total_income=0, total_expense=0)
        Daily.objects.create(user=self.u1, date=date(2025, 6, 10), title="Food", category="Jedzenie", store="", cost=20, month=m6)
        Daily.objects.create(user=self.u1, date=date(2025, 7, 10), title="Bus", category="Transport", store="", cost=15, month=m7)

        resp = self.client.get(reverse("finance:expense_list"), {"month": "2025-06", "category": "Jedzenie"})
        self.assertEqual(resp.status_code, 200)
        expenses = list(resp.context["expenses"])
        self.assertEqual(len(expenses), 1)
        self.assertEqual(expenses[0].title, "Food")
        self.assertEqual(float(resp.context["total_filtered"]), 20.0)

    def test_income_list_filters_by_month_and_source(self):
        m6 = Monthly.objects.create(user=self.u1, date=date(2025, 6, 1), total_income=0, total_expense=0)
        m7 = Monthly.objects.create(user=self.u1, date=date(2025, 7, 1), total_income=0, total_expense=0)
        Income.objects.create(user=self.u1, date=date(2025, 6, 15), title="Salary", source="Pensja", amount=1000, month=m6)
        Income.objects.create(user=self.u1, date=date(2025, 7, 15), title="Freelance", source="Freelance", amount=500, month=m7)

        resp = self.client.get(reverse("finance:income_list"), {"month": "2025-06", "source": "Pensja"})
        self.assertEqual(resp.status_code, 200)
        incomes = list(resp.context["incomes"])
        self.assertEqual(len(incomes), 1)
        self.assertEqual(incomes[0].title, "Salary")
        self.assertEqual(float(resp.context["total_filtered"]), 1000.0)