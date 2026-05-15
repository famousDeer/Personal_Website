from django.db import models
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from decimal import Decimal
from django.db.models import Sum

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
    odometer = models.PositiveIntegerField(blank=True, null=True)
    is_winter = models.BooleanField(default=False)

    class Meta:
        db_table = 'car_tyres'
        ordering = ['-purchase_date', '-id']
        verbose_name = "Car Tyre"
        verbose_name_plural = "Car Tyres"

    def __str__(self):
        return f"{self.brand} {self.width}/{self.aspect_ratio}/{self.diameter} for {self.car.brand} {self.car.model}"

    @property
    def is_mounted(self):
        return self.active_usage is not None

    @property
    def active_usage(self):
        prefetched_periods = getattr(self, '_prefetched_objects_cache', {}).get('usage_periods')
        if prefetched_periods is not None:
            return next((usage for usage in prefetched_periods if usage.is_open), None)
        return self.usage_periods.filter(removed_date__isnull=True, removed_odometer__isnull=True).first()

    @property
    def total_driven_distance(self):
        total = 0
        has_distance = False
        prefetched_periods = getattr(self, '_prefetched_objects_cache', {}).get('usage_periods')
        usage_periods = prefetched_periods if prefetched_periods is not None else self.usage_periods.all()

        for usage in usage_periods:
            distance = usage.driven_distance
            if distance is not None:
                total += distance
                has_distance = True

        return total if has_distance else None


class CarTyreUsage(models.Model):
    tyre = models.ForeignKey(CarTyres, on_delete=models.CASCADE, related_name='usage_periods')
    mounted_date = models.DateField()
    mounted_odometer = models.PositiveIntegerField()
    removed_date = models.DateField(blank=True, null=True)
    removed_odometer = models.PositiveIntegerField(blank=True, null=True)

    class Meta:
        db_table = 'car_tyre_usage'
        ordering = ['-mounted_date', '-mounted_odometer']
        verbose_name = "Car Tyre Usage"
        verbose_name_plural = "Car Tyre Usages"

    def __str__(self):
        return f"{self.tyre.brand} mounted on {self.mounted_date}"

    @property
    def is_open(self):
        return self.removed_date is None and self.removed_odometer is None

    @property
    def driven_distance(self):
        end_odometer = self.removed_odometer
        if end_odometer is None and self.tyre_id and getattr(self, 'tyre', None):
            end_odometer = self.tyre.car.odometer

        if end_odometer is None:
            return None

        return max(end_odometer - self.mounted_odometer, 0)

    def clean(self):
        errors = {}

        if self.removed_date and self.mounted_date and self.removed_date < self.mounted_date:
            errors['removed_date'] = 'Data zdjęcia nie może być wcześniejsza niż data założenia.'

        if (
            self.removed_odometer is not None
            and self.mounted_odometer is not None
            and self.removed_odometer < self.mounted_odometer
        ):
            errors['removed_odometer'] = 'Przebieg przy zdjęciu nie może być mniejszy niż przy założeniu.'

        if (self.removed_date is None) != (self.removed_odometer is None):
            message = 'Podaj jednocześnie datę i przebieg zdjęcia opon albo zostaw oba pola puste.'
            errors['removed_date'] = message
            errors['removed_odometer'] = message

        if self.is_open and self.tyre_id:
            open_usage = self.tyre.usage_periods.filter(removed_date__isnull=True, removed_odometer__isnull=True)
            if self.pk:
                open_usage = open_usage.exclude(pk=self.pk)
            if open_usage.exists():
                errors['removed_date'] = 'Ten zestaw ma już otwarty okres użycia. Najpierw uzupełnij zdjęcie poprzedniego wpisu.'

        if errors:
            raise ValidationError(errors)

class CarService(models.Model):
    car = models.ForeignKey(Cars, on_delete=models.CASCADE, related_name='services')
    date = models.DateField()
    service_type = models.CharField(max_length=100)
    workshop_name = models.CharField(max_length=150, blank=True)
    description = models.TextField()
    cost = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = 'car_services'
        ordering = ['-date']
        verbose_name = "Car Service"
        verbose_name_plural = "Car Services"

    def __str__(self):
        return f"{self.car.brand} {self.car.model} - {self.service_type} on {self.date}"

    @property
    def parts_total(self):
        prefetched_parts = getattr(self, '_prefetched_objects_cache', {}).get('parts')
        if prefetched_parts is not None:
            return sum((part.price for part in prefetched_parts), Decimal('0.00'))
        return self.parts.aggregate(total=Sum('price'))['total'] or Decimal('0.00')


class CarServicePart(models.Model):
    service = models.ForeignKey(CarService, on_delete=models.CASCADE, related_name='parts')
    name = models.CharField(max_length=150)
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))],
    )

    class Meta:
        db_table = 'car_service_parts'
        ordering = ['id']
        verbose_name = "Car Service Part"
        verbose_name_plural = "Car Service Parts"

    def __str__(self):
        return f"{self.name} ({self.price} zł)"
    
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
