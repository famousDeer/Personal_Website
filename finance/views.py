from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.db.models import Sum
from django.contrib import messages
from django.utils import timezone
from datetime import datetime
from .models import Monthly, Daily, Income
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
import json

def index(request):
    return redirect('dashboard')

def dashboard(request):
    current_date = timezone.now().date()
    current_month_date = current_date.replace(day=1)
    
    monthly_record, created = Monthly.objects.get_or_create(
        user=request.user,
        date=current_month_date,
        defaults={'total_income': 0, 'total_expense': 0}
    )
    
    daily_expenses = Daily.objects.filter(
        user=request.user,
        month=monthly_record
    ).aggregate(total=Sum('cost'))['total'] or 0
    
    if monthly_record.total_expense != daily_expenses:
        monthly_record.total_expense = daily_expenses
        monthly_record.save()
    
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
    
    total_income_calculated = Income.objects.filter(
        user=request.user,
        month=monthly_record
    ).aggregate(total=Sum('amount'))['total'] or 0
    
    if monthly_record.total_income != total_income_calculated:
        monthly_record.total_income = total_income_calculated
        monthly_record.save()
    
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
        'recent_incomes': recent_incomes
    }
    
    return render(request, 'finance/dashboard.html', context)

def expense_list(request):
    month_filter = request.GET.get('month', '')
    category_filter = request.GET.get('category', '')

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
    
    expenses = expenses.order_by('-date')
    total_filtered = expenses.aggregate(Sum('cost'))['cost__sum'] or 0
    categories = Daily.objects.filter(user=request.user).values_list('category', flat=True).distinct()
    months = Monthly.objects.filter(user=request.user).order_by('-date')
    
    context = {
        'expenses': expenses,
        'categories': categories,
        'months': months,
        'current_month_filter': month_filter,
        'current_category_filter': category_filter,
        'total_filtered': total_filtered,
    }
    
    return render(request, 'finance/expense_list.html', context)

