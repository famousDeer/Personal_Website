from django import forms
from .models import TravelDestinations

class TravelDestinationForm(forms.ModelForm):
    start_date = forms.DateField(
        input_formats=['%Y-%m-%d', '%d.%m.%Y'],
        widget=forms.DateInput(
            format='%d.%m.%Y',
            attrs={
                'class': 'form-control',
                'type': 'text',
                'autocomplete': 'off',
                'data-flatpickr': 'date',
            },
        ),
    )
    end_date = forms.DateField(
        input_formats=['%Y-%m-%d', '%d.%m.%Y'],
        widget=forms.DateInput(
            format='%d.%m.%Y',
            attrs={
                'class': 'form-control',
                'type': 'text',
                'autocomplete': 'off',
                'data-flatpickr': 'date',
            },
        ),
    )

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
            'city': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'np. Warszawa'}),
            'budget': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'np. 2000.00'}),
        }
