from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.utils import timezone
from finance.models import Daily, Income, Monthly
from datetime import date

User = get_user_model()

class FinanceCoreTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="pass123")
        self.personal_account = self.user.owned_finance_accounts.get(account_type='personal')
        self.client.login(username="u1", password="pass123")

    def test_add_expense_creates_monthly_and_updates_total(self):
        resp = self.client.post(reverse("finance:add_expense"), {
            "date": "2025-06-15",
            "title": "Lunch",
            "category": "Jedzenie",
            "store": "Cafe",
            "cost": "12.50",
        }, follow=True)
        self.assertEqual(resp.status_code, 200)

        monthly = Monthly.objects.get(account=self.personal_account, date=date(2025, 6, 1))
        self.assertEqual(float(monthly.total_expense), 12.50)

        exp = Daily.objects.get(account=self.personal_account, title="Lunch")
        self.assertEqual(float(exp.cost), 12.50)

    def test_edit_expense_move_to_other_month_recalculates_both(self):
        # start in May
        may = Monthly.objects.create(user=self.user, account=self.personal_account, date=date(2025, 5, 1), total_income=0, total_expense=0)
        exp = Daily.objects.create(
            user=self.user, account=self.personal_account, date=date(2025, 5, 10), title="Ticket", category="Transport",
            store="", cost=100.0, month=may
        )
        may.total_expense = 100
        may.save()

        # move to June
        resp = self.client.post(reverse("finance:edit_expense", args=[exp.id]), {
            "date": "2025-06-02",
            "title": "Ticket",
            "category": "Transport",
            "store": "",
            "cost": "100",
        }, follow=True)
        self.assertEqual(resp.status_code, 200)

        may.refresh_from_db()
        june = Monthly.objects.get(account=self.personal_account, date=date(2025, 6, 1))
        exp.refresh_from_db()

        self.assertEqual(exp.month, june)
        self.assertEqual(float(may.total_expense), 0.0)
        self.assertEqual(float(june.total_expense), 100.0)

    def test_delete_expense_recalculates_month(self):
        m = Monthly.objects.create(user=self.user, account=self.personal_account, date=date(2025, 7, 1), total_income=0, total_expense=0)
        e1 = Daily.objects.create(user=self.user, account=self.personal_account, date=date(2025, 7, 1), title="A", category="Inne", store="", cost=10, month=m)
        Daily.objects.create(user=self.user, account=self.personal_account, date=date(2025, 7, 2), title="B", category="Inne", store="", cost=5, month=m)
        m.total_expense = 15
        m.save()

        resp = self.client.post(reverse("finance:delete_expense", args=[e1.id]), follow=True)
        self.assertEqual(resp.status_code, 200)

        m.refresh_from_db()
        self.assertEqual(float(m.total_expense), 5.0)
        self.assertFalse(Daily.objects.filter(id=e1.id).exists())

    def test_add_income_updates_month_total(self):
        resp = self.client.post(reverse("finance:add_income"), {
            "date": "2025-06-20",
            "title": "Salary",
            "amount": "3000",
            "source": "Pensja",
        }, follow=True)
        self.assertEqual(resp.status_code, 200)

        monthly = Monthly.objects.get(account=self.personal_account, date=date(2025, 6, 1))
        self.assertEqual(float(monthly.total_income), 3000.0)
        inc = Income.objects.get(account=self.personal_account, title="Salary")
        self.assertEqual(float(inc.amount), 3000.0)

    def test_edit_income_move_to_other_month_recalculates_both(self):
        may = Monthly.objects.create(user=self.user, account=self.personal_account, date=date(2025, 5, 1), total_income=0, total_expense=0)
        inc = Income.objects.create(user=self.user, account=self.personal_account, date=date(2025, 5, 15), title="Bonus", source="Premia", amount=200.0, month=may)
        may.total_income = 200
        may.save()

        resp = self.client.post(reverse("finance:edit_income", args=[inc.id]), {
            "date": "2025-06-01",
            "title": "Bonus",
            "source": "Premia",
            "amount": "200",
        }, follow=True)
        self.assertEqual(resp.status_code, 200)

        may.refresh_from_db()
        june = Monthly.objects.get(account=self.personal_account, date=date(2025, 6, 1))
        inc.refresh_from_db()

        # Expected: moved and both months recalculated
        self.assertEqual(inc.month, june)
        self.assertEqual(float(may.total_income), 0.0)
        self.assertEqual(float(june.total_income), 200.0)

    def test_dashboard_sets_context(self):
        today = timezone.now().date()
        Monthly.objects.get_or_create(
            account=self.personal_account,
            date=today.replace(day=1),
            defaults={"user": self.user, "total_income": 0, "total_expense": 0},
        )
        resp = self.client.get(reverse("finance:dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("total_income", resp.context)
        self.assertIn("total_expense", resp.context)
        self.assertIn("balance", resp.context)

    def test_dashboard_uses_selected_cost_categories(self):
        today = timezone.now().date()
        month = Monthly.objects.create(user=self.user, account=self.personal_account, date=today.replace(day=1), total_income=0, total_expense=0)
        Daily.objects.create(user=self.user, account=self.personal_account, date=today, title="Paliwo", category="Paliwo", store="", cost=100, month=month)
        Daily.objects.create(user=self.user, account=self.personal_account, date=today, title="Sport", category="Sport", store="", cost=50, month=month)
        Daily.objects.create(user=self.user, account=self.personal_account, date=today, title="Rachunki", category="Rachunki", store="", cost=200, month=month)

        resp = self.client.get(reverse("finance:dashboard"), {"cost_category": ["Sport", "Paliwo"]})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["selected_cost_categories"], ["Sport", "Paliwo"])
        self.assertEqual(float(resp.context["selected_category_total"]), 150.0)

    def test_transfer_to_shared_account_creates_linked_income(self):
        partner = User.objects.create_user(username="u2", password="pass123")
        shared_account = partner.finance_accounts.filter(account_type='shared').first()
        self.assertIsNone(shared_account)

        resp = self.client.post(reverse("profile"), {
            "form_name": "shared_account",
            "name": "Dom",
            "partner_username": "u2",
        }, follow=True)
        self.assertEqual(resp.status_code, 200)

        shared_account = self.user.finance_accounts.get(account_type='shared')

        resp = self.client.post(reverse("finance:add_expense"), {
            "date": "2025-06-15",
            "title": "Wpłata majowa",
            "category": "Wpłata do wspólnego z mBank",
            "store": "",
            "cost": "250.00",
            "transfer_target_account": str(shared_account.id),
        }, follow=True)
        self.assertEqual(resp.status_code, 200)

        shared_month = Monthly.objects.get(account=shared_account, date=date(2025, 6, 1))
        linked_income = Income.objects.get(account=shared_account, linked_expense__title="Wpłata majowa")

        self.assertEqual(float(shared_month.total_income), 250.0)
        self.assertEqual(float(linked_income.amount), 250.0)
