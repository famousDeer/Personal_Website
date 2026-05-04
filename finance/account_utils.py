from decimal import Decimal

from django.db.models import Sum

from .models import Daily, FinanceAccount, Income, Monthly

ACTIVE_FINANCE_ACCOUNT_SESSION_KEY = 'active_finance_account_id'
TRANSFER_TO_SHARED_CATEGORY = 'Wpłata do wspólnego z mBank'
TRANSFER_INCOME_SOURCE = 'Wpłata na konto wspólne'


def ensure_personal_finance_account(user):
    account, created = FinanceAccount.objects.get_or_create(
        owner=user,
        account_type=FinanceAccount.PERSONAL,
        defaults={'name': 'Konto osobiste'},
    )
    if created or not account.members.filter(id=user.id).exists():
        account.members.add(user)
    return account


def get_user_finance_accounts(user):
    ensure_personal_finance_account(user)
    return FinanceAccount.objects.filter(members=user).distinct().order_by('account_type', 'name')


def get_available_shared_accounts(user):
    return get_user_finance_accounts(user).filter(account_type=FinanceAccount.SHARED)


def get_active_finance_account(request):
    personal_account = ensure_personal_finance_account(request.user)
    available_accounts = get_user_finance_accounts(request.user)
    account_id = request.session.get(ACTIVE_FINANCE_ACCOUNT_SESSION_KEY)
    active_account = available_accounts.filter(id=account_id).first()

    if active_account is None:
        active_account = personal_account
        request.session[ACTIVE_FINANCE_ACCOUNT_SESSION_KEY] = personal_account.id

    return active_account


def set_active_finance_account(request, account):
    request.session[ACTIVE_FINANCE_ACCOUNT_SESSION_KEY] = account.id


def get_or_create_monthly_record(*, user, account, month_date, for_update=False):
    queryset = Monthly.objects
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.get_or_create(
        account=account,
        date=month_date,
        defaults={'user': user, 'total_income': Decimal('0.00'), 'total_expense': Decimal('0.00')},
    )


def recalculate_monthly_record(monthly_record):
    monthly_record.total_income = Income.objects.filter(
        account=monthly_record.account,
        month=monthly_record,
    ).aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')
    monthly_record.total_expense = Daily.objects.filter(
        account=monthly_record.account,
        month=monthly_record,
    ).aggregate(Sum('cost'))['cost__sum'] or Decimal('0.00')
    monthly_record.save(update_fields=['total_income', 'total_expense'])


def sync_shared_account_transfer(expense):
    should_transfer = (
        expense.account.account_type == FinanceAccount.PERSONAL
        and expense.category == TRANSFER_TO_SHARED_CATEGORY
        and expense.transfer_target_account is not None
        and expense.transfer_target_account.account_type == FinanceAccount.SHARED
    )

    linked_income = getattr(expense, 'linked_shared_income', None)

    if not should_transfer:
        if linked_income:
            target_month = linked_income.month
            linked_income.delete()
            recalculate_monthly_record(target_month)
        if expense.transfer_target_account_id and expense.category != TRANSFER_TO_SHARED_CATEGORY:
            expense.transfer_target_account = None
            expense.save(update_fields=['transfer_target_account'])
        return

    target_month, _ = get_or_create_monthly_record(
        user=expense.user,
        account=expense.transfer_target_account,
        month_date=expense.date.replace(day=1),
        for_update=True,
    )

    income_defaults = {
        'user': expense.user,
        'account': expense.transfer_target_account,
        'date': expense.date,
        'title': expense.title or f'Wpłata od {expense.user.username}',
        'amount': expense.cost,
        'source': TRANSFER_INCOME_SOURCE,
        'month': target_month,
    }

    if linked_income:
        old_target_month = linked_income.month
        linked_income.user = expense.user
        linked_income.account = expense.transfer_target_account
        linked_income.date = expense.date
        linked_income.title = income_defaults['title']
        linked_income.amount = expense.cost
        linked_income.source = TRANSFER_INCOME_SOURCE
        linked_income.month = target_month
        linked_income.save()
        recalculate_monthly_record(old_target_month)
    else:
        Income.objects.create(linked_expense=expense, **income_defaults)

    recalculate_monthly_record(target_month)
