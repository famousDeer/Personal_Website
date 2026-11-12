from unittest import mock

from django.db.utils import OperationalError
from django.test import TestCase, override_settings


@override_settings(ALLOWED_HOSTS=['raspberrypi.local'])
class HealthCheckTests(TestCase):
    def test_ok_without_allowed_host(self):
        # Healthcheck Dockera pyta 127.0.0.1, którego nie ma w ALLOWED_HOSTS.
        response = self.client.get('/healthz', HTTP_HOST='127.0.0.1:8000')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'ok')
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_other_paths_still_check_host(self):
        response = self.client.get('/accounts/login/', HTTP_HOST='127.0.0.1:8000')
        self.assertEqual(response.status_code, 400)

    def test_database_down_is_503(self):
        with mock.patch('config.health.connection.cursor', side_effect=OperationalError):
            response = self.client.get('/healthz', HTTP_HOST='127.0.0.1:8000')
        self.assertEqual(response.status_code, 503)


class HomeSummaryTests(TestCase):
    """Start: kafelki pokazują bieżący stan modułów zamiast stałych opisów."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create_user('ola', password='x')
        self.client.force_login(self.user)

    def test_empty_home_keeps_descriptions(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['summary'], {
            'finance': None, 'cooking': None, 'sport': None, 'timelog': None,
        })
        self.assertContains(response, 'Budżet, wydatki, przychody i raporty.')

    def test_anonymous_home_has_no_summary(self):
        self.client.logout()
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('summary', response.context)

    def test_finance_month_without_investments(self):
        from decimal import Decimal
        from django.utils import timezone
        from finance.account_utils import ensure_personal_finance_account, get_or_create_monthly_record
        from finance.models import Daily
        today = timezone.localdate()
        account = ensure_personal_finance_account(self.user)
        monthly, _ = get_or_create_monthly_record(user=self.user, account=account, month_date=today.replace(day=1))
        monthly.total_expense = Decimal('1500')
        monthly.total_income = Decimal('4000')
        monthly.save()
        Daily.objects.create(user=self.user, account=account, date=today, title='ETF', cost=Decimal('500'),
                             month=monthly, category='Inwestycje')
        summary = self.client.get('/').context['summary']
        self.assertIn('wydatki 1000\xa0zł', summary['finance'])
        self.assertIn('przychody 4000\xa0zł', summary['finance'])

    def test_cooking_counts_expiring_and_shopping(self):
        from datetime import timedelta
        from django.utils import timezone
        from cooking.models import PantryBatch, PantryProduct, ShoppingList, ShoppingListItem
        today = timezone.localdate()
        milk = PantryProduct.objects.create(name='Mleko', current_quantity=2)
        PantryBatch.objects.create(product=milk, quantity=1, expires_on=today + timedelta(days=1))
        PantryBatch.objects.create(product=milk, quantity=1, expires_on=today + timedelta(days=2))
        rice = PantryProduct.objects.create(name='Ryż', current_quantity=1)
        PantryBatch.objects.create(product=rice, quantity=1, expires_on=today + timedelta(days=60))
        active = ShoppingList.objects.create(title='Sobota')
        ShoppingListItem.objects.create(shopping_list=active, name='Chleb')
        ShoppingListItem.objects.create(shopping_list=active, name='Ser', is_purchased=True)
        done = ShoppingList.objects.create(title='Stara', status=ShoppingList.COMPLETED)
        ShoppingListItem.objects.create(shopping_list=done, name='Sól')
        summary = self.client.get('/').context['summary']
        self.assertEqual(summary['cooking'], '1 produkt do zjedzenia najpierw · 1 pozycja do kupienia')

    def test_sport_counts_training_days_this_week(self):
        from datetime import timedelta
        from django.utils import timezone
        from habits.models import ActivitySettings, Workout
        today = timezone.localdate()
        monday = today - timedelta(days=today.weekday())
        ActivitySettings.objects.create(user=self.user, weekly_goal=4)
        Workout.objects.create(user=self.user, kind='run', date=monday)
        Workout.objects.create(user=self.user, kind='gym', date=monday)
        Workout.objects.create(user=self.user, kind='swim', date=monday - timedelta(days=1))
        Workout.objects.create(user=self.user, kind='swim', date=monday, is_deleted=True)
        summary = self.client.get('/').context['summary']
        self.assertEqual(summary['sport'], 'W tym tygodniu 1 z 4 dni z treningiem')

    def test_timelog_shows_running_timer_and_waste(self):
        from datetime import timedelta
        from django.utils import timezone
        from timelog.models import Category, Entry
        now = timezone.now()
        waste = Category.objects.create(user=self.user, name='Telefon', value=Category.WASTE)
        work = Category.objects.create(user=self.user, name='Praca')
        with mock.patch('timelog.services.now', return_value=now), \
                mock.patch('django.utils.timezone.localdate', return_value=timezone.localtime(now).date()):
            start = max(now - timedelta(minutes=30), timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0))
            Entry.objects.create(user=self.user, category=waste, start=start, end=now)
            Entry.objects.create(user=self.user, category=work, start=now)
            summary = self.client.get('/').context['summary']
        self.assertIn('Teraz: Praca', summary['timelog'])
        if now - start >= timedelta(minutes=1):
            self.assertIn('strata dziś', summary['timelog'])
