import calendar
import json
from datetime import date, datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Sum
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
from .forms import TravelDestinationForm
from .models import Daily, FinanceAccount, Income, Monthly, TravelDestinations

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
            'categories': json.dumps(categories) if categories else json.dumps([]),
            'amounts': json.dumps(amounts) if amounts else json.dumps([]),
            'income_sources': json.dumps(income_sources) if income_sources else json.dumps([]),
            'income_amounts': json.dumps(income_amounts) if income_amounts else json.dumps([]),
            'recent_expenses': recent_expenses,
            'recent_investments': recent_investments,
            'recent_incomes': recent_incomes,
            'days_in_month': json.dumps(days_in_month),
            'daily_expenses_data': json.dumps(daily_expenses_data),
            'daily_investments_data': json.dumps(daily_investments_data),
            'daily_incomes_data': json.dumps(daily_incomes_data),
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
        date_value = request.POST.get('date')
        title = request.POST.get('title')
        amount = parse_decimal(request.POST.get('amount'))
        source = request.POST.get('source')

        try:
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

        old_monthly = income.month
        income.date = parse_date_input(request.POST.get('date'))
        income.title = request.POST.get('title')
        income.source = request.POST.get('source')
        income.amount = parse_decimal(request.POST.get('amount'))

        try:
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
            'months_labels': json.dumps(months_labels),
            'monthly_balance': json.dumps(monthly_balance),
            'income_data': json.dumps(income_data),
            'expense_data': json.dumps(expense_data),
            'investment_data': json.dumps(investment_data),
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
        destinations = TravelDestinations.objects.filter(user=request.user)
        country_objs = []
        distinct_countries = destinations.order_by('country').values_list('country', flat=True).distinct()
        for code in distinct_countries:
            name = dict(django_countries).get(code, code)
            country_objs.append({'code': code, 'name': name})

        if country_filter:
            destinations = destinations.filter(country=country_filter)

        destinations = destinations.order_by('-start_date')
        paginator = Paginator(destinations, 10)
        page_number = request.GET.get('page')
        destinations = paginator.get_page(page_number)

        qs = request.GET.copy()
        qs.pop('page', None)
        querystring = qs.urlencode()

        context = {
            'countries': country_objs,
            'destinations': destinations,
            'querystring': querystring,
            'current_country_filter': country_filter,
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
                travel_destination.save()

                messages.success(request, 'Nowa podróz została dodana pomyślnie!')
                return redirect('finance:travels')
            messages.error(request, 'Formularz zawiera błędy. Proszę poprawić i spróbować ponownie.')
        except Exception as exc:
            messages.error(request, f'Błąd podczas dodawania wydatku podróży: {exc}')
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
                form.save()
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
                cost=parse_decimal(data['cost']),
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
