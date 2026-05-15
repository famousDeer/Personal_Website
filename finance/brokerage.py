from collections import defaultdict
from decimal import Decimal

from django.utils import timezone

from .models import BrokerageAccount, BrokerageDividend, BrokerageTransaction


BELKA_TAX_RATE = Decimal('19.00')
ZERO = Decimal('0.00')


def money(value):
    return value.quantize(Decimal('0.01'))


def get_quantity(account, instrument, as_of=None):
    transactions = BrokerageTransaction.objects.filter(account=account, instrument=instrument)
    if as_of is not None:
        transactions = transactions.filter(trade_date__lte=as_of)

    quantity = Decimal('0')
    for transaction in transactions.order_by('trade_date', 'id'):
        if transaction.transaction_type == BrokerageTransaction.BUY:
            quantity += transaction.quantity
        else:
            quantity -= transaction.quantity
    return max(quantity, Decimal('0'))


def build_portfolio_summary(user):
    accounts = list(BrokerageAccount.objects.filter(user=user))
    transactions = (
        BrokerageTransaction.objects
        .filter(account__user=user)
        .select_related('account', 'instrument')
        .order_by('trade_date', 'id')
    )

    lots = defaultdict(list)
    realized_tax_by_currency = defaultdict(lambda: ZERO)
    realized_gain_by_currency = defaultdict(lambda: ZERO)

    for transaction in transactions:
        key = (transaction.account_id, transaction.instrument_id)

        if transaction.transaction_type == BrokerageTransaction.BUY:
            total_cost = transaction.gross_value + transaction.fees
            lots[key].append({
                'quantity': transaction.quantity,
                'unit_cost': total_cost / transaction.quantity,
                'transaction': transaction,
            })
            continue

        quantity_to_sell = transaction.quantity
        cost_basis = ZERO
        while quantity_to_sell > 0 and lots[key]:
            lot = lots[key][0]
            consumed_quantity = min(quantity_to_sell, lot['quantity'])
            cost_basis += consumed_quantity * lot['unit_cost']
            lot['quantity'] -= consumed_quantity
            quantity_to_sell -= consumed_quantity
            if lot['quantity'] <= 0:
                lots[key].pop(0)

        proceeds = (transaction.quantity * transaction.price) - transaction.fees
        gain = proceeds - cost_basis
        realized_gain_by_currency[transaction.instrument.currency] += gain
        if not transaction.account.is_tax_exempt and gain > 0:
            realized_tax_by_currency[transaction.instrument.currency] += gain * (BELKA_TAX_RATE / Decimal('100'))

    positions = []
    account_totals = {
        account.id: {
            'account': account,
            'currencies': defaultdict(lambda: {'currency': '', 'value': ZERO, 'cost': ZERO, 'unrealized': ZERO}),
        }
        for account in accounts
    }
    currency_totals = defaultdict(lambda: {'currency': '', 'value': ZERO, 'cost': ZERO, 'unrealized': ZERO})
    planned_dividend_net_by_currency = defaultdict(lambda: ZERO)

    for (account_id, instrument_id), open_lots in lots.items():
        quantity = sum((lot['quantity'] for lot in open_lots), Decimal('0'))
        if quantity <= 0:
            continue

        first_transaction = open_lots[0]['transaction']
        account = first_transaction.account
        instrument = first_transaction.instrument
        currency = instrument.currency
        cost = sum((lot['quantity'] * lot['unit_cost'] for lot in open_lots), ZERO)
        current_value = quantity * instrument.last_price if instrument.last_price is not None else None
        unrealized = current_value - cost if current_value is not None else None

        account_currency_totals = account_totals[account.id]['currencies'][currency]
        account_currency_totals['currency'] = currency
        account_currency_totals['cost'] += cost
        currency_totals[currency]['currency'] = currency
        currency_totals[currency]['cost'] += cost
        if unrealized is not None:
            account_currency_totals['unrealized'] += unrealized
            currency_totals[currency]['unrealized'] += unrealized
        if current_value is not None:
            account_currency_totals['value'] += current_value
            currency_totals[currency]['value'] += current_value

        positions.append({
            'account': account,
            'instrument': instrument,
            'quantity': quantity,
            'average_cost': cost / quantity,
            'cost': money(cost),
            'current_value': money(current_value) if current_value is not None else None,
            'unrealized': money(unrealized) if unrealized is not None else None,
            'currency': currency,
        })

    today = timezone.localdate()
    dividends = []
    dividend_queryset = (
        BrokerageDividend.objects
        .filter(account__user=user, payment_date__gte=today)
        .select_related('account', 'instrument')
        .order_by('payment_date', 'instrument__ticker')
    )

    for dividend in dividend_queryset:
        quantity_date = dividend.ex_dividend_date or today
        quantity = get_quantity(dividend.account, dividend.instrument, quantity_date)
        gross_total = quantity * dividend.gross_amount_per_share
        tax_rate = ZERO if dividend.account.is_tax_exempt else dividend.tax_rate
        tax_amount = gross_total * (tax_rate / Decimal('100'))
        net_total = gross_total - tax_amount
        planned_dividend_net_by_currency[dividend.currency] += net_total

        dividends.append({
            'dividend': dividend,
            'quantity': quantity,
            'gross_total': money(gross_total),
            'tax_amount': money(tax_amount),
            'net_total': money(net_total),
            'tax_rate': tax_rate,
        })

    formatted_account_totals = []
    for totals in account_totals.values():
        formatted_account_totals.append({
            'account': totals['account'],
            'currencies': sorted(
                (
                    {
                        'currency': item['currency'],
                        'value': money(item['value']),
                        'cost': money(item['cost']),
                        'unrealized': money(item['unrealized']),
                    }
                    for item in totals['currencies'].values()
                ),
                key=lambda item: item['currency'],
            ),
        })

    formatted_currency_totals = []
    all_currencies = (
        set(currency_totals)
        | set(planned_dividend_net_by_currency)
        | set(realized_gain_by_currency)
        | set(realized_tax_by_currency)
    )
    for currency in all_currencies:
        totals = currency_totals[currency]
        totals['currency'] = currency
        formatted_currency_totals.append({
            'currency': currency,
            'value': money(totals['value']),
            'cost': money(totals['cost']),
            'unrealized': money(totals['unrealized']),
            'planned_dividend_net': money(planned_dividend_net_by_currency[currency]),
            'realized_gain': money(realized_gain_by_currency[currency]),
            'estimated_sell_tax': money(realized_tax_by_currency[currency]),
        })

    return {
        'accounts': accounts,
        'account_totals': formatted_account_totals,
        'currency_totals': sorted(formatted_currency_totals, key=lambda item: item['currency']),
        'positions': sorted(positions, key=lambda item: (item['account'].name, item['instrument'].ticker)),
        'upcoming_dividends': dividends,
    }
