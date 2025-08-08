# finance/urls.py
from django.urls import path
from django.contrib.auth.decorators import login_required
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('dashboard/', login_required(views.dashboard), name='dashboard'),

    # Expensews
    path('expenses/', login_required(views.expense_list), name='expense_list'),
    path('expenses/add/', login_required(views.add_expense), name='add_expense'),
    path('expenses/edit/<int:expense_id>/', login_required(views.edit_expense), name='edit_expense'),
    path('expenses/delete/<int:expense_id>/', login_required(views.delete_expense), name='delete_expense'),

    # Incomes
    path('income/', login_required(views.income_list), name='income_list'),
    path('income/add/', login_required(views.add_income), name='add_income'),
    path('income/edit/<int:income_id>/', login_required(views.edit_income), name='edit_income'),
    path('income/delete/<int:income_id>/', login_required(views.delete_income), name='delete_income'),

    # Reports
    path('reports/', login_required(views.reports), name='reports'),

    # API
    path('api/daily/', login_required(views.DailyRecordAPI.as_view()), name='api_daily'),
    path('api/monthly/', login_required(views.MonthlyRecordAPI.as_view()), name='api_monthly'),
]