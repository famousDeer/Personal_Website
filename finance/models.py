# finance/models.py
from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()

class Monthly(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='monthly_records')
    date = models.DateField()
    total_income = models.FloatField(default=0.0)
    total_expense = models.FloatField(default=0.0)

    class Meta:
        db_table = 'monthly_records'
        ordering = ['date']
        verbose_name = "Monthly Record"
        verbose_name_plural = "Monthly Records"

    def __str__(self):
        return f"{self.user.username} – {self.date}"


class Daily(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='daily_records')
    date = models.DateField()
    title = models.CharField(max_length=255)
    cost = models.FloatField()
    store = models.CharField(max_length=255, blank=True)
    month = models.ForeignKey(Monthly, on_delete=models.CASCADE, related_name='daily_entries')
    category = models.CharField(max_length=100)

    class Meta:
        db_table = 'daily_records'
        ordering = ['-date']
        verbose_name = "Daily Record"
        verbose_name_plural = "Daily Records"

    def __str__(self):
        return f"{self.user.username} – {self.date} – {self.title}"


class Income(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='income_records')
    date = models.DateField()
    title = models.CharField(max_length=255)
    amount = models.FloatField()
    source = models.CharField(max_length=100)
    month = models.ForeignKey(Monthly, on_delete=models.CASCADE, related_name='income_entries')

    class Meta:
        db_table = 'income_records'
        ordering = ['-date']
        verbose_name = "Income Record"
        verbose_name_plural = "Income Records"

    def __str__(self):
        return f"{self.user.username} – {self.date} – {self.title}"