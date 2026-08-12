# finance/models.py
from django.db import models
from django.db.models import Q
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
    external_account_id = models.CharField(max_length=120, blank=True)
    last_import_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'brokerage_accounts'
        ordering = ['broker', 'account_type', 'currency', 'name']
        constraints = [
            models.UniqueConstraint(fields=['user', 'name'], name='unique_user_brokerage_account_name'),
            models.UniqueConstraint(
                fields=['user', 'broker', 'external_account_id'],
                condition=~Q(external_account_id=''),
                name='unique_user_broker_external_account',
            ),
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
    history_synced_from = models.DateField(blank=True, null=True)
    history_synced_through = models.DateField(blank=True, null=True)
    history_sync_at = models.DateTimeField(blank=True, null=True)
    history_sync_error = models.CharField(max_length=500, blank=True)

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
    import_source = models.CharField(max_length=40, blank=True)
    external_id = models.CharField(max_length=120, blank=True)
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
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'external_id'],
                condition=~Q(external_id=''),
                name='unique_brokerage_account_external_transaction',
            ),
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


class BrokerageCashOperation(models.Model):
    DEPOSIT = 'deposit'
    WITHDRAWAL = 'withdrawal'
    INTERNAL_TRANSFER = 'internal_transfer'
    BUY = 'buy'
    SELL = 'sell'
    DIVIDEND = 'dividend'
    WITHHOLDING_TAX = 'withholding_tax'
    INTEREST = 'interest'
    INTEREST_TAX = 'interest_tax'
    FEE = 'fee'
    OTHER = 'other'
    OPERATION_TYPE_CHOICES = [
        (DEPOSIT, 'Wpłata'),
        (WITHDRAWAL, 'Wypłata'),
        (INTERNAL_TRANSFER, 'Przelew wewnętrzny'),
        (BUY, 'Kupno'),
        (SELL, 'Sprzedaż'),
        (DIVIDEND, 'Dywidenda'),
        (WITHHOLDING_TAX, 'Podatek u źródła'),
        (INTEREST, 'Odsetki'),
        (INTEREST_TAX, 'Podatek od odsetek'),
        (FEE, 'Opłata'),
        (OTHER, 'Inna operacja'),
    ]

    account = models.ForeignKey(BrokerageAccount, on_delete=models.CASCADE, related_name='cash_operations')
    instrument = models.ForeignKey(
        BrokerageInstrument,
        on_delete=models.SET_NULL,
        related_name='cash_operations',
        null=True,
        blank=True,
    )
    operation_type = models.CharField(max_length=20, choices=OPERATION_TYPE_CHOICES)
    occurred_at = models.DateTimeField()
    amount = models.DecimalField(max_digits=18, decimal_places=4)
    currency = models.CharField(max_length=3, choices=BrokerageAccount.CURRENCY_CHOICES)
    external_id = models.CharField(max_length=120, blank=True)
    position_external_id = models.CharField(max_length=120, blank=True)
    description = models.CharField(max_length=500, blank=True)
    product = models.CharField(max_length=255, blank=True)
    import_source = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'brokerage_cash_operations'
        ordering = ['-occurred_at', '-id']
        indexes = [
            models.Index(fields=['account', 'occurred_at']),
            models.Index(fields=['instrument', 'occurred_at']),
            models.Index(fields=['account', 'operation_type', 'occurred_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'import_source', 'external_id'],
                condition=~Q(external_id=''),
                name='unique_broker_cash_import_operation',
            ),
        ]

    def __str__(self):
        return f"{self.get_operation_type_display()} {self.amount} {self.currency}"


class BrokeragePositionSnapshot(models.Model):
    account = models.ForeignKey(BrokerageAccount, on_delete=models.CASCADE, related_name='position_snapshots')
    instrument = models.ForeignKey(BrokerageInstrument, on_delete=models.CASCADE, related_name='position_snapshots')
    as_of = models.DateTimeField()
    quantity = models.DecimalField(max_digits=18, decimal_places=6)
    market_value = models.DecimalField(max_digits=18, decimal_places=4)
    current_price = models.DecimalField(max_digits=18, decimal_places=6, blank=True, null=True)
    profit = models.DecimalField(max_digits=18, decimal_places=4, blank=True, null=True)
    profit_percent = models.DecimalField(max_digits=12, decimal_places=4, blank=True, null=True)
    currency = models.CharField(max_length=3, choices=BrokerageAccount.CURRENCY_CHOICES)
    source = models.CharField(max_length=80, blank=True)

    class Meta:
        db_table = 'brokerage_position_snapshots'
        ordering = ['-as_of', 'instrument__ticker']
        indexes = [
            models.Index(fields=['account', 'as_of']),
            models.Index(fields=['instrument', 'as_of']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'instrument', 'as_of'],
                name='unique_broker_position_snapshot',
            ),
        ]

    def __str__(self):
        return f"{self.account} - {self.instrument.ticker} @ {self.as_of}"