def add_expense(request):
    categories = [
        'Jedzenie',
        'Transport',
        'Rozrywka',
        'Zakupy',
        'Rachunki',
        'Zdrowie',
        'Edukacja',
        'Rodzice',
        'Inne'
    ]
    
    today = timezone.now().date()
    
    if request.method == 'POST':
        try:
            date = datetime.strptime(request.POST.get('date'), '%Y-%m-%d').date()
            title = request.POST.get('title')
            category = request.POST.get('category')
            store = request.POST.get('store', '')
            cost = float(request.POST.get('cost'))
            
            if cost <= 0:
                raise ValueError("Kwota musi być większa od 0")
                
            month_date = date.replace(day=1)
            
            monthly_record, created = Monthly.objects.get_or_create(
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
            return redirect('expense_list')
            
        except Exception as e:
            messages.error(request, f'Błąd podczas dodawania wydatku: {str(e)}')
    
    context = {
        'categories': categories,
        'today': today
    }
    
    return render(request, 'finance/add_expense.html', context)

def edit_expense(request, expense_id):
    expense = get_object_or_404(Daily, id=expense_id, user=request.user)
    
    if request.method == 'POST':
        old_monthly = expense.month
        expense.date = datetime.strptime(request.POST.get('date'), '%Y-%m-%d').date()
        expense.title = request.POST.get('title')
        expense.category = request.POST.get('category')
        expense.store = request.POST.get('store', '')
        expense.cost = float(request.POST.get('cost'))
        
        try:
            new_month_date = expense.date.replace(day=1)
            if old_monthly.date != new_month_date:
                new_monthly, created = Monthly.objects.get_or_create(
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
            return redirect('expense_list')
            
        except Exception as e:
            messages.error(request, f'Błąd podczas aktualizacji: {str(e)}')
    
    categories = [
        'Jedzenie',
        'Transport',
        'Rozrywka',
        'Zakupy',
        'Rachunki',
        'Zdrowie',
        'Edukacja',
        'Inne'
    ]
    
    context = {
        'expense': expense,
        'categories': categories
    }
    
    return render(request, 'finance/edit_expense.html', context)

def delete_expense(request, expense_id):
    expense = get_object_or_404(Daily, id=expense_id, user=request.user)
    
    if request.method == 'POST':
        monthly_record = expense.month
        expense_title = expense.title
        expense.delete()
        
        monthly_record.total_expense = Daily.objects.filter(
            user=request.user,
            month=monthly_record
        ).aggregate(Sum('cost'))['cost__sum'] or 0
        monthly_record.save()
        
        messages.success(request, f'Wydatek "{expense_title}" został usunięty!')
        return redirect('expense_list')
    
    context = {
        'expense': expense
    }
    
    return render(request, 'finance/delete_expense.html', context)

def reports(request):
    monthly_records = Monthly.objects.filter(user=request.user).order_by('-date')[:6]
    
    months_labels = []
    income_data = []
    expense_data = []
    monthly_balance = []
    
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
def income_list(request):
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
        'sources': sources,
        'months': months,
        'current_month_filter': month_filter,
        'current_source_filter': source_filter,
        'total_filtered': total_filtered,
    }
    
    return render(request, 'finance/income_list.html', context)

def edit_income(request, income_id):
    income = get_object_or_404(Income, id=income_id, user=request.user)
    
    if request.method == 'POST':
        old_monthly = income.month
        income.date = datetime.strptime(request.POST.get('date'), '%Y-%m-%d').date()
        income.title = request.POST.get('title')
        income.source = request.POST.get('source')
        income.amount = float(request.POST.get('amount'))
        
        try:
            new_month_date = income.date.replace(day=1)
            if old_monthly.date != new_month_date:
                new_monthly, created = Monthly.objects.get_or_create(
                    user=request.user,
                    date=new_month_date,
                    defaults={'total_income': 0, 'total_expense': 0}
                )
                income.month = new_monthly
            income.save()
            
            months_to_recalc = {income.month}
            if old_monthly != income.month:
                months_to_recalc.add(old_monthly)

            for monthly in [income.month]:
                monthly.total_income = Income.objects.filter(
                    user=request.user,
                    month=monthly
                ).aggregate(Sum('amount'))['amount__sum'] or 0
                monthly.save()
            
            messages.success(request, 'Przychód został zaktualizowany!')
            return redirect('income_list')
            
        except Exception as e:
            messages.error(request, f'Błąd podczas aktualizacji: {str(e)}')
    
    income_sources = [
        'Pensja',
        'Premia',
        'Freelance',
        'Inwestycje',
        'Zwrot podatku',
        'Sprzedaż',
        'Rodzice',
        'Inne'
    ]
    
    context = {
        'income': income,
        'income_sources': income_sources
    }
    
    return render(request, 'finance/edit_income.html', context)

def delete_income(request, income_id):
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
        return redirect('income_list')
    
    context = {
        'income': income
    }
    
    return render(request, 'finance/delete_income.html', context)

def add_income(request):
    if request.method == 'POST':
        date = request.POST.get('date')
        title = request.POST.get('title')
        amount = float(request.POST.get('amount'))
        source = request.POST.get('source')
        
        try:
            income_date = datetime.strptime(date, '%Y-%m-%d').date()
            month_date = income_date.replace(day=1)
            
            monthly_record, created = Monthly.objects.get_or_create(
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
            return redirect('dashboard')
            
        except Exception as e:
            messages.error(request, f'Błąd: {str(e)}')
    
    income_sources = [
        'Pensja',
        'Premia',
        'Freelance',
        'Inwestycje',
        'Zwrot podatku',
        'Sprzedaż',
        'Inne'
    ]
    
    return render(request, 'finance/add_income.html', {
        'default_date': timezone.now().date().strftime('%Y-%m-%d'),
        'income_sources': income_sources
    })

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
                cost=float(data['cost']),
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