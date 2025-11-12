from django.shortcuts import render, redirect, get_object_or_404
from django.core.paginator import Paginator
from django.views import View
from django.db.models import Sum
from django.contrib import messages
from django.utils import timezone
from django.db import transaction
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator
from django_countries import countries as django_countries
from datetime import datetime
import calendar
from .models import Monthly, Daily, Income, TravelDestinations
from .forms import TravelDestinationForm
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import serializers
from utils.tools import month_start, parse_decimal
import json

CATEGORIES_EXPENSES = [
        'Zakupy spozywcze', 'Jedzenie na miescie', 'Transport miejski',
        'Rozrywka', 'Podroze - nocleg', 'Podroze - jedzenie', 
        'Podroze - atrakcje', 'Podroze - pamiatki', 'Podroze - transport',
        'Paliwo', 'Rachunki', 'Zdrowie', 'Edukacja', 'Rodzice', 'Ubrania', 
        'Delegacje', 'Inwestycje', 'Inne'
]

INCOME_SOURCES = [
        'Pensja', 'Premia', 'Dieta', 'Inwestycje',
        'Zwrot podatku', 'Sprzedaż', 'Rodzina', 'Inne'
]

@login_required
def index(request):
    return render(request, 'finance/index.html')

@method_decorator(login_required, name='dispatch')
class DashboardView(View):
    def get(self, request):
        current_date = timezone.now().date()
        current_month_date = month_start(current_date)
        
        monthly_record, created = Monthly.objects.get_or_create(
            user=request.user,
            date=current_month_date,
            defaults={'total_income': 0, 'total_expense': 0}
        )
        
        days = calendar.monthrange(current_month_date.year, current_month_date.month)[1]
        days_in_month = [i for i in range(1, days+1)]
        daily_expenses_data = [0.0] * days
        daily_incomes_data = [0.0] * days
        daily_cost = (
            Daily.objects
            .filter(user=request.user,month=monthly_record)
            .values('date')
            .annotate(cost=Sum('cost'))
            .order_by('-date')
        )
        daily_incomes = (
            Income.objects
            .filter(user=request.user,month=monthly_record)
            .values('date')
            .annotate(income=Sum('amount'))
            .order_by('-date')
        )
        
        for record in daily_cost:
            day = record['date'].day
            daily_expenses_data[day-1] = float(record['cost'] or 0.0)

        for record in daily_incomes:
            day = record['date'].day
            daily_incomes_data[day-1] = float(record['income'] or 0.0)

        expenses_by_category = Daily.objects.filter(
            user=request.user,
            month=monthly_record
        ).values('category').annotate(
            total=Sum('cost')
        ).order_by('-total')
        
        categories = [item['category'] for item in expenses_by_category]
        amounts = [float(item['total']) for item in expenses_by_category]
        
        income_by_source = Income.objects.filter(
            user=request.user,
            month=monthly_record
        ).values('source').annotate(
            total=Sum('amount')
        ).order_by('-total')
        
        income_sources = [item['source'] for item in income_by_source]
        income_amounts = [float(item['total']) for item in income_by_source]
        
        balance = monthly_record.total_income - monthly_record.total_expense
        
        recent_incomes = Income.objects.filter(user=request.user, month=monthly_record).order_by('-date')[:5]
        
        context = {
            'current_month': monthly_record.date,
            'total_income': monthly_record.total_income,
            'total_expense': monthly_record.total_expense,
            'balance': balance,
            'categories': json.dumps(categories) if categories else json.dumps([]),
            'amounts': json.dumps(amounts) if amounts else json.dumps([]),
            'income_sources': json.dumps(income_sources) if income_sources else json.dumps([]),
            'income_amounts': json.dumps(income_amounts) if income_amounts else json.dumps([]),
            'recent_expenses': Daily.objects.filter(user=request.user, month=monthly_record).order_by('-date')[:5],
            'recent_incomes': recent_incomes,
            'days_in_month': json.dumps(days_in_month),
            'daily_expenses_data': json.dumps(daily_expenses_data),
            'daily_incomes_data': json.dumps(daily_incomes_data),
        }
        
        return render(request, 'finance/dashboard.html', context)

