from datetime import timedelta

from django.db import transaction

from .models import BrokerageCashOperation, InvestmentFunding


INVESTMENT_CATEGORY = 'Inwestycje'
MATCH_WINDOW_DAYS = 2


def _funding_matches_operation(funding, cash_operation):
    if cash_operation is None:
        return False
    if cash_operation.account_id != funding.account_id:
        return False
    if cash_operation.operation_type != BrokerageCashOperation.DEPOSIT:
        return False
    if abs((cash_operation.occurred_at.date() - funding.occurred_on).days) > MATCH_WINDOW_DAYS:
        return False
    if funding.source_currency != cash_operation.currency:
        return False
    return cash_operation.amount == funding.source_amount


def _unmatched_deposit_candidates(funding):
    if funding.source_currency != funding.account.currency:
        return BrokerageCashOperation.objects.none()

    start_date = funding.occurred_on - timedelta(days=MATCH_WINDOW_DAYS)
    end_date = funding.occurred_on + timedelta(days=MATCH_WINDOW_DAYS)
    return (
        BrokerageCashOperation.objects
        .filter(
            account=funding.account,
            operation_type=BrokerageCashOperation.DEPOSIT,
            amount=funding.source_amount,
            currency=funding.source_currency,
            occurred_at__date__gte=start_date,
            occurred_at__date__lte=end_date,
            investment_funding__isnull=True,
        )
        .order_by('occurred_at', 'id')
    )


def _try_match_funding(funding):
    candidates = list(_unmatched_deposit_candidates(funding)[:2])
    if len(candidates) != 1:
        if len(candidates) > 1:
            funding.status = InvestmentFunding.NEEDS_REVIEW
            funding.match_method = ''
            funding.save(update_fields=['status', 'match_method', 'updated_at'])
        return False

    funding.cash_operation = candidates[0]
    funding.status = InvestmentFunding.MATCHED
    funding.match_method = 'Kwota, waluta i data ±2 dni'
    funding.save(update_fields=['cash_operation', 'status', 'match_method', 'updated_at'])
    return True


@transaction.atomic
def sync_investment_funding(expense):
    """Keep the bank-side investment funding projection in sync with a Daily expense."""
    funding = getattr(expense, 'investment_funding', None)
    should_create = expense.category == INVESTMENT_CATEGORY and expense.brokerage_account_id is not None

    if expense.brokerage_account_id and expense.brokerage_account.user_id != expense.user_id:
        raise ValueError('Wybrane konto maklerskie nie należy do użytkownika.')

    if not should_create:
        if funding is not None:
            funding.delete()
        return None

    if funding is None:
        funding = InvestmentFunding.objects.create(
            expense=expense,
            account=expense.brokerage_account,
            source_amount=expense.cost,
            source_currency='PLN',
            occurred_on=expense.date,
        )
    else:
        funding.account = expense.brokerage_account
        funding.source_amount = expense.cost
        funding.source_currency = 'PLN'
        funding.occurred_on = expense.date

        if funding.cash_operation_id and not _funding_matches_operation(funding, funding.cash_operation):
            funding.cash_operation = None
            funding.status = InvestmentFunding.NEEDS_REVIEW
            funding.match_method = ''
        elif funding.cash_operation_id:
            funding.status = InvestmentFunding.MATCHED
        else:
            funding.status = InvestmentFunding.PENDING
            funding.match_method = ''
        funding.save()

    if funding.cash_operation_id is None:
        _try_match_funding(funding)
    return funding


@transaction.atomic
def reconcile_cash_operation(cash_operation):
    """Match an imported external deposit with one unambiguous bank-side funding record."""
    if cash_operation.operation_type != BrokerageCashOperation.DEPOSIT:
        return None
    if getattr(cash_operation, 'investment_funding', None) is not None:
        return cash_operation.investment_funding

    start_date = cash_operation.occurred_at.date() - timedelta(days=MATCH_WINDOW_DAYS)
    end_date = cash_operation.occurred_at.date() + timedelta(days=MATCH_WINDOW_DAYS)
    candidates = list(
        InvestmentFunding.objects
        .filter(
            account=cash_operation.account,
            cash_operation__isnull=True,
            source_currency=cash_operation.currency,
            source_amount=cash_operation.amount,
            occurred_on__gte=start_date,
            occurred_on__lte=end_date,
        )
        .order_by('occurred_on', 'id')[:2]
    )
    if len(candidates) != 1:
        if len(candidates) > 1:
            InvestmentFunding.objects.filter(id__in=[item.id for item in candidates]).update(
                status=InvestmentFunding.NEEDS_REVIEW,
                match_method='',
            )
        return None

    funding = candidates[0]
    funding.cash_operation = cash_operation
    funding.status = InvestmentFunding.MATCHED
    funding.match_method = 'Kwota, waluta i data ±2 dni'
    funding.save(update_fields=['cash_operation', 'status', 'match_method', 'updated_at'])
    return funding
