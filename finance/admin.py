from django.contrib import admin
from .models import Monthly, Daily, Income

@admin.register(Monthly)
class MonthlyAdmin(admin.ModelAdmin):
    list_display = ('user', 'date', 'total_income', 'total_expense')
    list_filter = ('user', 'date')

@admin.register(Daily)
class DailyAdmin(admin.ModelAdmin):
    list_display = ('user', 'date', 'title', 'category', 'store', 'cost', 'month')
    list_filter = ('user', 'category', 'date')
    search_fields = ('title', 'store')

@admin.register(Income)
class IncomeAdmin(admin.ModelAdmin):
    list_display = ('user', 'date', 'title', 'source', 'amount', 'month')
    list_filter = ('user', 'source', 'date')
    search_fields = ('title',)