class BrokeragePriceSnapshot(models.Model):
    instrument = models.ForeignKey(BrokerageInstrument, on_delete=models.CASCADE, related_name='price_snapshots')
    observed_at = models.DateTimeField()
    price = models.DecimalField(max_digits=18, decimal_places=6)
    source = models.CharField(max_length=80, blank=True)

    class Meta:
        db_table = 'brokerage_price_snapshots'
        ordering = ['-observed_at']
        indexes = [
            models.Index(fields=['instrument', 'observed_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['instrument', 'observed_at'],
                name='unique_broker_price_snapshot',
            ),
        ]

    def __str__(self):
        return f"{self.instrument.ticker} {self.price} @ {self.observed_at}"


class BrokerageDailyPrice(models.Model):
    instrument = models.ForeignKey(
        BrokerageInstrument,
        on_delete=models.CASCADE,
        related_name='daily_prices',
    )
    trading_date = models.DateField()
    open = models.DecimalField(max_digits=18, decimal_places=6, blank=True, null=True)
    high = models.DecimalField(max_digits=18, decimal_places=6, blank=True, null=True)
    low = models.DecimalField(max_digits=18, decimal_places=6, blank=True, null=True)
    close = models.DecimalField(max_digits=18, decimal_places=6)
    adjusted_close = models.DecimalField(max_digits=18, decimal_places=6, blank=True, null=True)
    volume = models.BigIntegerField(blank=True, null=True)
    currency = models.CharField(max_length=3, blank=True)
    provider_symbol = models.CharField(max_length=32, blank=True)
    source = models.CharField(max_length=80, blank=True)
    is_final = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'brokerage_daily_prices'
        ordering = ['trading_date']
        indexes = [
            models.Index(
                fields=['instrument', 'trading_date'],
                name='broker_dly_instr_date_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['instrument', 'trading_date'],
                name='unique_broker_daily_price',
            ),
        ]

    def __str__(self):
        return f"{self.instrument.ticker} {self.close} @ {self.trading_date}"

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
    brokerage_account = models.ForeignKey(
        BrokerageAccount,
        on_delete=models.SET_NULL,
        related_name='investment_expenses',
        null=True,
        blank=True,
    )
    import_source = models.CharField(max_length=40, blank=True)
    external_id = models.CharField(max_length=120, blank=True)

    class Meta:
        db_table = 'daily_records'
        ordering = ['-date']
        verbose_name = "Daily Record"
        verbose_name_plural = "Daily Records"
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'external_id'],
                condition=~Q(external_id=''),
                name='unique_daily_account_external_transaction',
            ),
        ]

    def __str__(self):
        account_name = self.account.display_name if self.account else self.user.username
        return f"{account_name} – {self.date} – {self.title}"


