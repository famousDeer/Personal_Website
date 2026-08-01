import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Max, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django_countries import countries as django_countries
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from utils.tools import month_start, parse_date_input, parse_decimal

from .account_utils import (
    TRANSFER_INCOME_SOURCE,
    TRANSFER_TO_SHARED_CATEGORY,
    get_active_finance_account,
    get_available_shared_accounts,
    get_or_create_monthly_record,
    recalculate_monthly_record,
    set_active_finance_account,
    sync_shared_account_transfer,
)
from .brokerage import build_portfolio_summary, get_quantity
from .bank_import import BankImportError, import_candidates_from_post, parse_bank_csv
from .forms import (
    BankTransactionImportForm,
    BrokerageAccountForm,
    BrokerageDividendForm,
    BrokerageInstrumentForm,
    BrokerageTransactionImportForm,
    BrokerageTransactionForm,
    TravelDestinationForm,
)
from .brokerage_import import BrokerageImportError, import_xtb_transactions
from .geocoding import populate_destination_coordinates
from .market_data import MarketDataError, fetch_historical_market_prices, refresh_market_data_for_user
from .models import (
    BrokerageAccount,
    BrokerageDividend,
    BrokerageInstrument,
    BrokerageTransaction,
    Daily,
    FinanceAccount,
    Income,
    Monthly,
    TravelDestinations,
)
from .portfolio_history import build_portfolio_value_history

CATEGORIES_EXPENSES = sorted([
    'Zakupy spozywcze', 'Jedzenie na miescie', 'Transport miejski',
    'Rozrywka', 'Podroze - nocleg', 'Podroze - jedzenie',
    'Podroze - atrakcje', 'Podroze - pamiatki', 'Podroze - transport',
    'Paliwo', 'Rachunki', 'Zdrowie', 'Edukacja', 'Rodzina', 'Ubrania',
    'Delegacje', 'Inwestycje', 'Inne', 'Rata kredytu konsumenckiego',
    'Wyposazenie domu', 'Sport', 'Hobby', 'Prezenty', 'Spłata karty kredytowej',
    'Uroda', 'Drogeria', 'Pielęgnacja auta', 'Serwis auta', 'Części do auta',
    'Subskrypcje', TRANSFER_TO_SHARED_CATEGORY,
])

INCOME_SOURCES = sorted([
    'Pensja', 'Premia', 'Dieta', 'Inwestycje',
    'Zwrot podatku', 'Sprzedaż', 'Rodzina', 'Inne'
])


def _travel_status(destination, today):
    if destination.start_date > today:
        return {
            'key': 'planned',
            'label': 'Planowana',
            'badge_class': 'bg-primary-subtle text-primary',
            'icon': 'bi-calendar-event',
        }
    if destination.end_date < today:
        return {
            'key': 'completed',
            'label': 'Zakończona',
            'badge_class': 'bg-success-subtle text-success',
            'icon': 'bi-check2-circle',
        }
    return {
        'key': 'active',
        'label': 'W trakcie',
        'badge_class': 'bg-warning-subtle text-warning',
        'icon': 'bi-geo-alt-fill',
    }


def _attach_travel_status(destination, today):
    destination.travel_status = _travel_status(destination, today)
    return destination


def _travel_type_meta(destination):
    if destination.travel_type == TravelDestinations.BUSINESS:
        return {
            'key': destination.travel_type,
            'label': destination.get_travel_type_display(),
            'badge_class': 'bg-warning-subtle text-warning',
            'icon': 'bi-briefcase-fill',
        }
    return {
        'key': destination.travel_type,
        'label': destination.get_travel_type_display(),
        'badge_class': 'bg-info-subtle text-info',
        'icon': 'bi-sun-fill',
    }


def _attach_travel_display_meta(destination, today):
    _attach_travel_status(destination, today)
    destination.travel_type_meta = _travel_type_meta(destination)
    return destination


def _travel_location_key(destination):
    return (
        str(destination.country.code),
        (destination.city or '').strip().casefold(),
    )


def _travel_interval_json(destination):
    return {
        'id': destination.id,
        'startDate': destination.start_date.strftime('%d.%m.%Y'),
        'endDate': destination.end_date.strftime('%d.%m.%Y'),
        'days': destination.duration_days,
        'budget': float(destination.budget),
        'type': destination.travel_type_meta['label'],
        'typeKey': destination.travel_type_meta['key'],
        'status': destination.travel_status['label'],
        'editUrl': reverse('finance:edit_travel', args=[destination.id]),
    }


def _group_travel_locations(destinations):
    groups = {}
    for destination in destinations:
        key = _travel_location_key(destination)
        if key not in groups:
            groups[key] = {
                'key': f'{key[0]}-{key[1] or "country"}',
                'label': destination.destination_name,
                'country': destination.country.name,
                'country_code': str(destination.country.code),
                'flag': str(destination.country.flag),
                'latitude': destination.latitude,
                'longitude': destination.longitude,
                'destinations': [],
                'total_budget': Decimal('0.00'),
                'total_days': 0,
                'business_days': 0,
                'leisure_days': 0,
                'business_count': 0,
                'leisure_count': 0,
                'latest_start_date': destination.start_date,
            }

        group = groups[key]
        group['destinations'].append(destination)
        group['total_budget'] += destination.budget
        group['total_days'] += destination.duration_days
        group['latest_start_date'] = max(group['latest_start_date'], destination.start_date)
        if destination.has_coordinates and group['latitude'] is None:
            group['latitude'] = destination.latitude
            group['longitude'] = destination.longitude

        if destination.is_business_trip:
            group['business_days'] += destination.duration_days
            group['business_count'] += 1
        else:
            group['leisure_days'] += destination.duration_days
            group['leisure_count'] += 1

    for group in groups.values():
        group['destinations'] = sorted(
            group['destinations'],
            key=lambda destination: (destination.start_date, destination.id),
            reverse=True,
        )
        group['trip_count'] = len(group['destinations'])
        group['has_coordinates'] = group['latitude'] is not None and group['longitude'] is not None

    return sorted(groups.values(), key=lambda group: group['latest_start_date'], reverse=True)


def _travel_map_point(location_group):
    if not location_group['has_coordinates']:
        return None
    return {
        'id': location_group['key'],
        'label': location_group['label'],
        'country': location_group['country'],
        'countryCode': location_group['country_code'],
        'flag': location_group['flag'],
        'lat': float(location_group['latitude']),
        'lng': float(location_group['longitude']),
        'tripCount': location_group['trip_count'],
        'totalDays': location_group['total_days'],
        'totalBudget': float(location_group['total_budget']),
        'businessCount': location_group['business_count'],
        'leisureCount': location_group['leisure_count'],
        'intervals': [
            _travel_interval_json(destination)
            for destination in location_group['destinations']
        ],
    }


def _days_in_year(year):
    return 366 if calendar.isleap(year) else 365


def _delegation_period_days(year, today):
    if year == today.year:
        return (today - date(year, 1, 1)).days + 1
    return _days_in_year(year)


def _delegation_year_stats(destinations, today):
    days_by_year = {}
    for destination in destinations:
        if not destination.is_business_trip:
            continue
        if destination.start_date > today:
            continue
        current = destination.start_date
        end_date = min(destination.end_date, today)
        while current <= end_date:
            days_by_year.setdefault(current.year, set()).add(current)
            current += timedelta(days=1)

    stats = []
    for year, days in days_by_year.items():
        year_days = _delegation_period_days(year, today)
        delegation_days = len(days)
        percent = (Decimal(delegation_days) / Decimal(year_days) * Decimal('100')).quantize(Decimal('0.1'))
        stats.append({
            'year': year,
            'days': delegation_days,
            'year_days': year_days,
            'percent': percent,
            'percent_css': str(percent),
        })
    return sorted(stats, key=lambda item: item['year'], reverse=True)


def _delegation_days_count(destinations):
    delegation_days = set()
    for destination in destinations:
        if not destination.is_business_trip:
            continue
        current = destination.start_date
        while current <= destination.end_date:
            delegation_days.add(current)
            current += timedelta(days=1)
    return len(delegation_days)


