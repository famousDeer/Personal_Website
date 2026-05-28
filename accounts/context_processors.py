from django.conf import settings
from django.contrib.auth import get_user_model


def account_access(request):
    User = get_user_model()
    return {
        'public_signup_enabled': settings.ALLOW_PUBLIC_SIGNUP or not User.objects.exists(),
    }