class InvestmentFunding(models.Model):
    PENDING = 'pending'
    MATCHED = 'matched'
    NEEDS_REVIEW = 'needs_review'
    STATUS_CHOICES = [
        (PENDING, 'Oczekuje'),
        (MATCHED, 'Dopasowane'),
        (NEEDS_REVIEW, 'Wymaga weryfikacji'),
    ]

    expense = models.OneToOneField(Daily, on_delete=models.CASCADE, related_name='investment_funding')
    account = models.ForeignKey(BrokerageAccount, on_delete=models.CASCADE, related_name='investment_fundings')
    cash_operation = models.OneToOneField(
        BrokerageCashOperation,
        on_delete=models.SET_NULL,
        related_name='investment_funding',
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING)
    source_amount = models.DecimalField(max_digits=12, decimal_places=2)
    source_currency = models.CharField(
        max_length=3,
        choices=BrokerageAccount.CURRENCY_CHOICES,
        default='PLN',
    )
    occurred_on = models.DateField()
    match_method = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'investment_fundings'
        ordering = ['-occurred_on', '-id']
        indexes = [
            models.Index(fields=['account', 'occurred_on']),
            models.Index(fields=['status', 'occurred_on']),
        ]

    def __str__(self):
        return f"{self.account} - {self.source_amount} {self.source_currency} ({self.get_status_display()})"

class Income(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='income_records')
    account = models.ForeignKey('FinanceAccount', on_delete=models.CASCADE, related_name='income_records', null=True, blank=True)
    date = models.DateField()
    title = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    source = models.CharField(max_length=100)
    counterparty = models.CharField(max_length=255, blank=True)
    month = models.ForeignKey(Monthly, on_delete=models.CASCADE, related_name='income_entries')
    linked_expense = models.OneToOneField(
        Daily,
        on_delete=models.CASCADE,
        related_name='linked_shared_income',
        null=True,
        blank=True,
    )
    import_source = models.CharField(max_length=40, blank=True)
    external_id = models.CharField(max_length=120, blank=True)

    class Meta:
        db_table = 'income_records'
        ordering = ['-date']
        verbose_name = "Income Record"
        verbose_name_plural = "Income Records"
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'external_id'],
                condition=~Q(external_id=''),
                name='unique_income_account_external_transaction',
            ),
        ]

    def __str__(self):
        account_name = self.account.display_name if self.account else self.user.username
        return f"{account_name} – {self.date} – {self.title}"

# Travel database
class TravelDestinations(models.Model):
    LEISURE = 'leisure'
    BUSINESS = 'business'
    TRAVEL_TYPE_CHOICES = [
        (LEISURE, 'Wakacje'),
        (BUSINESS, 'Delegacja'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='travel_destinations')
    country = CountryField(verbose_name='Kraj')
    city = models.CharField(max_length=255, blank=True)
    start_date = models.DateField()
    end_date = models.DateField()
    budget = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.00'))], default=Decimal('0.00'))
    travel_type = models.CharField(max_length=20, choices=TRAVEL_TYPE_CHOICES, default=LEISURE)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    class Meta:
        db_table = 'travel_destinations'
        ordering = ['-start_date']
        verbose_name = "Travel Destination"
        verbose_name_plural = "Travel Destinations"

    def __str__(self):
        destination = f"{self.city}, {self.country.name}" if self.city else self.country.name
        return f"{self.user.username} – {destination} ({self.start_date} to {self.end_date})"

    @property
    def destination_name(self):
        return self.city or self.country.name

    @property
    def duration_days(self):
        if not self.start_date or not self.end_date:
            return 0
        return max((self.end_date - self.start_date).days + 1, 0)

    @property
    def budget_per_day(self):
        if not self.duration_days:
            return Decimal('0.00')
        return self.budget / Decimal(self.duration_days)

    @property
    def has_coordinates(self):
        return self.latitude is not None and self.longitude is not None

    @property
    def is_business_trip(self):
        return self.travel_type == self.BUSINESS

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
