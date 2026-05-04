# accounts/forms.py
from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import get_user_model
from finance.models import FinanceAccount

User = get_user_model()

class SignUpForm(UserCreationForm):
    email = forms.EmailField(required=True, widget=forms.EmailInput(attrs={'class': 'form-control'}))
    username = forms.CharField(max_length=150,
                               widget=forms.TextInput(attrs={'class': 'form-control'}))
    password1 = forms.CharField(label="Hasło",
                                widget=forms.PasswordInput(attrs={'class': 'form-control'}))
    password2 = forms.CharField(label="Powtórz hasło",
                                widget=forms.PasswordInput(attrs={'class': 'form-control'}))

    class Meta:
        model = User
        fields = ("username", "email", "password1", "password2")


class ProfileUpdateForm(forms.ModelForm):
    email = forms.EmailField(required=True, widget=forms.EmailInput(attrs={'class': 'form-control'}))
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )

    class Meta:
        model = User
        fields = ('username', 'email')


class SharedAccountCreateForm(forms.Form):
    name = forms.CharField(
        max_length=255,
        label='Nazwa konta wspólnego',
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'np. Dom i rachunki'})
    )
    partner_username = forms.CharField(
        max_length=150,
        label='Login drugiego użytkownika',
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'np. anna'})
    )

    def __init__(self, *args, **kwargs):
        self.current_user = kwargs.pop('current_user')
        super().__init__(*args, **kwargs)

    def clean_partner_username(self):
        partner_username = self.cleaned_data['partner_username'].strip()
        try:
            partner = User.objects.get(username=partner_username)
        except User.DoesNotExist as exc:
            raise forms.ValidationError('Nie znaleziono użytkownika o takim loginie.') from exc

        if partner == self.current_user:
            raise forms.ValidationError('Nie możesz utworzyć konta wspólnego sam ze sobą.')

        self.cleaned_data['partner'] = partner
        return partner_username


class SharedAccountUpdateForm(forms.ModelForm):
    partner_username = forms.CharField(
        max_length=150,
        required=False,
        label='Drugi użytkownik',
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'np. anna'})
    )

    def __init__(self, *args, **kwargs):
        self.current_user = kwargs.pop('current_user')
        self.can_manage_membership = kwargs.pop('can_manage_membership', False)
        super().__init__(*args, **kwargs)

        other_member = self.instance.members.exclude(id=self.current_user.id).order_by('username').first()
        if 'partner_username' in self.fields:
            self.fields['partner_username'].initial = other_member.username if other_member else ''

        if not self.can_manage_membership:
            self.fields.pop('partner_username', None)

    def clean_partner_username(self):
        partner_username = self.cleaned_data['partner_username'].strip()
        if not partner_username:
            raise forms.ValidationError('Podaj login drugiego użytkownika.')

        try:
            partner = User.objects.get(username=partner_username)
        except User.DoesNotExist as exc:
            raise forms.ValidationError('Nie znaleziono użytkownika o takim loginie.') from exc

        if partner == self.current_user:
            raise forms.ValidationError('Drugim użytkownikiem musi być ktoś inny niż Ty.')

        self.cleaned_data['partner'] = partner
        return partner_username

    class Meta:
        model = FinanceAccount
        fields = ('name',)
        labels = {
            'name': 'Nazwa konta wspólnego',
        }
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'np. Dom i rachunki'}),
        }
