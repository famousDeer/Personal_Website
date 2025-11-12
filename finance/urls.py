# finance/urls.py
from django.urls import path
from . import views

app_name = 'finance'

urlpatterns = [
    path('', views.index, name='index'),
    path('dashboard/', views.DashboardView.as_view(), name='dashboard'),

    # Expensews
    path('expenses/', views.ExpenseListView.as_view(), name='expense_list'),
    path('expenses/add/', views.AddExpenseView.as_view(), name='add_expense'),
    path('expenses/edit/<int:expense_id>/', views.EditExpenseView.as_view(), name='edit_expense'),
    path('expenses/delete/<int:expense_id>/', views.DeleteExpenseView.as_view(), name='delete_expense'),

    # Incomes
    path('income/', views.IncomeListView.as_view(), name='income_list'),
    path('income/add/', views.AddIncomeView.as_view(), name='add_income'),
    path('income/edit/<int:income_id>/', views.EditIncomeView.as_view(), name='edit_income'),
    path('income/delete/<int:income_id>/', views.DeleteIncomeView.as_view(), name='delete_income'),

    # Reports
    path('reports/', views.ReportsView.as_view(), name='reports'),

    # Travels
    path('travel/', views.TravelView.as_view(), name='travels'),
    path('travel/add/', views.AddTravelView.as_view(), name='add_travel'),
    path('travel/edit/<int:travel_id>/', views.EditTravelView.as_view(), name='edit_travel'),
    path('travel/delete/<int:travel_id>/', views.DeleteTravelView.as_view(), name='delete_travel'),

    # API
    path('api/daily/', views.DailyRecordAPI.as_view(), name='api_daily'),
    path('api/monthly/', views.MonthlyRecordAPI.as_view(), name='api_monthly'),
]