def _copy_existing_destination_coordinates(destination, user):
    matching_destinations = TravelDestinations.objects.filter(
        user=user,
        country=destination.country,
        city__iexact=(destination.city or '').strip(),
        latitude__isnull=False,
        longitude__isnull=False,
    )
    if destination.pk:
        matching_destinations = matching_destinations.exclude(pk=destination.pk)

    existing_destination = matching_destinations.order_by('-start_date').first()
    if existing_destination is None:
        return False

    destination.latitude = existing_destination.latitude
    destination.longitude = existing_destination.longitude
    return True


def _populate_destination_coordinates_for_user(destination, user, force=False):
    if force:
        destination.latitude = None
        destination.longitude = None
    elif destination.has_coordinates:
        return True

    if _copy_existing_destination_coordinates(destination, user):
        return True

    if not getattr(settings, 'TRAVEL_GEOCODING_ENABLED', True):
        return False

    return populate_destination_coordinates(destination, force=False)


def _decimal_json(value):
    if value is None:
        return None
    return str(value)


def _history_summary(points):
    if not points:
        return {
            'points_count': 0,
            'first_close': None,
            'last_close': None,
            'change': None,
            'change_percent': None,
            'high': None,
            'low': None,
        }

    closes = [point['close'] for point in points if point.get('close') is not None]
    highs = [point['high'] for point in points if point.get('high') is not None]
    lows = [point['low'] for point in points if point.get('low') is not None]
    if not closes:
        return {
            'points_count': len(points),
            'first_close': None,
            'last_close': None,
            'change': None,
            'change_percent': None,
            'high': None,
            'low': None,
        }

    first_close = closes[0]
    last_close = closes[-1]
    change = last_close - first_close
    change_percent = None
    if first_close:
        change_percent = (change / first_close) * Decimal('100')

    return {
        'points_count': len(points),
        'first_close': _decimal_json(first_close),
        'last_close': _decimal_json(last_close),
        'change': _decimal_json(change),
        'change_percent': _decimal_json(change_percent.quantize(Decimal('0.01'))) if change_percent is not None else None,
        'high': _decimal_json(max(highs or closes)),
        'low': _decimal_json(min(lows or closes)),
    }


def _history_points_json(points):
    return [
        {
            'date': point['date'].isoformat(),
            'open': _decimal_json(point.get('open')),
            'high': _decimal_json(point.get('high')),
            'low': _decimal_json(point.get('low')),
            'close': _decimal_json(point.get('close')),
            'volume': point.get('volume'),
        }
        for point in points
    ]


def _parse_history_range(request):
    today = timezone.localdate()
    allowed_days = {30, 90, 180, 365, 1095}
    start_value = request.GET.get('start_date')
    end_value = request.GET.get('end_date')

    if start_value or end_value:
        if not start_value or not end_value:
            raise ValueError('Podaj datę początku i końca zakresu.')
        start_date = parse_date_input(start_value)
        end_date = parse_date_input(end_value)
        if end_date > today:
            end_date = today
        if start_date > end_date:
            raise ValueError('Data początku zakresu nie może być późniejsza niż data końca.')
        if (end_date - start_date).days > 3650:
            raise ValueError('Maksymalny zakres wykresu to 10 lat.')
        return start_date, end_date, None, 'custom'

    try:
        days = int(request.GET.get('days', '365'))
    except (TypeError, ValueError):
        days = 365
    if days not in allowed_days:
        days = 365

    return today - timedelta(days=days), today, days, 'days'


def _portfolio_history_cache_version(user, selected_account=None):
    transaction_queryset = BrokerageTransaction.objects.filter(account__user=user)
    instrument_queryset = BrokerageInstrument.objects.filter(user=user)
    if selected_account is not None:
        transaction_queryset = transaction_queryset.filter(account=selected_account)
        instrument_queryset = instrument_queryset.filter(transactions__account=selected_account)

    transaction_stats = transaction_queryset.aggregate(
        count=Count('id'),
        latest_id=Max('id'),
        latest_created_at=Max('created_at'),
    )
    instrument_stats = instrument_queryset.aggregate(
        count=Count('id', distinct=True),
        latest_id=Max('id'),
        latest_price_at=Max('last_price_at'),
    )
    latest_created_at = transaction_stats.get('latest_created_at')
    latest_price_at = instrument_stats.get('latest_price_at')
    return ':'.join([
        str(transaction_stats.get('count') or 0),
        str(transaction_stats.get('latest_id') or 0),
        latest_created_at.isoformat() if latest_created_at else '0',
        str(instrument_stats.get('count') or 0),
        str(instrument_stats.get('latest_id') or 0),
        latest_price_at.isoformat() if latest_price_at else '0',
    ])


def _portfolio_history_cache_key(user, selected_account, start_date, end_date):
    account_key = selected_account.id if selected_account is not None else 'all'
    version = _portfolio_history_cache_version(user, selected_account)
    return f'brokerage-portfolio-history:{user.id}:{account_key}:{start_date.isoformat()}:{end_date.isoformat()}:{version}'


def _portfolio_history_payload(user, selected_account, start_date, end_date, selected_days, range_mode):
    cache_key = _portfolio_history_cache_key(user, selected_account, start_date, end_date)
    history = cache.get(cache_key)
    if history is None:
        history = build_portfolio_value_history(
            user,
            start_date=start_date,
            end_date=end_date,
            selected_account=selected_account,
        )
        cache.set(cache_key, history, timeout=300)

    history = {**history}
    history.update({
        'range': {
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat(),
            'days': selected_days,
            'mode': range_mode,
        },
        'scope': {
            'account_id': selected_account.id if selected_account else None,
            'account_name': selected_account.name if selected_account else 'Wszystkie konta',
            'is_all': selected_account is None,
        },
    })
    return history

COST_OF_LIVING_CATEGORIES = [
    'Zakupy spozywcze', 'Paliwo', 'Rachunki', 'Zdrowie'
]
INVESTMENT_CATEGORY = 'Inwestycje'


def get_available_expense_categories(account):
    dynamic_categories = Daily.objects.filter(account=account).values_list('category', flat=True).distinct()
    categories = set(CATEGORIES_EXPENSES)
    if account.account_type != FinanceAccount.PERSONAL:
        categories.discard(TRANSFER_TO_SHARED_CATEGORY)
    return sorted(categories.union(dynamic_categories))


def get_store_suggestions(account):
    stores = Daily.objects.filter(account=account).values_list('store', flat=True)
    return sorted(
        {store.strip() for store in stores if store and store.strip()},
        key=str.casefold,
    )


def get_investment_queryset(queryset):
    return queryset.filter(category=INVESTMENT_CATEGORY)


def get_non_investment_queryset(queryset):
    return queryset.exclude(category=INVESTMENT_CATEGORY)


def get_selected_transfer_target(request, active_account, category):
    if active_account.account_type != FinanceAccount.PERSONAL or category != TRANSFER_TO_SHARED_CATEGORY:
        return None

    raw_target_id = request.POST.get('transfer_target_account')
    if not raw_target_id:
        raise ValueError('Wybierz konto wspólne, które chcesz zasilić.')

    target_account = get_available_shared_accounts(request.user).filter(id=raw_target_id).first()
    if target_account is None:
        raise ValueError('Wybrane konto wspólne nie jest dostępne dla tego użytkownika.')
    return target_account


def get_expense_form_context(request, active_account, **extra_context):
    shared_target_accounts = list(get_available_shared_accounts(request.user))
    context = {
        'categories': get_available_expense_categories(active_account),
        'store_suggestions': get_store_suggestions(active_account),
        'today': timezone.now().date(),
        'transfer_category': TRANSFER_TO_SHARED_CATEGORY,
        'shared_target_accounts': shared_target_accounts,
        'show_shared_transfer_option': active_account.account_type == FinanceAccount.PERSONAL and bool(shared_target_accounts),
    }
    context.update(extra_context)
    return context


@login_required
def index(request):
    return render(request, 'finance/index.html')


@login_required
def switch_account(request):
    if request.method != 'POST':
        return redirect('finance:index')

    account = get_object_or_404(FinanceAccount, id=request.POST.get('account_id'), members=request.user)
    set_active_finance_account(request, account)
    messages.success(request, f'Aktywne konto: {account.display_name}.')

    next_url = request.POST.get('next') or reverse('finance:index')
    return redirect(next_url)


