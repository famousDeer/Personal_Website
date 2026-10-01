from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils.functional import SimpleLazyObject


def _signup_enabled():
    return settings.ALLOW_PUBLIC_SIGNUP or not get_user_model().objects.exists()


def account_access(request):
    # Leniwie: zapytanie o użytkowników tylko na stronach, które pokazują
    # link do rejestracji (dla zalogowanych żadna go nie pokazuje).
    return {
        'public_signup_enabled': SimpleLazyObject(_signup_enabled),
    }
