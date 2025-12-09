from django import forms
from .models import Cars, CarTyres, CarService, CarFuelConsumption

# Klasa bazowa dla stylizacji Bootstrapa
class BootstrapFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({'class': 'form-control', 'placeholder': field.label})

class CarForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Cars
        fields = ['brand', 'model', 'yaer', 'odometer', 'fuel_type', 'price']
        labels = {
            'yaer': 'Rok produkcji', # Ładna etykieta dla pola z literówką
            'odometer': 'Przebieg (km)',
            'fuel_type': 'Rodzaj paliwa',
            'price': 'Cena zakupu'
        }

class FuelForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = CarFuelConsumption
        fields = ['date', 'fuel_staton', 'liters', 'price', 'odometer', 'consumption']
        labels = {'fuel_staton': 'Stacja paliw', 'odometer': 'Przebieg przy tankowaniu'}
        widgets = {'date': forms.DateInput(attrs={'type': 'date'})}

class ServiceForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = CarService
        fields = ['date', 'service_type', 'description', 'cost']
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'description': forms.Textarea(attrs={'rows': 3}),
        }

class TyreForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = CarTyres
        fields = ['brand', 'size', 'purchase_date', 'price', 'odometer', 'is_winter']
        widgets = {
            'purchase_date': forms.DateInput(attrs={'type': 'date'}),
            'is_winter': forms.CheckboxInput(attrs={'class': 'form-check-input'}) # Inny styl dla checkboxa
        }