@method_decorator(login_required, name='dispatch')
class BrokeragePortfolioView(View):
    def get(self, request):
        current_month_start = month_start(timezone.localdate())
        if current_month_start.month == 12:
            next_month_start = current_month_start.replace(year=current_month_start.year + 1, month=1)
        else:
            next_month_start = current_month_start.replace(month=current_month_start.month + 1)

        brokerage_accounts = list(BrokerageAccount.objects.filter(user=request.user))
        selected_account = None
        selected_account_param = request.GET.get('account', 'all')
        if selected_account_param and selected_account_param != 'all':
            try:
                selected_account_id = int(selected_account_param)
            except (TypeError, ValueError):
                selected_account_id = None
            if selected_account_id is not None:
                selected_account = get_object_or_404(BrokerageAccount, id=selected_account_id, user=request.user)

        brokerage_instruments = BrokerageInstrument.objects.filter(user=request.user)
        brokerage_transactions = BrokerageTransaction.objects.filter(account__user=request.user)
        brokerage_dividends = BrokerageDividend.objects.filter(account__user=request.user)
        if selected_account is not None:
            brokerage_instruments = brokerage_instruments.filter(
                Q(transactions__account=selected_account) | Q(dividends__account=selected_account)
            ).distinct()
            brokerage_transactions = brokerage_transactions.filter(account=selected_account)
            brokerage_dividends = brokerage_dividends.filter(account=selected_account)

        history_days = 365
        history_end_date = timezone.localdate()
        initial_portfolio_history = _portfolio_history_payload(
            request.user,
            selected_account,
            history_end_date - timedelta(days=history_days),
            history_end_date,
            history_days,
            'days',
        )

        summary = build_portfolio_summary(request.user, selected_account=selected_account)
        summary.update({
            'brokerage_accounts': brokerage_accounts,
            'selected_brokerage_account': selected_account,
            'selected_brokerage_account_id': selected_account.id if selected_account else None,
            'brokerage_current_month': current_month_start,
            'brokerage_initial_portfolio_history': initial_portfolio_history,
            'brokerage_instruments': brokerage_instruments.order_by('name', 'ticker'),
            'brokerage_transactions': (
                brokerage_transactions
                .filter(trade_date__gte=current_month_start, trade_date__lt=next_month_start)
                .select_related('account', 'instrument')
                .order_by('-trade_date', '-id')[:25]
            ),
            'brokerage_dividends': (
                brokerage_dividends
                .select_related('account', 'instrument')
                .order_by('-payment_date', 'instrument__ticker')[:25]
            ),
        })
        return render(request, 'finance/brokerage.html', summary)


@method_decorator(login_required, name='dispatch')
class BrokeragePortfolioHistoryDataView(View):
    def get(self, request):
        try:
            start_date, end_date, selected_days, range_mode = _parse_history_range(request)
        except ValueError as exc:
            return JsonResponse({'error': str(exc)}, status=400)

        selected_account = None
        account_param = request.GET.get('account')
        if account_param and account_param != 'all':
            try:
                account_id = int(account_param)
            except (TypeError, ValueError):
                return JsonResponse({'error': 'Nieprawidłowe konto maklerskie.'}, status=400)
            selected_account = get_object_or_404(BrokerageAccount, id=account_id, user=request.user)

        history = _portfolio_history_payload(
            request.user,
            selected_account,
            start_date,
            end_date,
            selected_days,
            range_mode,
        )
        return JsonResponse(history)


@method_decorator(login_required, name='dispatch')
class RefreshBrokerageMarketDataView(View):
    def post(self, request):
        try:
            result = refresh_market_data_for_user(request.user)
        except MarketDataError as exc:
            messages.warning(request, f'Nie udało się odświeżyć danych rynkowych: {exc}')
        else:
            failed_count = len(result.get('failed_quotes', []))
            if failed_count:
                messages.warning(
                    request,
                    f"Nie odświeżono {failed_count} instrumentów: {'; '.join(result['failed_quotes'][:3])}",
                )
            failed_dividends_count = len(result.get('failed_dividends', []))
            if failed_dividends_count:
                messages.warning(
                    request,
                    f"Nie odświeżono dywidend dla {failed_dividends_count} instrumentów: {'; '.join(result['failed_dividends'][:3])}",
                )
            messages.success(
                request,
                f"Odświeżono ceny: {result['updated_quotes']}, "
                f"scalone instrumenty: {result.get('merged_instruments', 0)}, "
                f"nowe dywidendy: {result['updated_dividends']} ({result['source']}).",
            )
        return redirect('finance:brokerage')


@method_decorator(login_required, name='dispatch')
class AddBrokerageAccountView(View):
    def get(self, request):
        return render(request, 'finance/brokerage_form.html', {
            'form': BrokerageAccountForm(),
            'title': 'Dodaj konto maklerskie',
        })

    def post(self, request):
        form = BrokerageAccountForm(request.POST)
        if form.is_valid():
            account = form.save(commit=False)
            account.user = request.user
            account.save()
            messages.success(request, 'Konto maklerskie zostało dodane.')
            return redirect('finance:brokerage')
        return render(request, 'finance/brokerage_form.html', {'form': form, 'title': 'Dodaj konto maklerskie'})


@method_decorator(login_required, name='dispatch')
class EditBrokerageAccountView(View):
    def get(self, request, account_id):
        account = get_object_or_404(BrokerageAccount, id=account_id, user=request.user)
        return render(request, 'finance/brokerage_form.html', {
            'form': BrokerageAccountForm(instance=account),
            'title': 'Edytuj konto maklerskie',
        })

    def post(self, request, account_id):
        account = get_object_or_404(BrokerageAccount, id=account_id, user=request.user)
        form = BrokerageAccountForm(request.POST, instance=account)
        if form.is_valid():
            form.save()
            messages.success(request, 'Konto maklerskie zostało zaktualizowane.')
            return redirect('finance:brokerage')
        return render(request, 'finance/brokerage_form.html', {'form': form, 'title': 'Edytuj konto maklerskie'})


@method_decorator(login_required, name='dispatch')
class DeleteBrokerageAccountView(View):
    def post(self, request, account_id):
        account = get_object_or_404(BrokerageAccount, id=account_id, user=request.user)
        account_name = account.name
        account.delete()
        messages.success(request, f'Konto maklerskie "{account_name}" zostało usunięte.')
        return redirect('finance:brokerage')


@method_decorator(login_required, name='dispatch')
class AddBrokerageInstrumentView(View):
    def get(self, request):
        return render(request, 'finance/brokerage_form.html', {
            'form': BrokerageInstrumentForm(),
            'title': 'Dodaj instrument',
        })

    def post(self, request):
        form = BrokerageInstrumentForm(request.POST)
        if form.is_valid():
            instrument = form.save(commit=False)
            instrument.user = request.user
            if instrument.last_price is not None:
                instrument.last_price_at = timezone.now()
                instrument.market_data_source = 'Ręcznie'
            instrument.save()
            messages.success(request, 'Instrument został dodany.')
            return redirect('finance:brokerage')
        return render(request, 'finance/brokerage_form.html', {'form': form, 'title': 'Dodaj instrument'})


@method_decorator(login_required, name='dispatch')
class EditBrokerageInstrumentView(View):
    def get(self, request, instrument_id):
        instrument = get_object_or_404(BrokerageInstrument, id=instrument_id, user=request.user)
        return render(request, 'finance/brokerage_form.html', {
            'form': BrokerageInstrumentForm(instance=instrument),
            'title': 'Edytuj instrument',
        })

    def post(self, request, instrument_id):
        instrument = get_object_or_404(BrokerageInstrument, id=instrument_id, user=request.user)
        form = BrokerageInstrumentForm(request.POST, instance=instrument)
        if form.is_valid():
            instrument = form.save(commit=False)
            if instrument.last_price is not None and not instrument.last_price_at:
                instrument.last_price_at = timezone.now()
                instrument.market_data_source = instrument.market_data_source or 'Ręcznie'
            instrument.save()
            messages.success(request, 'Instrument został zaktualizowany.')
            return redirect('finance:brokerage')
        return render(request, 'finance/brokerage_form.html', {'form': form, 'title': 'Edytuj instrument'})


@method_decorator(login_required, name='dispatch')
class DeleteBrokerageInstrumentView(View):
    def post(self, request, instrument_id):
        instrument = get_object_or_404(BrokerageInstrument, id=instrument_id, user=request.user)
        instrument_name = instrument.name
        instrument.delete()
        messages.success(request, f'Instrument "{instrument_name}" został usunięty.')
        return redirect('finance:brokerage')