@method_decorator(login_required, name='dispatch')
class ExpenseListView(View):
    def get(self, request):
        per_page_view = 10
        month_filter = request.GET.get('month', '')
        category_filter = request.GET.get('category', '')
        date_filter = request.GET.get('date', '')

        expenses = Daily.objects.filter(user=request.user).select_related('month')
        
        if month_filter:
            try:
                month_date = datetime.strptime(month_filter, '%Y-%m').date()
                month_obj = Monthly.objects.filter(
                    user=request.user,
                    date__year=month_date.year, 
                    date__month=month_date.month
                ).first()
                if month_obj:
                    expenses = expenses.filter(month=month_obj)
            except ValueError:
                pass
        
        if category_filter:
            expenses = expenses.filter(category=category_filter)
        
        if date_filter:
            specific_date = datetime.strptime(date_filter, '%Y-%m-%d').date()
            expenses = expenses.filter(date=specific_date)

        
        expenses = expenses.order_by('-date', 'title')
        total_filtered = expenses.aggregate(Sum('cost'))['cost__sum'] or 0
        categories = set(Daily.objects.filter(user=request.user).values_list('category', flat=True).distinct())
        months = Monthly.objects.filter(user=request.user).order_by('-date')
        
        expenses_page = Paginator(expenses, per_page_view).get_page(request.GET.get('page'))
        qs = request.GET.copy()
        qs.pop('page', None) 
        querystring = qs.urlencode()

        context = {
            'expenses': expenses_page,
            'categories': categories,
            'months': months,
            'current_month_filter': month_filter,
            'current_category_filter': category_filter,
            'current_date_filter': specific_date if date_filter else '',
            'total_filtered': total_filtered,
            'today': timezone.now().date(),
            'querystring': querystring,
        }
        
        return render(request, 'finance/expense_list.html', context)

@method_decorator(login_required, name='dispatch')
class AddExpenseView(View):
    def get(self, request):
        context = {
            'categories': CATEGORIES_EXPENSES,
            'today': timezone.now().date()
        }
        
        return render(request, 'finance/add_expense.html', context)
    
    @transaction.atomic
    def post(self, request):
        try:
            date = datetime.strptime(request.POST.get('date'), '%Y-%m-%d').date()
            title = request.POST.get('title')
            category = request.POST.get('category')
            store = request.POST.get('store', '')
            cost = parse_decimal(request.POST.get('cost'))
            
            if cost <= 0:
                raise ValueError("Kwota musi być większa od 0")
                
            month_date = month_start(date)
            
            monthly_record, created = Monthly.objects.select_for_update().get_or_create(
                user=request.user,
                date=month_date,
                defaults={'total_income': 0, 'total_expense': 0}
            )
            
            expense = Daily.objects.create(
                user=request.user,
                date=date,
                title=title,
                category=category,
                store=store,
                cost=cost,
                month=monthly_record
            )
            
            monthly_record.total_expense = Daily.objects.filter(user=request.user,month=monthly_record).aggregate(Sum('cost'))['cost__sum'] or 0
            monthly_record.save()
            
            messages.success(request, 'Wydatek został dodany pomyślnie!')
            
        except Exception as e:
            messages.error(request, f'Błąd podczas dodawania wydatku: {str(e)}')
        return redirect('finance:expense_list')

