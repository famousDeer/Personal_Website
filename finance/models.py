# finance/models.py
from django.db import models
from django.contrib.auth import get_user_model
from django.core.validators import MinValueValidator
from decimal import Decimal
from django_countries.fields import CountryField

User = get_user_model()

# Expense and Income database
class Monthly(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='monthly_records')
    date = models.DateField()
    total_income = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    total_expense = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))

    class Meta:
        db_table = 'monthly_records'
        ordering = ['date']
        verbose_name = "Monthly Record"
        verbose_name_plural = "Monthly Records"
        constraints = [
            models.UniqueConstraint(fields=['user', 'date'], name='unique_user_month'),
        ]
        indexes = [
            models.Index(fields=['user', 'date']),
        ]

    def __str__(self):
        return f"{self.user.username} – {self.date}"

class Daily(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='daily_records')
    date = models.DateField()
    title = models.CharField(max_length=255)
    cost = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
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
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    source = models.CharField(max_length=100)
    month = models.ForeignKey(Monthly, on_delete=models.CASCADE, related_name='income_entries')

    class Meta:
        db_table = 'income_records'
        ordering = ['-date']
        verbose_name = "Income Record"
        verbose_name_plural = "Income Records"

    def __str__(self):
        return f"{self.user.username} – {self.date} – {self.title}"

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

# Cars database
class Cars(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='cars')
    brand = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    yaer = models.PositiveIntegerField()
    odometer = models.PositiveIntegerField()
    fuel_type = models.CharField(max_length=50)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = 'cars'
        ordering = ['brand', 'model']
        verbose_name = "Car"
        verbose_name_plural = "Cars"

    def __str__(self):
        return f"{self.brand} {self.model} ({self.year})"

class CarTyres(models.Model):
    car = models.ForeignKey(Cars, on_delete=models.CASCADE, related_name='tyres')
    brand = models.CharField(max_length=100)
    size = models.CharField(max_length=50)
    purchase_date = models.DateField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    odometer = models.PositiveIntegerField()
    is_winter = models.BooleanField(default=False)

    class Meta:
        db_table = 'car_tyres'
        ordering = ['-odometer']
        verbose_name = "Car Tyre"
        verbose_name_plural = "Car Tyres"

    def __str__(self):
        return f"{self.brand} {self.size} for {self.car.brand} {self.car.model}"

class CarService(models.Model):
    car = models.ForeignKey(Cars, on_delete=models.CASCADE, related_name='services')
    date = models.DateField()
    service_type = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = 'car_services'
        ordering = ['-date']
        verbose_name = "Car Service"
        verbose_name_plural = "Car Services"

    def __str__(self):
        return f"{self.car.brand} {self.car.model} - {self.service_type} on {self.date}"
    
class CarFuelConsumption(models.Model):
    car = models.ForeignKey(Cars, on_delete=models.CASCADE, related_name='fuel_consumptions')
    fuel_staton = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    date = models.DateField()
    liters = models.DecimalField(max_digits=6, decimal_places=2)
    odometer = models.PositiveIntegerField()
    consumption = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)

    class Meta:
        db_table = 'car_fuel_consumptions'
        ordering = ['-date']
        verbose_name = "Car Fuel Consumption"
        verbose_name_plural = "Car Fuel Consumptions"

    def __str__(self):
        return f"{self.car.brand} {self.car.model} - {self.liters}L on {self.date}"