@method_decorator(login_required, name='dispatch')
class BrokerageInstrumentDetailDataView(View):
    def get(self, request, instrument_id):
        instrument = get_object_or_404(BrokerageInstrument, id=instrument_id, user=request.user)
        try:
            start_date, end_date, selected_days, range_mode = _parse_history_range(request)
        except ValueError as exc:
            return JsonResponse({'error': str(exc)}, status=400)

        history_error = ''
        history_source = ''
        history_points = []
        try:
            history_data = fetch_historical_market_prices(
                symbol=instrument.ticker,
                exchange=instrument.exchange,
                currency=instrument.currency,
                isin=instrument.isin,
                price_symbol=instrument.price_symbol,
                name=instrument.name,
                start_date=start_date,
                end_date=end_date,
            )
        except MarketDataError as exc:
            history_error = str(exc)
        else:
            history_points = history_data['points']
            history_source = history_data['source']
            if history_data.get('resolution_error'):
                history_error = f"OpenFIGI: {history_data['resolution_error']}"

        accounts = []
        account_queryset = (
            BrokerageAccount.objects
            .filter(user=request.user, transactions__instrument=instrument)
            .distinct()
            .order_by('name')
        )
        for account in account_queryset:
            quantity = get_quantity(account, instrument)
            if quantity <= 0:
                continue
            accounts.append({
                'name': account.name,
                'broker': account.get_broker_display(),
                'type': account.get_account_type_display(),
                'currency': account.currency,
                'quantity': _decimal_json(quantity),
            })

        latest_transaction = (
            BrokerageTransaction.objects
            .filter(account__user=request.user, instrument=instrument)
            .select_related('account')
            .order_by('-trade_date', '-id')
            .first()
        )
        latest_transaction_payload = None
        if latest_transaction is not None:
            latest_transaction_payload = {
                'date': latest_transaction.trade_date.isoformat(),
                'type': latest_transaction.get_transaction_type_display(),
                'quantity': _decimal_json(latest_transaction.quantity),
                'price': _decimal_json(latest_transaction.price),
                'account': latest_transaction.account.name,
            }

        chart_transactions = []
        transaction_queryset = (
            BrokerageTransaction.objects
            .filter(
                account__user=request.user,
                instrument=instrument,
                trade_date__gte=start_date,
                trade_date__lte=end_date,
            )
            .select_related('account')
            .order_by('trade_date', 'id')
        )
        for transaction in transaction_queryset:
            chart_transactions.append({
                'date': transaction.trade_date.isoformat(),
                'type': transaction.transaction_type,
                'type_display': transaction.get_transaction_type_display(),
                'quantity': _decimal_json(transaction.quantity),
                'price': _decimal_json(transaction.price),
                'account': transaction.account.name,
            })

        return JsonResponse({
            'instrument': {
                'id': instrument.id,
                'name': instrument.name,
                'ticker': instrument.ticker,
                'isin': instrument.isin,
                'exchange': instrument.exchange,
                'asset_type': instrument.get_asset_type_display(),
                'currency': instrument.currency,
                'price_symbol': instrument.price_symbol,
                'last_price': _decimal_json(instrument.last_price),
                'last_price_at': instrument.last_price_at.isoformat() if instrument.last_price_at else None,
                'market_data_source': instrument.market_data_source or 'Ręcznie',
                'edit_url': reverse('finance:edit_brokerage_instrument', args=[instrument.id]),
            },
            'accounts': accounts,
            'latest_transaction': latest_transaction_payload,
            'chart_transactions': chart_transactions,
            'history': _history_points_json(history_points),
            'history_summary': _history_summary(history_points),
            'history_source': history_source,
            'history_error': history_error,
            'history_range': {
                'start_date': start_date.isoformat(),
                'end_date': end_date.isoformat(),
                'days': selected_days,
                'mode': range_mode,
            },
        })


@method_decorator(login_required, name='dispatch')
class AddBrokerageTransactionView(View):
    def get(self, request):
        return render(request, 'finance/brokerage_form.html', {
            'form': BrokerageTransactionForm(user=request.user),
            'title': 'Dodaj transakcję',
        })

    def post(self, request):
        form = BrokerageTransactionForm(request.POST, user=request.user)
        if form.is_valid():
            if form.market_price_fetched is not None and not form.cleaned_data.get('market_price_confirmed'):
                initial = {
                    'account': form.cleaned_data['account'].id,
                    'transaction_type': form.cleaned_data['transaction_type'],
                    'instrument_name': form.cleaned_data['instrument_name'],
                    'isin': form.cleaned_data['isin'],
                    'asset_type': form.cleaned_data['asset_type'],
                    'currency': form.cleaned_data['currency'],
                    'trade_date': form.cleaned_data['trade_date'],
                    'trade_time': form.cleaned_data['trade_time'],
                    'quantity': form.cleaned_data['quantity'],
                    'price': form.cleaned_data['price'],
                    'fees': form.cleaned_data['fees'],
                    'notes': form.cleaned_data.get('notes', ''),
                    'market_price_confirmed': True,
                    'market_price_value': form.market_price_fetched,
                    'market_price_source_value': form.market_price_source,
                    'market_symbol_value': form.resolved_symbol,
                }
                messages.info(
                    request,
                    f"Pobrano cenę {form.market_price_fetched} z {form.market_price_source}. "
                    "Sprawdź ją z ceną z brokera i zapisz albo wpisz własną cenę.",
                )
                return render(request, 'finance/brokerage_form.html', {
                    'form': BrokerageTransactionForm(user=request.user, initial=initial),
                    'title': 'Dodaj transakcję',
                })
            form.save()
            messages.success(request, 'Transakcja została dodana.')
            return redirect('finance:brokerage')
        return render(request, 'finance/brokerage_form.html', {'form': form, 'title': 'Dodaj transakcję'})


@method_decorator(login_required, name='dispatch')
class EditBrokerageTransactionView(View):
    def get(self, request, transaction_id):
        transaction_obj = get_object_or_404(
            BrokerageTransaction.objects.select_related('account', 'instrument'),
            id=transaction_id,
            account__user=request.user,
        )
        return render(request, 'finance/brokerage_form.html', {
            'form': BrokerageTransactionForm(user=request.user, instance=transaction_obj),
            'title': 'Edytuj transakcję',
        })

    def post(self, request, transaction_id):
        transaction_obj = get_object_or_404(
            BrokerageTransaction.objects.select_related('account', 'instrument'),
            id=transaction_id,
            account__user=request.user,
        )
        form = BrokerageTransactionForm(request.POST, user=request.user, instance=transaction_obj)
        if form.is_valid():
            form.save()
            messages.success(request, 'Transakcja została zaktualizowana.')
            return redirect('finance:brokerage')
        return render(request, 'finance/brokerage_form.html', {'form': form, 'title': 'Edytuj transakcję'})


@method_decorator(login_required, name='dispatch')
class DeleteBrokerageTransactionView(View):
    def post(self, request, transaction_id):
        transaction_obj = get_object_or_404(
            BrokerageTransaction.objects.select_related('account', 'instrument'),
            id=transaction_id,
            account__user=request.user,
        )
        transaction_obj.delete()
        messages.success(request, 'Transakcja została usunięta.')
        return redirect('finance:brokerage')


@method_decorator(login_required, name='dispatch')
class ImportBrokerageTransactionsView(View):
    def get(self, request):
        return render(request, 'finance/brokerage_form.html', {
            'form': BrokerageTransactionImportForm(user=request.user),
            'title': 'Import transakcji z XTB',
        })

    def post(self, request):
        form = BrokerageTransactionImportForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            try:
                result = import_xtb_transactions(
                    request.user,
                    form.cleaned_data['account'],
                    form.cleaned_data['file'],
                )
            except BrokerageImportError as exc:
                form.add_error('file', str(exc))
            else:
                messages.success(
                    request,
                    f'Import XTB zakończony: dodano {result.created}, pominięto duplikatów {result.duplicates}.',
                )
                for warning in result.warnings[:5]:
                    messages.warning(request, warning)
                if len(result.warnings) > 5:
                    messages.warning(request, f'Pozostałe ostrzeżenia: {len(result.warnings) - 5}.')
                return redirect('finance:brokerage')

        return render(request, 'finance/brokerage_form.html', {
            'form': form,
            'title': 'Import transakcji z XTB',
        })


