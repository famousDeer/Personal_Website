# finance/urls.py
from django.urls import path
from . import views

app_name = 'finance'

urlpatterns = [
    path('', views.index, name='index'),
    path('switch-account/', views.switch_account, name='switch_account'),
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

    # Brokerage portfolio
    path('brokerage/', views.BrokeragePortfolioView.as_view(), name='brokerage'),
    path('brokerage/refresh/', views.RefreshBrokerageMarketDataView.as_view(), name='brokerage_refresh'),
    path('brokerage/accounts/add/', views.AddBrokerageAccountView.as_view(), name='add_brokerage_account'),
    path('brokerage/accounts/<int:account_id>/edit/', views.EditBrokerageAccountView.as_view(), name='edit_brokerage_account'),
    path('brokerage/accounts/<int:account_id>/delete/', views.DeleteBrokerageAccountView.as_view(), name='delete_brokerage_account'),
    path('brokerage/instruments/add/', views.AddBrokerageInstrumentView.as_view(), name='add_brokerage_instrument'),
    path('brokerage/instruments/<int:instrument_id>/edit/', views.EditBrokerageInstrumentView.as_view(), name='edit_brokerage_instrument'),
    path('brokerage/instruments/<int:instrument_id>/delete/', views.DeleteBrokerageInstrumentView.as_view(), name='delete_brokerage_instrument'),
    path('brokerage/transactions/add/', views.AddBrokerageTransactionView.as_view(), name='add_brokerage_transaction'),
    path('brokerage/transactions/<int:transaction_id>/edit/', views.EditBrokerageTransactionView.as_view(), name='edit_brokerage_transaction'),
    path('brokerage/transactions/<int:transaction_id>/delete/', views.DeleteBrokerageTransactionView.as_view(), name='delete_brokerage_transaction'),
    path('brokerage/dividends/add/', views.AddBrokerageDividendView.as_view(), name='add_brokerage_dividend'),
    path('brokerage/dividends/<int:dividend_id>/edit/', views.EditBrokerageDividendView.as_view(), name='edit_brokerage_dividend'),
    path('brokerage/dividends/<int:dividend_id>/delete/', views.DeleteBrokerageDividendView.as_view(), name='delete_brokerage_dividend'),

    # Travels
    path('travel/', views.TravelView.as_view(), name='travels'),
    path('travel/add/', views.AddTravelView.as_view(), name='add_travel'),
    path('travel/edit/<int:travel_id>/', views.EditTravelView.as_view(), name='edit_travel'),
    path('travel/delete/<int:travel_id>/', views.DeleteTravelView.as_view(), name='delete_travel'),

    # API
    path('api/daily/', views.DailyRecordAPI.as_view(), name='api_daily'),
    path('api/monthly/', views.MonthlyRecordAPI.as_view(), name='api_monthly'),
]
