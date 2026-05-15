from django.contrib import admin
from .models import (
    BrokerageAccount,
    BrokerageDividend,
    BrokerageInstrument,
    BrokerageTransaction,
    Daily,
    FinanceAccount,
    Income,
    Monthly,
)

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


@admin.register(BrokerageAccount)
class BrokerageAccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'broker', 'account_type', 'currency')
    list_filter = ('broker', 'account_type', 'currency')
    search_fields = ('name', 'user__username')


@admin.register(BrokerageInstrument)
class BrokerageInstrumentAdmin(admin.ModelAdmin):
    list_display = ('ticker', 'name', 'user', 'asset_type', 'currency', 'last_price', 'last_price_at')
    list_filter = ('asset_type', 'currency')
    search_fields = ('ticker', 'name', 'isin')


@admin.register(BrokerageTransaction)
class BrokerageTransactionAdmin(admin.ModelAdmin):
    list_display = ('trade_date', 'account', 'instrument', 'transaction_type', 'quantity', 'price', 'fees')
    list_filter = ('transaction_type', 'trade_date', 'account')
    search_fields = ('instrument__ticker', 'account__name')


@admin.register(BrokerageDividend)
class BrokerageDividendAdmin(admin.ModelAdmin):
    list_display = ('payment_date', 'account', 'instrument', 'gross_amount_per_share', 'currency', 'tax_rate', 'status')
    list_filter = ('status', 'payment_date', 'currency')
    search_fields = ('instrument__ticker', 'account__name')