@method_decorator(login_required, name='dispatch')
class AddBrokerageDividendView(View):
    def get(self, request):
        return render(request, 'finance/brokerage_form.html', {
            'form': BrokerageDividendForm(user=request.user),
            'title': 'Dodaj dywidendę',
        })

    def post(self, request):
        form = BrokerageDividendForm(request.POST, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Dywidenda została dodana.')
            return redirect('finance:brokerage')
        return render(request, 'finance/brokerage_form.html', {'form': form, 'title': 'Dodaj dywidendę'})


@method_decorator(login_required, name='dispatch')
class EditBrokerageDividendView(View):
    def get(self, request, dividend_id):
        dividend = get_object_or_404(BrokerageDividend, id=dividend_id, account__user=request.user)
        return render(request, 'finance/brokerage_form.html', {
            'form': BrokerageDividendForm(user=request.user, instance=dividend),
            'title': 'Edytuj dywidendę',
        })

    def post(self, request, dividend_id):
        dividend = get_object_or_404(BrokerageDividend, id=dividend_id, account__user=request.user)
        form = BrokerageDividendForm(request.POST, user=request.user, instance=dividend)
        if form.is_valid():
            form.save()
            messages.success(request, 'Dywidenda została zaktualizowana.')
            return redirect('finance:brokerage')
        return render(request, 'finance/brokerage_form.html', {'form': form, 'title': 'Edytuj dywidendę'})


@method_decorator(login_required, name='dispatch')
class DeleteBrokerageDividendView(View):
    def post(self, request, dividend_id):
        dividend = get_object_or_404(BrokerageDividend, id=dividend_id, account__user=request.user)
        dividend.delete()
        messages.success(request, 'Dywidenda została usunięta.')
        return redirect('finance:brokerage')


@method_decorator(login_required, name='dispatch')
class DashboardView(View):
    def get(self, request):
        active_account = get_active_finance_account(request)
        real_today = timezone.now().date()
        selected_month_str = request.GET.get('month')

        if selected_month_str:
            try:
                year, month = map(int, selected_month_str.split('-'))
                current_month_date = date(year, month, 1)
            except ValueError:
                current_month_date = month_start(real_today)
        else:
            current_month_date = month_start(real_today)

        available_months = Monthly.objects.filter(account=active_account).order_by('-date')
        monthly_record, _ = get_or_create_monthly_record(
            user=request.user,
            account=active_account,
            month_date=current_month_date,
        )

        days_in_month_count = calendar.monthrange(current_month_date.year, current_month_date.month)[1]
        if current_month_date.year == real_today.year and current_month_date.month == real_today.month:
            days_passed = real_today.day
        elif current_month_date < month_start(real_today):
            days_passed = days_in_month_count
        else:
            days_passed = 0

        days_in_month = list(range(1, days_in_month_count + 1))
        daily_expenses_data = [0.0] * days_in_month_count
        daily_investments_data = [0.0] * days_in_month_count
        daily_incomes_data = [0.0] * days_in_month_count

        regular_daily_cost = (
            Daily.objects
            .filter(account=active_account, month=monthly_record)
            .exclude(category=INVESTMENT_CATEGORY)
            .values('date')
            .annotate(cost=Sum('cost'))
            .order_by('-date')
        )
        investment_daily_cost = (
            Daily.objects
            .filter(account=active_account, month=monthly_record, category=INVESTMENT_CATEGORY)
            .values('date')
            .annotate(cost=Sum('cost'))
            .order_by('-date')
        )
        daily_incomes = (
            Income.objects
            .filter(account=active_account, month=monthly_record)
            .values('date')
            .annotate(income=Sum('amount'))
            .order_by('-date')
        )

        for record in regular_daily_cost:
            daily_expenses_data[record['date'].day - 1] = float(record['cost'] or 0.0)
        for record in investment_daily_cost:
            daily_investments_data[record['date'].day - 1] = float(record['cost'] or 0.0)
        for record in daily_incomes:
            daily_incomes_data[record['date'].day - 1] = float(record['income'] or 0.0)

        investment_total = (
            Daily.objects.filter(account=active_account, month=monthly_record, category=INVESTMENT_CATEGORY)
            .aggregate(Sum('cost'))['cost__sum'] or 0
        )
        spending_total = monthly_record.total_expense - investment_total

        expenses_by_category = (
            Daily.objects.filter(account=active_account, month=monthly_record)
            .exclude(category=INVESTMENT_CATEGORY)
            .values('category')
            .annotate(total=Sum('cost'))
            .order_by('-total')
        )
        categories = [item['category'] for item in expenses_by_category]
        amounts = [float(item['total']) for item in expenses_by_category]
        available_expense_categories = get_available_expense_categories(active_account)

        requested_cost_categories = request.GET.getlist('cost_category')
        if requested_cost_categories:
            selected_cost_categories = [
                category for category in requested_cost_categories if category in available_expense_categories
            ]
        else:
            selected_cost_categories = [
                category for category in COST_OF_LIVING_CATEGORIES if category in available_expense_categories
            ]

        income_by_source = (
            Income.objects.filter(account=active_account, month=monthly_record)
            .values('source')
            .annotate(total=Sum('amount'))
            .order_by('-total')
        )
        income_sources = [item['source'] for item in income_by_source]
        income_amounts = [float(item['total']) for item in income_by_source]
        balance = monthly_record.total_income - monthly_record.total_expense
        recent_incomes = Income.objects.filter(account=active_account, month=monthly_record).order_by('-date')[:5]
        recent_expenses = Daily.objects.filter(
            account=active_account,
            month=monthly_record,
        ).exclude(category=INVESTMENT_CATEGORY).order_by('-date')[:5]
        recent_investments = Daily.objects.filter(
            account=active_account,
            month=monthly_record,
            category=INVESTMENT_CATEGORY,
        ).order_by('-date')[:5]

        savings_rate = 0
        if monthly_record.total_income > 0:
            savings_rate = ((monthly_record.total_income - monthly_record.total_expense) / monthly_record.total_income) * 100

        past_days_expenses = daily_expenses_data[:days_passed]
        adjusted_daily_avg = 0.0
        if days_passed > 0:
            sorted_expenses = sorted(past_days_expenses)
            cutoff = 0
            if days_passed > 5:
                cutoff = max(1, int(days_passed * 0.15))
            normal_days = sorted_expenses[:-cutoff] if cutoff > 0 else sorted_expenses
            if normal_days:
                adjusted_daily_avg = sum(normal_days) / len(normal_days)

        if selected_cost_categories:
            selected_category_total = Daily.objects.filter(
                account=active_account,
                month=monthly_record,
                category__in=selected_cost_categories,
            ).aggregate(Sum('cost'))['cost__sum'] or 0
        else:
            selected_category_total = 0

        projected_expense = float(spending_total) + (adjusted_daily_avg * (days_in_month_count - days_passed))

        context = {
            'current_month': monthly_record.date,
            'current_month_filter': current_month_date.strftime('%Y-%m'),
            'months': available_months,
            'total_income': monthly_record.total_income,
            'total_expense': monthly_record.total_expense,
            'spending_total': spending_total,
            'investment_total': investment_total,
            'balance': balance,
            'categories': categories,
            'amounts': amounts,
            'income_sources': income_sources,
            'income_amounts': income_amounts,
            'recent_expenses': recent_expenses,
            'recent_investments': recent_investments,
            'recent_incomes': recent_incomes,
            'days_in_month': days_in_month,
            'daily_expenses_data': daily_expenses_data,
            'daily_investments_data': daily_investments_data,
            'daily_incomes_data': daily_incomes_data,
            'savings_rate': savings_rate,
            'daily_average': adjusted_daily_avg,
            'projected_expense': projected_expense,
            'selected_category_total': selected_category_total,
            'available_expense_categories': available_expense_categories,
            'selected_cost_categories': selected_cost_categories,
        }
        return render(request, 'finance/dashboard.html', context)


@method_decorator(login_required, name='dispatch')
class ExpenseListView(View):
    def get(self, request):
        active_account = get_active_finance_account(request)
        per_page_view = 10
        month_filter = request.GET.get('month', '')
        category_filter = request.GET.get('category', '')
        date_filter = request.GET.get('date', '')
        specific_date = None

        records = Daily.objects.filter(account=active_account).select_related('month', 'transfer_target_account')

        if month_filter:
            try:
                month_date = datetime.strptime(month_filter, '%Y-%m').date()
                month_obj = Monthly.objects.filter(
                    account=active_account,
                    date__year=month_date.year,
                    date__month=month_date.month,
                ).first()
                if month_obj:
                    records = records.filter(month=month_obj)
            except ValueError:
                pass

        if date_filter:
            try:
                specific_date = parse_date_input(date_filter)
                records = records.filter(date=specific_date)
            except ValueError:
                pass

        if category_filter == 'Koszty zycia':
            regular_expenses = records.filter(category__in=COST_OF_LIVING_CATEGORIES).exclude(category=INVESTMENT_CATEGORY)
            investments = records.none()
        elif category_filter == INVESTMENT_CATEGORY:
            regular_expenses = records.none()
            investments = get_investment_queryset(records)
        elif category_filter:
            regular_expenses = records.filter(category=category_filter).exclude(category=INVESTMENT_CATEGORY)
            investments = records.none()
        else:
            regular_expenses = get_non_investment_queryset(records)
            investments = get_investment_queryset(records)

        regular_expenses = regular_expenses.order_by('-date', 'title')
        investments = investments.order_by('-date', 'title')
        total_filtered = regular_expenses.aggregate(Sum('cost'))['cost__sum'] or 0
        investment_total_filtered = investments.aggregate(Sum('cost'))['cost__sum'] or 0
        categories = list(
            Daily.objects.filter(account=active_account).order_by('category').values_list('category', flat=True).distinct()
        )
        if 'Koszty zycia' not in categories:
            categories.append('Koszty zycia')
        months = Monthly.objects.filter(account=active_account).order_by('-date')

        expenses_page = Paginator(regular_expenses, per_page_view).get_page(request.GET.get('page'))
        qs = request.GET.copy()
        qs.pop('page', None)
        querystring = qs.urlencode()

        context = {
            'expenses': expenses_page,
            'investments': investments,
            'categories': categories,
            'months': months,
            'current_month_filter': month_filter,
            'current_category_filter': category_filter,
            'current_date_filter': specific_date if date_filter else '',
            'total_filtered': total_filtered,
            'investment_total_filtered': investment_total_filtered,
            'today': timezone.now().date(),
            'querystring': querystring,
            'transfer_category': TRANSFER_TO_SHARED_CATEGORY,
            'investment_category': INVESTMENT_CATEGORY,
        }
        return render(request, 'finance/expense_list.html', context)


@method_decorator(login_required, name='dispatch')
class AddExpenseView(View):
    def get(self, request):
        active_account = get_active_finance_account(request)
        return render(request, 'finance/add_expense.html', get_expense_form_context(request, active_account))

    @transaction.atomic
    def post(self, request):
        active_account = get_active_finance_account(request)
        try:
            expense_date = parse_date_input(request.POST.get('date'))
            title = request.POST.get('title')
            category = request.POST.get('category')
            store = request.POST.get('store', '')
            cost = parse_decimal(request.POST.get('cost'))
            transfer_target_account = get_selected_transfer_target(request, active_account, category)

            if cost <= 0:
                raise ValueError("Kwota musi być większa od 0")

            monthly_record, _ = get_or_create_monthly_record(
                user=request.user,
                account=active_account,
                month_date=month_start(expense_date),
                for_update=True,
            )

            expense = Daily.objects.create(
                user=request.user,
                account=active_account,
                date=expense_date,
                title=title,
                category=category,
                store=store,
                cost=cost,
                month=monthly_record,
                transfer_target_account=transfer_target_account,
            )

            recalculate_monthly_record(monthly_record)
            sync_shared_account_transfer(expense)

            messages.success(request, 'Wydatek został dodany pomyślnie!')
            return redirect('finance:expense_list')
        except Exception as exc:
            messages.error(request, f'Błąd podczas dodawania wydatku: {exc}')
            context = get_expense_form_context(
                request,
                active_account,
                form_values=request.POST,
            )
            return render(request, 'finance/add_expense.html', context)


@method_decorator(login_required, name='dispatch')
class ImportBankTransactionsView(View):
    template_name = 'finance/import_bank_transactions.html'

    def _context(self, request, active_account, **extra_context):
        expense_categories = [
            category
            for category in get_available_expense_categories(active_account)
            if category != TRANSFER_TO_SHARED_CATEGORY
        ]
        income_sources = sorted(
            set(INCOME_SOURCES).union(
                Income.objects
                .filter(account=active_account)
                .exclude(source='')
                .values_list('source', flat=True)
            )
        )
        preview = extra_context.get('preview')
        if preview:
            expense_categories = sorted(set(expense_categories).union(
                candidate.category
                for candidate in preview.candidates
                if candidate.is_expense and candidate.category
            ))
            income_sources = sorted(set(income_sources).union(
                candidate.source
                for candidate in preview.candidates
                if candidate.is_income and candidate.source
            ))
        context = {
            'form': BankTransactionImportForm(),
            'expense_categories': expense_categories,
            'income_sources': income_sources,
        }
        context.update(extra_context)
        return context

    def get(self, request):
        active_account = get_active_finance_account(request)
        return render(request, self.template_name, self._context(request, active_account))

    def post(self, request):
        active_account = get_active_finance_account(request)
        if request.POST.get('confirm_import') == '1':
            try:
                result = import_candidates_from_post(request.user, active_account, request.POST)
            except BankImportError as exc:
                messages.error(request, str(exc))
                return redirect('finance:import_bank_transactions')

            messages.success(
                request,
                f'Import zakończony: dodano {result.created_expenses} wydatków i '
                f'{result.created_incomes} przychodów, pominięto duplikatów {result.duplicates}.',
            )
            for warning in result.warnings[:5]:
                messages.warning(request, warning)
            if len(result.warnings) > 5:
                messages.warning(request, f'Pozostałe ostrzeżenia: {len(result.warnings) - 5}.')
            return redirect('finance:dashboard')

        form = BankTransactionImportForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                preview = parse_bank_csv(form.cleaned_data['file'], active_account)
            except BankImportError as exc:
                form.add_error('file', str(exc))
            else:
                for warning in preview.warnings[:5]:
                    messages.warning(request, warning)
                if len(preview.warnings) > 5:
                    messages.warning(request, f'Pozostałe ostrzeżenia: {len(preview.warnings) - 5}.')
                return render(
                    request,
                    self.template_name,
                    self._context(request, active_account, preview=preview),
                )

        return render(request, self.template_name, self._context(request, active_account, form=form))


@method_decorator(login_required, name='dispatch')
class EditExpenseView(View):
    def get(self, request, expense_id):
        active_account = get_active_finance_account(request)
        expense = get_object_or_404(Daily, id=expense_id, account=active_account)
        querystring = request.GET.urlencode()
        context = get_expense_form_context(
            request,
            active_account,
            expense=expense,
            querystring=querystring,
        )
        return render(request, 'finance/edit_expense.html', context)

    @transaction.atomic
    def post(self, request, expense_id):
        active_account = get_active_finance_account(request)
        expense = get_object_or_404(Daily, id=expense_id, account=active_account)
        old_monthly = expense.month
        querystring = request.POST.get('querystring', '')

        try:
            expense.date = parse_date_input(request.POST.get('date'))
            expense.title = request.POST.get('title')
            expense.category = request.POST.get('category')
            expense.store = request.POST.get('store', '')
            expense.cost = parse_decimal(request.POST.get('cost'))
            expense.transfer_target_account = get_selected_transfer_target(request, active_account, expense.category)

            if expense.cost <= 0:
                raise ValueError("Kwota musi być większa od 0")

            new_month_date = month_start(expense.date)
            if old_monthly.date != new_month_date:
                new_monthly, _ = get_or_create_monthly_record(
                    user=request.user,
                    account=active_account,
                    month_date=new_month_date,
                    for_update=True,
                )
                expense.month = new_monthly
            expense.save()

            months_to_recalc = {expense.month, old_monthly}
            for monthly in months_to_recalc:
                recalculate_monthly_record(monthly)

            sync_shared_account_transfer(expense)

            messages.success(request, 'Wydatek został zaktualizowany!')
            redirect_url = reverse('finance:expense_list')
            if querystring:
                redirect_url += f'?{querystring}'
            return redirect(redirect_url)
        except Exception as exc:
            messages.error(request, f'Błąd podczas aktualizacji: {exc}')
            context = get_expense_form_context(
                request,
                active_account,
                expense=expense,
                querystring=querystring,
            )
            return render(request, 'finance/edit_expense.html', context)


@method_decorator(login_required, name='dispatch')
class DeleteExpenseView(View):
    @transaction.atomic
    def post(self, request, expense_id):
        active_account = get_active_finance_account(request)
        expense = get_object_or_404(Daily, id=expense_id, account=active_account)
        monthly_record = expense.month
        linked_income = getattr(expense, 'linked_shared_income', None)
        target_month = linked_income.month if linked_income else None
        expense_title = expense.title
        expense.delete()

        recalculate_monthly_record(monthly_record)
        if target_month:
            recalculate_monthly_record(target_month)

        messages.success(request, f'Wydatek "{expense_title}" został usunięty!')
        return redirect('finance:expense_list')


@method_decorator(login_required, name='dispatch')
class IncomeListView(View):
    def get(self, request):
        active_account = get_active_finance_account(request)
        month_filter = request.GET.get('month', '')
        source_filter = request.GET.get('source', '')

        incomes = Income.objects.filter(account=active_account).select_related('month', 'linked_expense')

        if month_filter:
            try:
                month_date = datetime.strptime(month_filter, '%Y-%m').date()
                month_obj = Monthly.objects.filter(
                    account=active_account,
                    date__year=month_date.year,
                    date__month=month_date.month,
                ).first()
                if month_obj:
                    incomes = incomes.filter(month=month_obj)
            except ValueError:
                pass

        if source_filter:
            incomes = incomes.filter(source=source_filter)

        incomes = incomes.order_by('-date')
        total_filtered = incomes.aggregate(Sum('amount'))['amount__sum'] or 0
        page_obj = Paginator(incomes, 10).get_page(request.GET.get('page'))

        querystring = request.GET.copy()
        querystring.pop('page', None)

        sources = Income.objects.filter(account=active_account).order_by('source').values_list('source', flat=True).distinct()
        months = Monthly.objects.filter(account=active_account).order_by('-date')

        context = {
            'incomes': page_obj,
            'page_obj': page_obj,
            'sources': sources,
            'months': months,
            'current_month_filter': month_filter,
            'current_source_filter': source_filter,
            'total_filtered': total_filtered,
            'querystring': querystring.urlencode(),
            'transfer_income_source': TRANSFER_INCOME_SOURCE,
        }
        return render(request, 'finance/income_list.html', context)


@method_decorator(login_required, name='dispatch')
class AddIncomeView(View):
    def get(self, request):
        context = {
            'default_date': timezone.now().date(),
            'income_sources': INCOME_SOURCES,
        }
        return render(request, 'finance/add_income.html', context)

    @transaction.atomic
    def post(self, request):
        active_account = get_active_finance_account(request)
        try:
            date_value = request.POST.get('date')
            title = request.POST.get('title')
            amount = parse_decimal(request.POST.get('amount'))
            source = request.POST.get('source')
            if amount <= 0:
                raise ValueError('Kwota musi być większa od 0')

            income_date = parse_date_input(date_value)
            monthly_record, _ = get_or_create_monthly_record(
                user=request.user,
                account=active_account,
                month_date=month_start(income_date),
                for_update=True,
            )

            Income.objects.create(
                user=request.user,
                account=active_account,
                date=income_date,
                title=title,
                amount=amount,
                source=source,
                month=monthly_record,
            )

            recalculate_monthly_record(monthly_record)
            messages.success(request, f'Przychód "{title}" ({amount} zł) został dodany!')
            return redirect('finance:income_list')
        except Exception as exc:
            messages.error(request, f'Błąd: {exc}')
            context = {
                'default_date': timezone.now().date(),
                'income_sources': INCOME_SOURCES,
                'form_values': request.POST,
            }
            return render(request, 'finance/add_income.html', context)


@method_decorator(login_required, name='dispatch')
class EditIncomeView(View):
    def get(self, request, income_id):
        active_account = get_active_finance_account(request)
        income = get_object_or_404(Income, id=income_id, account=active_account)
        if income.linked_expense_id:
            messages.warning(request, 'Ten przychód jest zasileniem konta wspólnego. Edytuj wydatek źródłowy.')
            return redirect('finance:income_list')

        context = {
            'income': income,
            'income_sources': INCOME_SOURCES,
        }
        return render(request, 'finance/edit_income.html', context)

    @transaction.atomic
    def post(self, request, income_id):
        active_account = get_active_finance_account(request)
        income = get_object_or_404(Income, id=income_id, account=active_account)
        if income.linked_expense_id:
            messages.warning(request, 'Ten przychód jest zasileniem konta wspólnego. Edytuj wydatek źródłowy.')
            return redirect('finance:income_list')

        try:
            old_monthly = income.month
            income.date = parse_date_input(request.POST.get('date'))
            income.title = request.POST.get('title')
            income.source = request.POST.get('source')
            income.amount = parse_decimal(request.POST.get('amount'))
            if income.amount <= 0:
                raise ValueError('Kwota musi być większa od 0')

            new_month_date = month_start(income.date)
            if old_monthly.date != new_month_date:
                new_monthly, _ = get_or_create_monthly_record(
                    user=request.user,
                    account=active_account,
                    month_date=new_month_date,
                    for_update=True,
                )
                income.month = new_monthly
            income.save()

            for monthly in {income.month, old_monthly}:
                recalculate_monthly_record(monthly)

            messages.success(request, 'Przychód został zaktualizowany!')
            return redirect('finance:income_list')
        except Exception as exc:
            messages.error(request, f'Błąd podczas aktualizacji: {exc}')
            return render(request, 'finance/edit_income.html', {'income': income, 'income_sources': INCOME_SOURCES})


@method_decorator(login_required, name='dispatch')
class DeleteIncomeView(View):
    @transaction.atomic
    def post(self, request, income_id):
        active_account = get_active_finance_account(request)
        income = get_object_or_404(Income, id=income_id, account=active_account)
        if income.linked_expense_id:
            messages.warning(request, 'Ten przychód jest zasileniem konta wspólnego. Usuń lub edytuj wydatek źródłowy.')
            return redirect('finance:income_list')

        monthly_record = income.month
        income_title = income.title
        income.delete()
        recalculate_monthly_record(monthly_record)

        messages.success(request, f'Przychód "{income_title}" został usunięty!')
        return redirect('finance:income_list')


@method_decorator(login_required, name='dispatch')
class ReportsView(View):
    def get(self, request):
        active_account = get_active_finance_account(request)
        monthly_records = Monthly.objects.filter(account=active_account).order_by('-date')[:6]

        months_labels = []
        income_data = []
        expense_data = []
        investment_data = []
        monthly_balance = []

        for record in reversed(monthly_records):
            record.investment_total = (
                Daily.objects.filter(account=active_account, month=record, category=INVESTMENT_CATEGORY)
                .aggregate(Sum('cost'))['cost__sum'] or 0
            )
            record.spending_total = record.total_expense - record.investment_total
            months_labels.append(record.date.strftime('%B %Y'))
            income_data.append(float(record.total_income))
            expense_data.append(float(record.spending_total))
            investment_data.append(float(record.investment_total))
            monthly_balance.append(float(record.total_income - record.total_expense))
            record.monthly_balance = record.total_income - record.total_expense

        total_income_all = Monthly.objects.filter(account=active_account).aggregate(Sum('total_income'))['total_income__sum'] or 0
        total_expense_all = Monthly.objects.filter(account=active_account).aggregate(Sum('total_expense'))['total_expense__sum'] or 0
        total_investment_all = (
            Daily.objects.filter(account=active_account, category=INVESTMENT_CATEGORY)
            .aggregate(Sum('cost'))['cost__sum'] or 0
        )
        total_spending_all = total_expense_all - total_investment_all
        top_categories = (
            Daily.objects.filter(account=active_account)
            .exclude(category=INVESTMENT_CATEGORY)
            .values('category')
            .annotate(total=Sum('cost'))
            .order_by('-total')[:5]
        )

        context = {
            'monthly_records': monthly_records,
            'months_labels': months_labels,
            'monthly_balance': monthly_balance,
            'income_data': income_data,
            'expense_data': expense_data,
            'investment_data': investment_data,
            'total_income_all': total_income_all,
            'total_spending_all': total_spending_all,
            'total_investment_all': total_investment_all,
            'total_expense_all': total_expense_all,
            'balance_all': total_income_all - total_expense_all,
            'top_categories': top_categories,
        }
        return render(request, 'finance/reports.html', context)


@method_decorator(login_required, name='dispatch')
class TravelView(View):
    def get(self, request):
        country_filter = request.GET.get('country', '')
        travel_type_filter = request.GET.get('travel_type', '')
        valid_travel_types = {choice[0] for choice in TravelDestinations.TRAVEL_TYPE_CHOICES}
        base_destinations = TravelDestinations.objects.filter(user=request.user)
        country_objs = []
        distinct_countries = base_destinations.order_by('country').values_list('country', flat=True).distinct()
        for code in distinct_countries:
            name = dict(django_countries).get(code, code)
            country_objs.append({'code': code, 'name': name})
        country_objs = sorted(country_objs, key=lambda country: country['name'])

        destinations = base_destinations
        if country_filter:
            destinations = destinations.filter(country=country_filter)
        if travel_type_filter in valid_travel_types:
            destinations = destinations.filter(travel_type=travel_type_filter)
        else:
            travel_type_filter = ''

        today = timezone.localdate()
        destination_list = [
            _attach_travel_display_meta(destination, today)
            for destination in destinations.order_by('-start_date')
        ]
        for destination in destination_list:
            if not destination.has_coordinates:
                if _populate_destination_coordinates_for_user(destination, request.user):
                    destination.save(update_fields=['latitude', 'longitude'])
                break

        location_groups = _group_travel_locations(destination_list)
        total_budget = sum((destination.budget for destination in destination_list), Decimal('0.00'))
        total_days = sum(destination.duration_days for destination in destination_list)
        business_days = _delegation_days_count(destination_list)
        leisure_days = sum(destination.duration_days for destination in destination_list if not destination.is_business_trip)
        travel_map_points = [
            point
            for point in (_travel_map_point(location_group) for location_group in location_groups)
            if point is not None
        ]
        missing_coordinates_count = len(location_groups) - len(travel_map_points)
        delegation_year_stats = _delegation_year_stats([
            _attach_travel_display_meta(destination, today)
            for destination in base_destinations.order_by('-start_date')
        ], today)

        paginator = Paginator(location_groups, 10)
        page_number = request.GET.get('page')
        paged_location_groups = paginator.get_page(page_number)

        qs = request.GET.copy()
        qs.pop('page', None)
        querystring = qs.urlencode()

        context = {
            'countries': country_objs,
            'location_groups': paged_location_groups,
            'querystring': querystring,
            'current_country_filter': country_filter,
            'current_travel_type_filter': travel_type_filter,
            'travel_type_options': [
                {'value': value, 'label': label}
                for value, label in TravelDestinations.TRAVEL_TYPE_CHOICES
            ],
            'travel_map_points': travel_map_points,
            'missing_coordinates_count': missing_coordinates_count,
            'delegation_year_stats': delegation_year_stats,
            'travel_stats': {
                'total_count': len(destination_list),
                'locations_count': len(location_groups),
                'countries_count': len({destination.country.code for destination in destination_list}),
                'total_days': total_days,
                'business_days': business_days,
                'leisure_days': leisure_days,
                'total_budget': total_budget,
                'average_budget_per_day': total_budget / Decimal(total_days) if total_days else Decimal('0.00'),
            },
        }
        return render(request, 'finance/travel.html', context=context)


@method_decorator(login_required, name='dispatch')
class AddTravelView(View):
    def get(self, request):
        form = TravelDestinationForm()
        return render(request, 'finance/add_travel.html', {'form': form})

    @transaction.atomic
    def post(self, request):
        try:
            form = TravelDestinationForm(request.POST)
            if form.is_valid():
                travel_destination = form.save(commit=False)
                travel_destination.user = request.user
                _populate_destination_coordinates_for_user(travel_destination, request.user, force=True)
                travel_destination.save()

                messages.success(request, 'Nowa podróż została dodana pomyślnie!')
                return redirect('finance:travels')
            messages.error(request, 'Formularz zawiera błędy. Proszę poprawić i spróbować ponownie.')
        except Exception as exc:
            messages.error(request, f'Błąd podczas dodawania podróży: {exc}')
        return render(request, 'finance/add_travel.html', {'form': form})


@method_decorator(login_required, name='dispatch')
class EditTravelView(View):
    def get(self, request, travel_id):
        travel = get_object_or_404(TravelDestinations, id=travel_id, user=request.user)
        form = TravelDestinationForm(instance=travel)
        return render(request, 'finance/edit_travel.html', {'form': form, 'travel': travel})

    @transaction.atomic
    def post(self, request, travel_id):
        travel = get_object_or_404(TravelDestinations, id=travel_id, user=request.user)
        try:
            form = TravelDestinationForm(request.POST, instance=travel)
            if form.is_valid():
                travel_destination = form.save(commit=False)
                should_refresh_coordinates = (
                    'country' in form.changed_data
                    or 'city' in form.changed_data
                    or not travel_destination.has_coordinates
                )
                if should_refresh_coordinates:
                    _populate_destination_coordinates_for_user(travel_destination, request.user, force=True)
                travel_destination.save()
                messages.success(request, 'Podróż została zaktualizowana pomyślnie!')
                return redirect('finance:travels')
            messages.error(request, 'Formularz zawiera błędy. Proszę poprawić i spróbować ponownie.')
        except Exception as exc:
            messages.error(request, f'Błąd podczas aktualizacji podróży: {exc}')
        return render(request, 'finance/edit_travel.html', {'form': form, 'travel': travel})


@method_decorator(login_required, name='dispatch')
class DeleteTravelView(View):
    def get(self, request, travel_id):
        travel = get_object_or_404(TravelDestinations, id=travel_id, user=request.user)
        return render(request, 'finance/delete_travel.html', {'travel': travel})

    @transaction.atomic
    def post(self, request, travel_id):
        travel = get_object_or_404(TravelDestinations, id=travel_id, user=request.user)
        travel_name = f"{travel.city}, {travel.country}"
        travel.delete()

        messages.success(request, f'Podróż "{travel_name}" została usunięta!')
        return redirect('finance:travels')


class DailyRecordAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        active_account = get_active_finance_account(request)
        records = Daily.objects.filter(account=active_account).values(
            'id', 'date', 'title', 'cost', 'store', 'category'
        )
        return Response(list(records))

    def post(self, request):
        active_account = get_active_finance_account(request)
        data = request.data
        try:
            expense_date = parse_date_input(data['date'])
            category = data.get('category', 'Inne')
            transfer_target_account = None
            if category == TRANSFER_TO_SHARED_CATEGORY and active_account.account_type == FinanceAccount.PERSONAL:
                transfer_target_account = get_available_shared_accounts(request.user).filter(
                    id=data.get('transfer_target_account')
                ).first()
                if transfer_target_account is None:
                    raise ValueError('Wybrano przelew na konto wspólne bez poprawnego konta docelowego.')
            cost = parse_decimal(data['cost'])
            if cost <= 0:
                raise ValueError('Kwota musi być większa od 0')

            monthly_record, _ = get_or_create_monthly_record(
                user=request.user,
                account=active_account,
                month_date=expense_date.replace(day=1),
            )

            daily_record = Daily.objects.create(
                user=request.user,
                account=active_account,
                date=expense_date,
                title=data['title'],
                category=category,
                store=data.get('store', ''),
                cost=cost,
                month=monthly_record,
                transfer_target_account=transfer_target_account,
            )

            recalculate_monthly_record(monthly_record)
            sync_shared_account_transfer(daily_record)

            return Response({'status': 'success', 'id': daily_record.id})
        except Exception as exc:
            return Response({'status': 'error', 'message': str(exc)}, status=400)


class MonthlyRecordAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        active_account = get_active_finance_account(request)
        records = Monthly.objects.filter(account=active_account).values(
            'id', 'date', 'total_income', 'total_expense'
        )
        return Response(list(records))
