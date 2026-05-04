from .account_utils import get_active_finance_account, get_user_finance_accounts


def finance_accounts(request):
    if not request.user.is_authenticated:
        return {}

    available_accounts = list(get_user_finance_accounts(request.user))
    active_account = get_active_finance_account(request)
    return {
        'finance_accounts': available_accounts,
        'active_finance_account': active_account,
    }
