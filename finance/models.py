# finance/models.py
from django.db import models
from django.contrib.auth import get_user_model
from django.core.validators import MinValueValidator
from decimal import Decimal
from django_countries.fields import CountryField

User = get_user_model()

class FinanceAccount(models.Model):
    PERSONAL = 'personal'
    SHARED = 'shared'
    ACCOUNT_TYPE_CHOICES = [
        (PERSONAL, 'Konto osobiste'),
        (SHARED, 'Konto wspólne'),
    ]

    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='owned_finance_accounts',
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=255)
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES, default=PERSONAL)
    members = models.ManyToManyField(User, related_name='finance_accounts')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'finance_accounts'
        ordering = ['account_type', 'name']
        verbose_name = "Finance Account"
        verbose_name_plural = "Finance Accounts"

    @property
    def display_name(self):
        if self.account_type == self.PERSONAL and self.owner:
            return f"{self.owner.username} - konto osobiste"
        return self.name

    def __str__(self):
        return self.display_name

# Expense and Income database
class Monthly(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='monthly_records')
    account = models.ForeignKey('FinanceAccount', on_delete=models.CASCADE, related_name='monthly_records', null=True, blank=True)
    date = models.DateField()
    total_income = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    total_expense = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))

    class Meta:
        db_table = 'monthly_records'
        ordering = ['date']
        verbose_name = "Monthly Record"
        verbose_name_plural = "Monthly Records"
        constraints = [
            models.UniqueConstraint(fields=['account', 'date'], name='unique_account_month'),
        ]
        indexes = [
            models.Index(fields=['account', 'date']),
        ]

    def __str__(self):
        account_name = self.account.display_name if self.account else self.user.username
        return f"{account_name} – {self.date}"

class Daily(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='daily_records')
    account = models.ForeignKey('FinanceAccount', on_delete=models.CASCADE, related_name='daily_records', null=True, blank=True)
    date = models.DateField()
    title = models.CharField(max_length=255)
    cost = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    store = models.CharField(max_length=255, blank=True)
    month = models.ForeignKey(Monthly, on_delete=models.CASCADE, related_name='daily_entries')
    category = models.CharField(max_length=100)
    transfer_target_account = models.ForeignKey(
        'FinanceAccount',
        on_delete=models.SET_NULL,
        related_name='incoming_transfer_expenses',
        null=True,
        blank=True,
    )

    class Meta:
        db_table = 'daily_records'
        ordering = ['-date']
        verbose_name = "Daily Record"
        verbose_name_plural = "Daily Records"

    def __str__(self):
        account_name = self.account.display_name if self.account else self.user.username
        return f"{account_name} – {self.date} – {self.title}"

class Income(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='income_records')
    account = models.ForeignKey('FinanceAccount', on_delete=models.CASCADE, related_name='income_records', null=True, blank=True)
    date = models.DateField()
    title = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    source = models.CharField(max_length=100)
    month = models.ForeignKey(Monthly, on_delete=models.CASCADE, related_name='income_entries')
    linked_expense = models.OneToOneField(
        Daily,
        on_delete=models.CASCADE,
        related_name='linked_shared_income',
        null=True,
        blank=True,
    )

    class Meta:
        db_table = 'income_records'
        ordering = ['-date']
        verbose_name = "Income Record"
        verbose_name_plural = "Income Records"

    def __str__(self):
        account_name = self.account.display_name if self.account else self.user.username
        return f"{account_name} – {self.date} – {self.title}"

# Travel database
class TravelDestinations(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='travel_destinations')
    country = CountryField(verbose_name='Kraj')
    city = models.CharField(max_length=255, blank=True)
    start_date = models.DateField()
    end_date = models.DateField()
    budget = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.00'))], default=Decimal('0.00'))

    class Meta:
        db_table = 'travel_destinations'
        ordering = ['-start_date']
        verbose_name = "Travel Destination"
        verbose_name_plural = "Travel Destinations"

    def __str__(self):
        return f"{self.user.username} – {self.destination} ({self.start_date} to {self.end_date})"

class TravelExpense(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='travel_expenses')
    travel_destination = models.ForeignKey(TravelDestinations, on_delete=models.CASCADE, related_name='expenses')
    date = models.DateField()
    title = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    category = models.CharField(max_length=100)

    class Meta:
        db_table = 'travel_expenses'
        ordering = ['-date']
        verbose_name = "Travel Expense"
        verbose_name_plural = "Travel Expenses"

    def __str__(self):
        return f"{self.user.username} – {self.travel_destination.destination} – {self.date} – {self.title}"
