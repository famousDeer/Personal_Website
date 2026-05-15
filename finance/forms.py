from django import forms
from django.utils import timezone

from .market_data import MarketDataError, fetch_transaction_market_price
from .models import (
    BrokerageAccount,
    BrokerageDividend,
    BrokerageInstrument,
    BrokerageTransaction,
    TravelDestinations,
)


class BootstrapFinanceFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.update({'class': 'form-check-input'})
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.update({'class': 'form-select'})
            else:
                field.widget.attrs.update({'class': 'form-control', 'placeholder': field.label})


class BrokerageAccountForm(BootstrapFinanceFormMixin, forms.ModelForm):
    class Meta:
        model = BrokerageAccount
        fields = ['name', 'broker', 'account_type', 'currency']
        labels = {
            'name': 'Nazwa konta',
            'broker': 'Broker',
            'account_type': 'Typ konta',
            'currency': 'Waluta konta',
        }


class BrokerageInstrumentForm(BootstrapFinanceFormMixin, forms.ModelForm):
    isin = forms.CharField(
        label='ISIN',
        max_length=12,
        help_text='ISIN będzie używany do wyszukania symbolu notowania przy odświeżaniu ceny.',
    )

    def clean_isin(self):
        return self.cleaned_data['isin'].strip().upper()

    def save(self, commit=True):
        instrument = super().save(commit=False)
        if not instrument.ticker:
            instrument.ticker = instrument.isin
        if commit:
            instrument.save()
        return instrument

    class Meta:
        model = BrokerageInstrument
        fields = ['name', 'isin', 'price_symbol', 'exchange', 'asset_type', 'currency', 'last_price']
        labels = {
            'name': 'Nazwa instrumentu',
            'price_symbol': 'Symbol ceny',
            'exchange': 'Giełda',
            'asset_type': 'Typ aktywa',
            'currency': 'Waluta notowania',
            'last_price': 'Ostatnia cena',
        }
        help_texts = {
            'price_symbol': 'Opcjonalnie. Używany do pobierania ceny, gdy automatycznie znaleziony symbol nie działa, np. UBI.PA.',
        }