@method_decorator(login_required, name='dispatch')
class EditExpenseView(View):
    def get(self, request, expense_id):
        expense = get_object_or_404(Daily, id=expense_id, user=request.user)
        
        context = {
            'expense': expense,
            'categories': CATEGORIES_EXPENSES
        }
        
        return render(request, 'finance/edit_expense.html', context)
    
    @transaction.atomic
    def post(self, request, expense_id):
        expense = get_object_or_404(Daily, id=expense_id, user=request.user)
        old_monthly = expense.month
        expense.date = datetime.strptime(request.POST.get('date'), '%Y-%m-%d').date()
        expense.title = request.POST.get('title')
        expense.category = request.POST.get('category')
        expense.store = request.POST.get('store', '')
        expense.cost = parse_decimal(request.POST.get('cost'))
        
        try:
            new_month_date = month_start(expense.date)
            if old_monthly.date != new_month_date:
                new_monthly, created = Monthly.objects.select_for_update().get_or_create(
                    user=request.user,
                    date=new_month_date,
                    defaults={'total_income': 0, 'total_expense': 0}
                )
                expense.month = new_monthly
            expense.save()
            
            months_to_recalc = {expense.month}
            if old_monthly != expense.month:
                months_to_recalc.add(old_monthly)

            for monthly in months_to_recalc:
                monthly.total_expense = Daily.objects.filter(
                    user=request.user,
                    month=monthly
                ).aggregate(Sum('cost'))['cost__sum'] or 0
                monthly.save()
            
            messages.success(request, 'Wydatek został zaktualizowany!')
            return redirect('finance:expense_list')
            
        except Exception as e:
            messages.error(request, f'Błąd podczas aktualizacji: {str(e)}')

@method_decorator(login_required, name='dispatch')
class DeleteExpenseView(View):
    def get(self, request, expense_id):
        expense = get_object_or_404(Daily, id=expense_id, user=request.user)
        context = {
            'expense': expense
        }
        return render(request, 'finance/delete_expense.html', context)
    
    @transaction.atomic
    def post(self, request, expense_id):
        expense = get_object_or_404(Daily, id=expense_id, user=request.user)
        monthly_record = expense.month
        expense_title = expense.title
        expense.delete()
        
        monthly_record.total_expense = Daily.objects.filter(
            user=request.user,
            month=monthly_record
        ).aggregate(Sum('cost'))['cost__sum'] or 0
        monthly_record.save()
        
        messages.success(request, f'Wydatek "{expense_title}" został usunięty!')
        return redirect('finance:expense_list')

@method_decorator(login_required, name='dispatch')
class IncomeListView(View):
    def get(self, request):
        month_filter = request.GET.get('month', '')
        source_filter = request.GET.get('source', '')
        
        incomes = Income.objects.filter(user=request.user).select_related('month')
        
        if month_filter:
            try:
                month_date = datetime.strptime(month_filter, '%Y-%m').date()
                month_obj = Monthly.objects.filter(
                    user=request.user,
                    date__year=month_date.year, 
                    date__month=month_date.month
                ).first()
                if month_obj:
                    incomes = incomes.filter(month=month_obj)
            except ValueError:
                pass
        
        if source_filter:
            incomes = incomes.filter(source=source_filter)
        
        incomes = incomes.order_by('-date')
        total_filtered = incomes.aggregate(Sum('amount'))['amount__sum'] or 0
        sources = Income.objects.filter(user=request.user).values_list('source', flat=True).distinct()
        months = Monthly.objects.filter(user=request.user).order_by('-date')
        
        context = {
            'incomes': incomes,
            'sources': set(sources),
            'months': months,
            'current_month_filter': month_filter,
            'current_source_filter': source_filter,
            'total_filtered': total_filtered,
        }
        
        return render(request, 'finance/income_list.html', context)

