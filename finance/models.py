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


class BrokerageAccount(models.Model):
    BROKER_XTB = 'xtb'
    BROKER_MBANK = 'mbank'
    BROKER_OTHER = 'other'
    BROKER_CHOICES = [
        (BROKER_XTB, 'XTB'),
        (BROKER_MBANK, 'mBank'),
        (BROKER_OTHER, 'Inny broker'),
    ]

    STANDARD = 'standard'
    IKE = 'ike'
    ACCOUNT_TYPE_CHOICES = [
        (STANDARD, 'Zwykłe konto maklerskie'),
        (IKE, 'IKE'),
    ]

    CURRENCY_CHOICES = [
        ('PLN', 'PLN'),
        ('EUR', 'EUR'),
        ('USD', 'USD'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='brokerage_accounts')
    name = models.CharField(max_length=120)
    broker = models.CharField(max_length=20, choices=BROKER_CHOICES)
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES, default=STANDARD)
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='PLN')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'brokerage_accounts'
        ordering = ['broker', 'account_type', 'currency', 'name']
        constraints = [
            models.UniqueConstraint(fields=['user', 'name'], name='unique_user_brokerage_account_name'),
        ]

    @property
    def is_tax_exempt(self):
        return self.account_type == self.IKE

    def __str__(self):
        return f"{self.name} ({self.get_broker_display()}, {self.currency})"


class BrokerageInstrument(models.Model):
    STOCK = 'stock'
    ETF = 'etf'
    FUND = 'fund'
    BOND = 'bond'
    OTHER = 'other'
    ASSET_TYPE_CHOICES = [
        (STOCK, 'Akcja'),
        (ETF, 'ETF'),
        (FUND, 'Fundusz'),
        (BOND, 'Obligacja'),
        (OTHER, 'Inne'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='brokerage_instruments')
    ticker = models.CharField(max_length=32)
    price_symbol = models.CharField(max_length=32, blank=True)
    name = models.CharField(max_length=160)
    isin = models.CharField(max_length=12, blank=True)
    exchange = models.CharField(max_length=40, blank=True)
    asset_type = models.CharField(max_length=20, choices=ASSET_TYPE_CHOICES, default=STOCK)
    currency = models.CharField(max_length=3, choices=BrokerageAccount.CURRENCY_CHOICES, default='PLN')
    last_price = models.DecimalField(max_digits=14, decimal_places=4, blank=True, null=True)
    last_price_at = models.DateTimeField(blank=True, null=True)
    market_data_source = models.CharField(max_length=80, blank=True)

    class Meta:
        db_table = 'brokerage_instruments'
        ordering = ['ticker']
        constraints = [
            models.UniqueConstraint(fields=['user', 'ticker'], name='unique_user_brokerage_ticker'),
        ]

    def __str__(self):
        return f"{self.ticker} - {self.name}"


class BrokerageTransaction(models.Model):
    BUY = 'buy'
    SELL = 'sell'
    TRANSACTION_TYPE_CHOICES = [
        (BUY, 'Kupno'),
        (SELL, 'Sprzedaż'),
    ]

    account = models.ForeignKey(BrokerageAccount, on_delete=models.CASCADE, related_name='transactions')
    instrument = models.ForeignKey(BrokerageInstrument, on_delete=models.CASCADE, related_name='transactions')
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPE_CHOICES)
    trade_date = models.DateField()
    trade_time = models.TimeField(blank=True, null=True)
    quantity = models.DecimalField(max_digits=18, decimal_places=6, validators=[MinValueValidator(Decimal('0.000001'))])
    price = models.DecimalField(max_digits=14, decimal_places=4, validators=[MinValueValidator(Decimal('0.0001'))])
    market_price = models.DecimalField(max_digits=14, decimal_places=4, blank=True, null=True)
    market_price_source = models.CharField(max_length=80, blank=True)
    fees = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))])
    fx_rate_to_pln = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        default=Decimal('1.000000'),
        validators=[MinValueValidator(Decimal('0.000001'))],
        help_text='Kurs waluty transakcji do PLN używany do szacowania podatku i wartości portfela.',
    )
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'brokerage_transactions'
        ordering = ['-trade_date', '-id']
        indexes = [
            models.Index(fields=['account', 'instrument', 'trade_date']),
        ]

    @property
    def gross_value(self):
        return self.quantity * self.price

    @property
    def gross_value_pln(self):
        return self.gross_value * self.fx_rate_to_pln

    def __str__(self):
        return f"{self.get_transaction_type_display()} {self.quantity} {self.instrument.ticker}"


class BrokerageDividend(models.Model):
    PLANNED = 'planned'
    PAID = 'paid'
    STATUS_CHOICES = [
        (PLANNED, 'Planowana'),
        (PAID, 'Wypłacona'),
    ]

    account = models.ForeignKey(BrokerageAccount, on_delete=models.CASCADE, related_name='dividends')
    instrument = models.ForeignKey(BrokerageInstrument, on_delete=models.CASCADE, related_name='dividends')
    ex_dividend_date = models.DateField(blank=True, null=True)
    payment_date = models.DateField()
    gross_amount_per_share = models.DecimalField(max_digits=12, decimal_places=6, validators=[MinValueValidator(Decimal('0.000001'))])
    currency = models.CharField(max_length=3, choices=BrokerageAccount.CURRENCY_CHOICES)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('19.00'), validators=[MinValueValidator(Decimal('0.00'))])
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PLANNED)
    source = models.CharField(max_length=80, blank=True)

    class Meta:
        db_table = 'brokerage_dividends'
        ordering = ['payment_date', 'instrument__ticker']
        indexes = [
            models.Index(fields=['account', 'payment_date']),
        ]

    def __str__(self):
        return f"{self.instrument.ticker} dividend {self.payment_date}"

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
        destination = f"{self.city}, {self.country.name}" if self.city else self.country.name
        return f"{self.user.username} – {destination} ({self.start_date} to {self.end_date})"

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
        destination = (
            f"{self.travel_destination.city}, {self.travel_destination.country.name}"
            if self.travel_destination.city
            else self.travel_destination.country.name
        )
        return f"{self.user.username} – {destination} – {self.date} – {self.title}"
