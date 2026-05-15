from rest_framework import serializers
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
    net_savings = serializers.SerializerMethodField()

    class Meta:
        model = Monthly
        fields = ['id', 'date', 'total_income', 'total_expense', 'net_savings']

    def get_net_savings(self, obj):
        return f'{obj.total_income - obj.total_expense:.2f}'
