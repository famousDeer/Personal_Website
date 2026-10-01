from django.utils.functional import SimpleLazyObject

from .account_utils import get_active_finance_account, get_user_finance_accounts


def finance_accounts(request):
    """Konta finansowe dla przełącznika w module finansów.

    Leniwie: zapytania lecą dopiero, gdy szablon naprawdę użyje zmiennej.
    Strony kuchni, garażu i strona startowa nie pytają więc o konta wcale.
    """
    if not request.user.is_authenticated:
        return {}

    return {
        'finance_accounts': SimpleLazyObject(lambda: list(get_user_finance_accounts(request.user))),
        'active_finance_account': SimpleLazyObject(lambda: get_active_finance_account(request)),
    }
