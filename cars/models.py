from django.db import models
from django.contrib.auth import get_user_model
from django.core.validators import MinValueValidator
from decimal import Decimal

User = get_user_model()

# Cars database
class Cars(models.Model):
    FUEL_TYPE_CHOICES = [
        ('Benzyna', 'Benzyna'),
        ('Diesel', 'Diesel'),
        ('Elektryczny', 'Elektryczny'),
        ('Hybryda', 'Hybryda'), 
        ('LPG', 'LPG'),         
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='cars')
    brand = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    year = models.PositiveIntegerField()
    odometer = models.PositiveIntegerField()
    fuel_type = models.CharField(max_length=50, choices=FUEL_TYPE_CHOICES, default='Benzyna')
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
    width = models.PositiveIntegerField(default=0)
    aspect_ratio = models.PositiveIntegerField(default=0)
    diameter = models.PositiveIntegerField(default=0)
    purchase_date = models.DateField()
    quantity = models.PositiveIntegerField(default=1,validators=[MinValueValidator(1)])
    price = models.DecimalField(max_digits=10, decimal_places=2)
    odometer = models.PositiveIntegerField()
    is_winter = models.BooleanField(default=False)

    class Meta:
        db_table = 'car_tyres'
        ordering = ['-odometer']
        verbose_name = "Car Tyre"
        verbose_name_plural = "Car Tyres"

    def __str__(self):
        return f"{self.brand} {self.width}/{self.aspect_ratio}/{self.diameter} for {self.car.brand} {self.car.model}"

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
    fuel_station = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    date = models.DateField()
    liters = models.DecimalField(max_digits=6, decimal_places=2)
    odometer = models.PositiveIntegerField()
    price_per_liter = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)
    consumption = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)

    class Meta:
        db_table = 'car_fuel_consumptions'
        ordering = ['-date']
        verbose_name = "Car Fuel Consumption"
        verbose_name_plural = "Car Fuel Consumptions"

    def __str__(self):
        return f"{self.car.brand} {self.car.model} - {self.liters}L on {self.date}"
