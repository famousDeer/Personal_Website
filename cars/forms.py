from django import forms
from .models import Cars, CarTyres, CarService, CarFuelConsumption

# Klasa bazowa dla stylizacji Bootstrapa
class BootstrapFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.update({'class': 'form-check-input'})
            else:
                field.widget.attrs.update({
                    'class': 'form-control', 
                    'placeholder': field.label
                })

class CarForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Cars
        fields = ['brand', 'model', 'year', 'odometer', 'fuel_type', 'price']
        labels = {
            'brand': 'Marka',
            'model': 'Model',
            'year': 'Rok produkcji',
            'odometer': 'Przebieg (km)',
            'fuel_type': 'Rodzaj paliwa',
            'price': 'Cena zakupu'
        }
        widgets = {
            'fuel_type': forms.Select(choices=[
                ('Benzyna', 'Benzyna'),
                ('Diesel', 'Diesel'),
                ('Elektryczny', 'Elektryczny'),
            ])
        }

class FuelForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = CarFuelConsumption
        fields = ['date', 'fuel_station', 'liters', 'price', 'odometer', 'price_per_liter']
        labels = {'date': 'Data',
                  'fuel_station': 'Stacja paliw', 
                  'odometer': 'Przebieg przy tankowaniu',
                  'liters': 'Zatankowane litry',
                  'price': 'Cena całkowita (PLN)',
                  'price_per_liter': 'Cena za litr (PLN)'}
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
        fields = ['brand', 'width', 'aspect_ratio', 'diameter', 'purchase_date', 'price', 'odometer', 'is_winter']
        labels = {'brand': 'Marka opon',
                  'width': 'Szerokość (mm)',
                  'aspect_ratio': 'Profil (%)',
                  'diameter': 'Średnica (cale)',
                  'purchase_date': 'Data zakupu',
                  'price': 'Cena całkowita (PLN)',
                  'odometer': 'Przebieg przy montażu (km)',
                  'is_winter': 'Opony zimowe'}
        widgets = {
            'purchase_date': forms.DateInput(attrs={'type': 'date'}),
        }