@method_decorator(login_required, name='dispatch')
class AddIncomeView(View):
    def get(self, request):
        context = {
            'default_date': timezone.now().date().strftime('%Y-%m-%d'),
            'income_sources': INCOME_SOURCES
        }
        return render(request, 'finance/add_income.html', context)
    
    @transaction.atomic
    def post(self, request):
        date = request.POST.get('date')
        title = request.POST.get('title')
        amount = parse_decimal(request.POST.get('amount'))
        source = request.POST.get('source')
        
        try:
            income_date = datetime.strptime(date, '%Y-%m-%d').date()
            month_date = month_start(income_date)
            
            monthly_record, created = Monthly.objects.select_for_update().get_or_create(
                user=request.user,
                date=month_date,
                defaults={'total_income': 0, 'total_expense': 0}
            )
            
            income_record = Income.objects.create(
                user=request.user,
                date=income_date,
                title=title,
                amount=amount,
                source=source,
                month=monthly_record
            )
            
            monthly_record.total_income = Income.objects.filter(
                user=request.user,
                month=monthly_record
            ).aggregate(Sum('amount'))['amount__sum'] or 0
            monthly_record.save()
            
            messages.success(request, f'Przychód "{title}" ({amount} zł) został dodany!')
            return redirect('finance:income_list')
            
        except Exception as e:
            messages.error(request, f'Błąd: {str(e)}')

@method_decorator(login_required, name='dispatch')
class EditIncomeView(View):
    def get(self, request, income_id):
        income = get_object_or_404(Income, id=income_id, user=request.user)
        context = {
            'income': income,
            'income_sources': INCOME_SOURCES
        }
        
        return render(request, 'finance/edit_income.html', context)

    @transaction.atomic
    def post(self, request, income_id):
        income = get_object_or_404(Income, id=income_id, user=request.user)
        old_monthly = income.month
        income.date = datetime.strptime(request.POST.get('date'), '%Y-%m-%d').date()
        income.title = request.POST.get('title')
        income.source = request.POST.get('source')
        income.amount = parse_decimal(request.POST.get('amount'))
        
        try:
            new_month_date = income.date.replace(day=1)
            if old_monthly.date != new_month_date:
                new_monthly, created = Monthly.objects.select_for_update().get_or_create(
                    user=request.user,
                    date=new_month_date,
                    defaults={'total_income': 0, 'total_expense': 0}
                )
                income.month = new_monthly
            income.save()
            
            months_to_recalc = {income.month}
            if old_monthly != income.month:
                months_to_recalc.add(old_monthly)

            for monthly in months_to_recalc:
                monthly.total_income = Income.objects.filter(
                    user=request.user,
                    month=monthly
                ).aggregate(Sum('amount'))['amount__sum'] or 0
                monthly.save()
            
            messages.success(request, 'Przychód został zaktualizowany!')
            return redirect('finance:income_list')
            
        except Exception as e:
            messages.error(request, f'Błąd podczas aktualizacji: {str(e)}')

@method_decorator(login_required, name='dispatch')
class DeleteIncomeView(View):
    def get(self, request, income_id):
        income = get_object_or_404(Income, id=income_id, user=request.user)
        context = {
        'income': income
        }
        return render(request, 'finance/delete_income.html', context)
    
    @transaction.atomic
    def post(self, request, income_id):
        income = get_object_or_404(Income, id=income_id, user=request.user)
        
        if request.method == 'POST':
            monthly_record = income.month
            income_title = income.title
            income.delete()
            
            monthly_record.total_income = Income.objects.filter(
                user=request.user,
                month=monthly_record
            ).aggregate(Sum('amount'))['amount__sum'] or 0
            monthly_record.save()
            
            messages.success(request, f'Przychód "{income_title}" został usunięty!')
            return redirect('finance:income_list')
    