class BrokerageTransactionForm(BootstrapFinanceFormMixin, forms.ModelForm):
    instrument_name = forms.CharField(
        label='Nazwa instrumentu',
        max_length=160,
        help_text='Nazwa jest opisowa, np. Kruk. Do pobierania ceny używany jest ISIN.',
    )
    isin = forms.CharField(
        label='ISIN',
        max_length=12,
        help_text='Dwunastoznakowy kod papieru wartościowego, np. PLKRK0000010 dla KRUK.',
    )
    asset_type = forms.ChoiceField(label='Typ aktywa', choices=BrokerageInstrument.ASSET_TYPE_CHOICES, initial=BrokerageInstrument.STOCK)
    currency = forms.ChoiceField(label='Waluta notowania', choices=BrokerageAccount.CURRENCY_CHOICES, initial='PLN')
    trade_date = forms.DateField(
        label='Data transakcji',
        input_formats=['%Y-%m-%d', '%d.%m.%Y'],
        widget=forms.DateInput(
            format='%d.%m.%Y',
            attrs={'type': 'text', 'autocomplete': 'off', 'data-flatpickr': 'date'},
        ),
    )
    trade_time = forms.TimeField(
        label='Godzina transakcji',
        input_formats=['%H:%M', '%H:%M:%S'],
        widget=forms.TimeInput(format='%H:%M', attrs={'type': 'time'}),
    )
    price = forms.DecimalField(
        label='Cena za sztukę',
        max_digits=14,
        decimal_places=4,
        required=False,
        help_text='Zostaw puste, żeby spróbować pobrać cenę rynkową. Wpisz swoją cenę, jeśli cena brokera jest inna.',
    )
    market_price_confirmed = forms.BooleanField(required=False, widget=forms.HiddenInput)
    market_price_value = forms.DecimalField(required=False, max_digits=14, decimal_places=4, widget=forms.HiddenInput)
    market_price_source_value = forms.CharField(required=False, widget=forms.HiddenInput)
    market_symbol_value = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.market_price_fetched = None
        self.market_price_source = ''
        self.resolved_symbol = ''
        self.resolved_market_data = {}
        self.fields['account'].queryset = BrokerageAccount.objects.filter(user=user) if user else BrokerageAccount.objects.none()
        if not self.is_bound and not self.initial.get('trade_date') and not getattr(self.instance, 'trade_date', None):
            self.initial['trade_date'] = timezone.localdate()
        if self.instance and self.instance.pk:
            self.initial.setdefault('instrument_name', self.instance.instrument.name)
            self.initial.setdefault('isin', self.instance.instrument.isin)
            self.initial.setdefault('asset_type', self.instance.instrument.asset_type)
            self.initial.setdefault('currency', self.instance.instrument.currency)

    def clean_isin(self):
        return self.cleaned_data['isin'].strip().upper()

    def clean(self):
        cleaned_data = super().clean()
        price = cleaned_data.get('price')
        isin = cleaned_data.get('isin')
        trade_date = cleaned_data.get('trade_date')
        trade_time = cleaned_data.get('trade_time')
        existing_market_price = cleaned_data.get('market_price_value')
        existing_market_source = cleaned_data.get('market_price_source_value', '')
        existing_market_symbol = cleaned_data.get('market_symbol_value', '')

        if price is None and isin and trade_date and trade_time:
            try:
                market_data = fetch_transaction_market_price(
                    isin,
                    trade_date,
                    trade_time,
                    '',
                    cleaned_data.get('currency', ''),
                )
            except MarketDataError as exc:
                self.add_error(
                    'price',
                    f'Nie udało się pobrać ceny rynkowej: {exc}. Wpisz cenę z brokera ręcznie.',
                )
            else:
                self.market_price_fetched = market_data['price']
                self.market_price_source = market_data['source']
                self.resolved_symbol = market_data.get('symbol', '')
                self.resolved_market_data = market_data
                cleaned_data['price'] = self.market_price_fetched
                cleaned_data['market_price_value'] = self.market_price_fetched
                cleaned_data['market_price_source_value'] = self.market_price_source
                cleaned_data['market_symbol_value'] = self.resolved_symbol
        elif existing_market_price is not None:
            self.market_price_fetched = existing_market_price
            self.market_price_source = existing_market_source
            self.resolved_symbol = existing_market_symbol

        if not self.resolved_symbol and self.instance and self.instance.pk:
            self.resolved_symbol = self.instance.instrument.ticker
        elif not self.resolved_symbol and isin:
            self.resolved_symbol = isin

        return cleaned_data

    def save(self, commit=True):
        instrument, _ = BrokerageInstrument.objects.update_or_create(
            user=self.user,
            ticker=self.resolved_symbol,
            defaults={
                'name': self.cleaned_data['instrument_name'],
                'isin': self.cleaned_data['isin'],
                'exchange': '',
                'asset_type': self.cleaned_data['asset_type'],
                'currency': self.cleaned_data['currency'],
            },
        )
        if self.resolved_market_data.get('name') and instrument.name == self.cleaned_data['instrument_name']:
            instrument.name = self.resolved_market_data['name']
        if self.resolved_market_data.get('exchange'):
            instrument.exchange = self.resolved_market_data['exchange']
        if self.resolved_market_data.get('currency'):
            instrument.currency = self.resolved_market_data['currency']
        elif not instrument.currency and self.cleaned_data.get('currency'):
            instrument.currency = self.cleaned_data['currency']

        if self.market_price_fetched is not None:
            instrument.last_price = self.market_price_fetched
            instrument.last_price_at = timezone.now()
            instrument.market_data_source = self.market_price_source
            if commit:
                instrument.save(update_fields=['name', 'exchange', 'currency', 'last_price', 'last_price_at', 'market_data_source'])

        transaction = super().save(commit=False)
        transaction.instrument = instrument
        if self.market_price_fetched is not None:
            transaction.market_price = self.market_price_fetched
            transaction.market_price_source = self.market_price_source
        elif transaction.price:
            transaction.market_price = None
            transaction.market_price_source = ''

        if commit:
            transaction.save()
        return transaction

    class Meta:
        model = BrokerageTransaction
        fields = ['account', 'transaction_type', 'trade_date', 'trade_time', 'quantity', 'price', 'fees', 'notes']
        labels = {
            'account': 'Konto maklerskie',
            'transaction_type': 'Typ transakcji',
            'quantity': 'Liczba sztuk',
            'fees': 'Prowizje i opłaty',
            'notes': 'Notatka',
        }


class BrokerageDividendForm(BootstrapFinanceFormMixin, forms.ModelForm):
    ex_dividend_date = forms.DateField(
        label='Dzień odcięcia prawa',
        required=False,
        input_formats=['%Y-%m-%d', '%d.%m.%Y'],
        widget=forms.DateInput(
            format='%d.%m.%Y',
            attrs={'type': 'text', 'autocomplete': 'off', 'data-flatpickr': 'date'},
        ),
    )
    payment_date = forms.DateField(
        label='Dzień wypłaty',
        input_formats=['%Y-%m-%d', '%d.%m.%Y'],
        widget=forms.DateInput(
            format='%d.%m.%Y',
            attrs={'type': 'text', 'autocomplete': 'off', 'data-flatpickr': 'date'},
        ),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['account'].queryset = BrokerageAccount.objects.filter(user=user) if user else BrokerageAccount.objects.none()
        self.fields['instrument'].queryset = BrokerageInstrument.objects.filter(user=user) if user else BrokerageInstrument.objects.none()

    class Meta:
        model = BrokerageDividend
        fields = ['account', 'instrument', 'ex_dividend_date', 'payment_date', 'gross_amount_per_share', 'currency', 'tax_rate', 'status']
        labels = {
            'account': 'Konto maklerskie',
            'instrument': 'Instrument',
            'gross_amount_per_share': 'Dywidenda brutto na akcję',
            'currency': 'Waluta dywidendy',
            'tax_rate': 'Podatek (%)',
            'status': 'Status',
        }

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

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get('start_date')
        end_date = cleaned_data.get('end_date')
        if start_date and end_date and end_date < start_date:
            self.add_error('end_date', 'Data zakończenia nie może być wcześniejsza niż data rozpoczęcia.')
        return cleaned_data
