from rest_framework import serializers
from django.db.models import Sum
from .models import Daily, Income, Monthly

class DailySerializer(serializers.ModelSerializer):
    class Meta:
        model = Daily
        fields = ['date', 'title', 'cost', 'store', 'category', 'month']

class IncomeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Income
        fields = ['date', 'title', 'amount', 'source', 'month']

class MonthlySerializer(serializers.ModelSerializer):
    total_income = serializers.SerializerMethodField()
    total_expenses = serializers.SerializerMethodField()
    net_savings = serializers.SerializerMethodField()

    class Meta:
        model = Monthly
        fields = ['date', ' total_income', 'total_expense']

    def get_total_income(self, obj):
        return Income.objects.filter(date__month=obj.month, date__year=obj.year).aggregate(Sum('amount'))['amount__sum'] or 0

    def get_total_expenses(self, obj):
        return Daily.objects.filter(date__month=obj.month, date__year=obj.year).aggregate(Sum('amount'))['amount__sum'] or 0

    def get_net_savings(self, obj):
        return self.get_total_income(obj) - self.get_total_expenses(obj)