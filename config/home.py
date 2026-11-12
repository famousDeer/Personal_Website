"""Stan modułów na stronie Start (`templates/home.html`).

Każda funkcja zwraca gotowe zdanie do kafelka albo None - wtedy kafelek
zostaje przy stałym opisie. Tylko kilka zliczających zapytań, bez zapisu:
strona główna jest otwierana najczęściej.
"""
from datetime import timedelta

from django.db.models import Sum
from django.utils import timezone
from django.utils.formats import date_format

from accounts.templatetags.ui import pl, zl
from cooking.models import PantryBatch, ShoppingList, ShoppingListItem
from cooking.services.pantry_batches import EAT_FIRST_DAYS
from finance.account_utils import get_active_finance_account
from finance.investment_funding import INVESTMENT_CATEGORY
from finance.models import Daily, Monthly
from habits.models import ActivitySettings, Workout
from timelog import services as timelog_services
from timelog.models import Category
from timelog.templatetags.timelog import hm
from utils.tools import month_start


def home_summary(request):
    today = timezone.localdate()
    return {
        'finance': _finance(request, today),
        'cooking': _cooking(today),
        'sport': _sport(request.user, today),
        'timelog': _timelog(request.user, today),
    }


def _finance(request, today):
    account = get_active_finance_account(request)
    monthly = Monthly.objects.filter(account=account, date=month_start(today)).first()
    if not monthly or not (monthly.total_expense or monthly.total_income):
        return None
    # Jak na panelu finansowym: inwestycje to nie wydatki na życie.
    invested = (
        Daily.objects.filter(account=account, month=monthly, category=INVESTMENT_CATEGORY)
        .aggregate(total=Sum('cost'))['total'] or 0
    )
    month_name = date_format(today, 'F').capitalize()
    return f'{month_name}: wydatki {zl(monthly.total_expense - invested)} · przychody {zl(monthly.total_income)}'


def _cooking(today):
    expiring = (
        PantryBatch.objects
        .filter(expires_on__lte=today + timedelta(days=EAT_FIRST_DAYS), quantity__gt=0)
        .values('product').distinct().count()
    )
    to_buy = ShoppingListItem.objects.filter(
        shopping_list__status=ShoppingList.ACTIVE, is_purchased=False,
    ).count()
    parts = []
    if expiring:
        parts.append(f'{pl(expiring, "produkt,produkty,produktów")} do zjedzenia najpierw')
    if to_buy:
        parts.append(f'{pl(to_buy, "pozycja,pozycje,pozycji")} do kupienia')
    return ' · '.join(parts) or None


def _sport(user, today):
    monday = today - timedelta(days=today.weekday())
    days = (
        Workout.objects
        .filter(user=user, is_deleted=False, date__gte=monday, date__lte=monday + timedelta(days=6))
        .values('date').distinct().count()
    )
    settings = ActivitySettings.objects.filter(user=user).first()
    if not settings and not days:
        return None
    goal = settings.weekly_goal if settings else ActivitySettings._meta.get_field('weekly_goal').default
    return f'W tym tygodniu {days} z {goal} dni z treningiem'


def _timelog(user, today):
    parts = []
    running = timelog_services.running_entry(user)
    if running:
        parts.append(f'Teraz: {running.category.name}')
    totals = timelog_services.day_data(user, today)['totals']
    waste = sum(seconds for category, seconds in totals if category.value == Category.WASTE)
    if waste >= 60:
        parts.append(f'strata dziś {hm(waste)}')
    return ' · '.join(parts) or None
