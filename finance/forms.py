from django import forms
from .models import TravelDestinations

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