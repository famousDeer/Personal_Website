from django import forms
from .models import TravelDestinations, Cars, CarTyres, CarService, CarFuelConsumption

class TravelDestinationForm(forms.ModelForm):
    class Meta:
        model = TravelDestinations
        fields = ['country', 'start_date', 'end_date', 'city', 'budget']
        labels = {
            'country': 'Kraj podrózy',
            'start_date': 'Data rozpoczęcia',
            'end_date': 'Data zakończenia',
            'city': 'Miasto',
            'budget': 'Budżet (PLN)',
        }
        widgets = {
            'country': forms.Select(attrs={'class': 'form-select'}),
            'start_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'end_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'city': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'np. Warszawa'}),
            'budget': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'np. 2000.00'}),
        }

class Cars(forms.ModelForm):
    class Meta:
        model = Cars
        fields = ['brand', 'model', 'yaer', 'odometer', 'fuel_type', 'price']
        labels = {
            'brand': 'Marka',
            'model': 'Model',
            'yaer': 'Rok produkcji',
            'odometer': 'Przebieg (km)',
            'fuel_type': 'Rodzaj paliwa',
            'price': 'Cena zakupu',
        }
        widgets = {
            'brand': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'np. Toyota'}),
            'model': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'np. Corolla'}),
            'yaer': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'np. 2020'}),
            'odometer': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'np. 150000'}),
            'fuel_type': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'np. Benzyna'}),
            'price': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'np. 50000.00'}),
        }