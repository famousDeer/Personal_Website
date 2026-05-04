from django.contrib import admin
from .models import Daily, FinanceAccount, Income, Monthly

@admin.register(FinanceAccount)
class FinanceAccountAdmin(admin.ModelAdmin):
    list_display = ('display_name', 'account_type', 'owner', 'created_at')
    list_filter = ('account_type', 'created_at')
    search_fields = ('name', 'owner__username', 'members__username')

@admin.register(Monthly)
class MonthlyAdmin(admin.ModelAdmin):
    list_display = ('account', 'user', 'date', 'total_income', 'total_expense')
    list_filter = ('account', 'user', 'date')

@admin.register(Daily)
class DailyAdmin(admin.ModelAdmin):
    list_display = ('account', 'user', 'date', 'title', 'category', 'store', 'cost', 'month')
    list_filter = ('account', 'user', 'category', 'date')
    search_fields = ('title', 'store')

@admin.register(Income)
class IncomeAdmin(admin.ModelAdmin):
    list_display = ('account', 'user', 'date', 'title', 'source', 'amount', 'month')
    list_filter = ('account', 'user', 'source', 'date')
    search_fields = ('title',)