@method_decorator(login_required, name='dispatch')
class ReportsView(View):
    def get(self, request):
        monthly_records = Monthly.objects.filter(user=request.user).order_by('-date')[:6]
        
        months_labels = list()
        income_data = list()
        expense_data = list()
        monthly_balance = list()
        
        for record in reversed(monthly_records):
            months_labels.append(record.date.strftime('%B %Y'))
            income_data.append(float(record.total_income))
            expense_data.append(float(record.total_expense))
            monthly_balance.append(float(record.total_income - record.total_expense))
            record.monthly_balance = record.total_income - record.total_expense
        
        total_income_all = Monthly.objects.filter(user=request.user).aggregate(Sum('total_income'))['total_income__sum'] or 0
        total_expense_all = Monthly.objects.filter(user=request.user).aggregate(Sum('total_expense'))['total_expense__sum'] or 0
        
        top_categories = Daily.objects.filter(user=request.user).values('category').annotate(
            total=Sum('cost')
        ).order_by('-total')[:5]
        
        context = {
            'monthly_records': monthly_records,
            'months_labels': json.dumps(months_labels),
            'monthly_balance': json.dumps(monthly_balance),
            'income_data': json.dumps(income_data),
            'expense_data': json.dumps(expense_data),
            'total_income_all': total_income_all,
            'total_expense_all': total_expense_all,
            'balance_all': total_income_all - total_expense_all,
            'top_categories': top_categories
        }
        
        return render(request, 'finance/reports.html', context)

@method_decorator(login_required, name='dispatch')
class TravelView(View):
    def get(self, request):
        country_filter = request.GET.get('country', '')
        destinations = TravelDestinations.objects.filter(user=request.user)
        country_objs = []
        for code in destinations.values_list('country', flat=True).distinct():
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
            else:
                messages.error(request, 'Formularz zawiera błędy. Proszę poprawić i spróbować ponownie.')
        except Exception as e:
            messages.error(request, f'Błąd podczas dodawania wydatku podróży: {str(e)}')
        return render(request, 'finance/add_travel.html', {'form': form})

@method_decorator(login_required, name='dispatch')
class EditTravelView(View):
    def get(self, request, travel_id):
        travel = get_object_or_404(TravelDestinations, id=travel_id, user=request.user)
        travel.start_date = travel.start_date.strftime('%Y-%m-%d')
        travel.end_date = travel.end_date.strftime('%Y-%m-%d')
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
            else:
                messages.error(request, 'Formularz zawiera błędy. Proszę poprawić i spróbować ponownie.')
        except Exception as e:
            messages.error(request, f'Błąd podczas aktualizacji podróży: {str(e)}')
        return render(request, 'finance/edit_travel.html', {'form': form, 'travel': travel})

@method_decorator(login_required, name='dispatch')
class DeleteTravelView(View):
    def get(self, request, travel_id):
        travel = get_object_or_404(TravelDestinations, id=travel_id, user=request.user)
        context = {
            'travel': travel
        }
        return render(request, 'finance/delete_travel.html', context)
    
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
        records = Daily.objects.filter(user=request.user).values(
            'id', 'date', 'title', 'cost', 'store', 'category'
        )
        return Response(list(records))
    
    def post(self, request):
        data = request.data
        try:
            expense_date = datetime.strptime(data['date'], '%Y-%m-%d').date()
            month_date = expense_date.replace(day=1)
            
            monthly_record, created = Monthly.objects.get_or_create(
                user=request.user,
                date=month_date,
                defaults={'total_income': 0, 'total_expense': 0}
            )
            
            daily_record = Daily.objects.create(
                user=request.user,
                date=expense_date,
                title=data['title'],
                category=data.get('category', 'Inne'),
                store=data.get('store', ''),
                cost=parse_decimal(data['cost']),
                month=monthly_record
            )
            
            monthly_record.total_expense = Daily.objects.filter(
                user=request.user,
                month=monthly_record
            ).aggregate(Sum('cost'))['cost__sum'] or 0
            monthly_record.save()
            
            return Response({
                'status': 'success',
                'id': daily_record.id
            })
        except Exception as e:
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=400)

class MonthlyRecordAPI(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        records = Monthly.objects.filter(user=request.user).values(
            'id', 'date', 'total_income', 'total_expense'
        )
        return Response(list(records))