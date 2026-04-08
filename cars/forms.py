from django import forms
from django.forms import inlineformset_factory
from django.utils import timezone
from .models import Cars, CarTyres, CarService, CarFuelConsumption, CarServicePart

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
    date = forms.DateField(
        label='Data',
        input_formats=['%Y-%m-%d', '%d.%m.%Y'],
        widget=forms.DateInput(
            format='%d.%m.%Y',
            attrs={
                'type': 'text',
                'autocomplete': 'off',
                'data-flatpickr': 'date',
            },
        ),
    )

    class Meta:
        model = CarFuelConsumption
        fields = ['date', 'fuel_station', 'liters', 'price', 'odometer', 'price_per_liter']
        labels = {'date': 'Data',
                  'fuel_station': 'Stacja paliw', 
                  'odometer': 'Przebieg przy tankowaniu',
                  'liters': 'Zatankowane litry',
                  'price': 'Cena całkowita (PLN)',
                  'price_per_liter': 'Cena za litr (PLN)'}
        widgets = {}

class ServiceForm(BootstrapFormMixin, forms.ModelForm):
    date = forms.DateField(
        label='Data',
        input_formats=['%d.%m.%Y', '%Y-%m-%d'],
        widget=forms.DateInput(
            format='%d.%m.%Y',
            attrs={
                'type': 'text',
                'autocomplete': 'off',
                'data-flatpickr': 'date',
            },
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound and not self.initial.get('date') and not getattr(self.instance, 'date', None):
            self.initial['date'] = timezone.localdate()

    class Meta:
        model = CarService
        fields = ['date', 'service_type', 'workshop_name', 'description', 'cost']
        labels = {
            'service_type': 'Rodzaj serwisu',
            'workshop_name': 'Nazwa warsztatu',
            'description': 'Opis naprawy',
            'cost': 'Cena całkowita usługi (PLN)',
        }
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4, 'style': 'height: 130px;'}),
        }


class ServicePartForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = CarServicePart
        fields = ['name', 'price']
        labels = {
            'name': 'Nazwa części',
            'price': 'Cena części (PLN)',
        }


class BaseServicePartFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()

        for form in self.forms:
            if not hasattr(form, 'cleaned_data'):
                continue

            if form.cleaned_data.get('DELETE'):
                continue

            name = (form.cleaned_data.get('name') or '').strip()
            price = form.cleaned_data.get('price')

            if not name and price in (None, ''):
                form.cleaned_data['DELETE'] = True
                continue

            if name and price in (None, ''):
                form.add_error('price', 'Podaj cenę części.')

            if price not in (None, '') and not name:
                form.add_error('name', 'Podaj nazwę części.')


ServicePartFormSet = inlineformset_factory(
    CarService,
    CarServicePart,
    form=ServicePartForm,
    formset=BaseServicePartFormSet,
    extra=1,
    can_delete=True,
)

class TyreForm(BootstrapFormMixin, forms.ModelForm):
    purchase_date = forms.DateField(
        label='Data zakupu',
        input_formats=['%Y-%m-%d', '%d.%m.%Y'],
        widget=forms.DateInput(
            format='%d.%m.%Y',
            attrs={
                'type': 'text',
                'autocomplete': 'off',
                'data-flatpickr': 'date',
            },
        ),
    )

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
        widgets = {}
