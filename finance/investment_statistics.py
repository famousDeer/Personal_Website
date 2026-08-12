import math
import statistics
from collections import defaultdict
from decimal import Decimal

from .models import BrokerageCashOperation, BrokerageTransaction


ZERO = Decimal('0')
ONE_HUNDRED = Decimal('100')


def first_purchase_for_instrument(user, instrument):
    return (
        BrokerageTransaction.objects
        .filter(
            account__user=user,
            instrument=instrument,
            transaction_type=BrokerageTransaction.BUY,
        )
        .select_related('account')
        .order_by('trade_date', 'trade_time', 'id')
        .first()
    )


def calculate_market_statistics(
    points,
    *,
    first_purchase_price=None,
    first_purchase_date=None,
    current_price=None,
):
    """Calculate market-risk statistics from finalized adjusted daily closes."""
    ordered_points = sorted(
        (
            point for point in points
            if (point.get('adjusted_close') or point.get('close')) is not None
        ),
        key=lambda point: point['date'],
    )
    result = {
        'sessions': len(ordered_points),
        'first_date': ordered_points[0]['date'] if ordered_points else None,
        'last_date': ordered_points[-1]['date'] if ordered_points else None,
        'period_return_percent': None,
        'price_change_since_purchase_percent': None,
        'price_change_since_purchase': None,
        'cagr_percent': None,
        'annualized_volatility_percent': None,
        'average_daily_return_percent': None,
        'maximum_drawdown_percent': None,
        'maximum_drawdown_peak_date': None,
        'maximum_drawdown_trough_date': None,
        'best_day_percent': None,
        'best_day_date': None,
        'worst_day_percent': None,
        'worst_day_date': None,
        'positive_sessions_percent': None,
    }
    if not ordered_points:
        return result

    adjusted = [point.get('adjusted_close') or point['close'] for point in ordered_points]
    raw_last_close = current_price or ordered_points[-1].get('close')
    if first_purchase_price and raw_last_close is not None:
        price_change = raw_last_close - first_purchase_price
        result['price_change_since_purchase'] = price_change
        result['price_change_since_purchase_percent'] = (
            price_change / first_purchase_price
        ) * ONE_HUNDRED

    first_adjusted = adjusted[0]
    last_adjusted = adjusted[-1]
    if first_adjusted:
        result['period_return_percent'] = (
            (last_adjusted / first_adjusted) - Decimal('1')
        ) * ONE_HUNDRED

    first_date = ordered_points[0]['date']
    last_date = ordered_points[-1]['date']
    elapsed_days = (last_date - first_date).days
    if elapsed_days > 0 and first_adjusted > 0 and last_adjusted > 0:
        years = elapsed_days / 365.2425
        cagr = math.pow(float(last_adjusted / first_adjusted), 1 / years) - 1
        result['cagr_percent'] = Decimal(str(cagr * 100))

    returns = []
    for index in range(1, len(ordered_points)):
        previous = adjusted[index - 1]
        current = adjusted[index]
        if previous in (None, ZERO):
            continue
        returns.append({
            'date': ordered_points[index]['date'],
            'return': (current / previous) - Decimal('1'),
        })

    if returns:
        return_values = [item['return'] for item in returns]
        result['average_daily_return_percent'] = (
            sum(return_values, ZERO) / Decimal(len(return_values))
        ) * ONE_HUNDRED
        best = max(returns, key=lambda item: item['return'])
        worst = min(returns, key=lambda item: item['return'])
        result['best_day_percent'] = best['return'] * ONE_HUNDRED
        result['best_day_date'] = best['date']
        result['worst_day_percent'] = worst['return'] * ONE_HUNDRED
        result['worst_day_date'] = worst['date']
        positive_sessions = sum(1 for item in returns if item['return'] > 0)
        result['positive_sessions_percent'] = (
            Decimal(positive_sessions) / Decimal(len(returns))
        ) * ONE_HUNDRED
        if len(return_values) >= 2:
            volatility = statistics.stdev(float(value) for value in return_values) * math.sqrt(252)
            result['annualized_volatility_percent'] = Decimal(str(volatility * 100))

    running_peak = adjusted[0]
    running_peak_date = ordered_points[0]['date']
    worst_drawdown = ZERO
    worst_peak_date = running_peak_date
    worst_trough_date = running_peak_date
    for point, value in zip(ordered_points, adjusted):
        if value > running_peak:
            running_peak = value
            running_peak_date = point['date']
        if running_peak <= 0:
            continue
        drawdown = (value / running_peak) - Decimal('1')
        if drawdown < worst_drawdown:
            worst_drawdown = drawdown
            worst_peak_date = running_peak_date
            worst_trough_date = point['date']
    result['maximum_drawdown_percent'] = worst_drawdown * ONE_HUNDRED
    result['maximum_drawdown_peak_date'] = worst_peak_date
    result['maximum_drawdown_trough_date'] = worst_trough_date
    return result


def actual_profit_by_currency(user, instrument, portfolio_summary):
    positions_by_account = {
        position['account'].id: position
        for position in portfolio_summary.get('positions', [])
        if position['instrument'].id == instrument.id
    }
    account_ids = set(positions_by_account)
    account_ids.update(
        instrument.cash_operations
        .filter(account__user=user)
        .values_list('account_id', flat=True)
    )
    account_ids.update(
        instrument.transactions
        .filter(account__user=user)
        .values_list('account_id', flat=True)
    )

    totals = defaultdict(lambda: {
        'currency': '',
        'current_value': ZERO,
        'purchase_outflows': ZERO,
        'sale_inflows': ZERO,
        'trade_profit': ZERO,
        'net_income': ZERO,
        'total_profit': ZERO,
        'cost_basis': ZERO,
        'unrealized_profit': ZERO,
        'has_ledger': False,
        'has_transaction_fallback': False,
        'valuation_complete': True,
        'accounts': [],
    })
    relevant_income_types = {
        BrokerageCashOperation.DIVIDEND,
        BrokerageCashOperation.WITHHOLDING_TAX,
        BrokerageCashOperation.FEE,
    }
    relevant_trade_types = {
        BrokerageCashOperation.BUY,
        BrokerageCashOperation.SELL,
    }

    for account_id in sorted(account_ids):
        position = positions_by_account.get(account_id)
        operations = list(
            instrument.cash_operations
            .filter(account_id=account_id, account__user=user)
            .select_related('account')
        )
        account_transactions = list(
            instrument.transactions
            .filter(account_id=account_id, account__user=user)
            .select_related('account')
            .order_by('trade_date', 'id')
        )
        account = operations[0].account if operations else None
        if account is None and position is not None:
            account = position['account']
        if account is None:
            transaction = account_transactions[0] if account_transactions else None
            account = transaction.account if transaction else None
        if account is None:
            continue

        currency = account.currency
        bucket = totals[currency]
        bucket['currency'] = currency
        if account.name not in bucket['accounts']:
            bucket['accounts'].append(account.name)

        current_value = ZERO
        cost_basis = ZERO
        unrealized_profit = ZERO
        if position is not None:
            if position.get('current_value') is None:
                bucket['valuation_complete'] = False
            else:
                current_value = position['current_value']
            cost_basis = position.get('cost') or ZERO
            if position.get('profit') is None:
                bucket['valuation_complete'] = False
            else:
                unrealized_profit = position['profit']
        bucket['current_value'] += current_value
        bucket['cost_basis'] += cost_basis
        bucket['unrealized_profit'] += unrealized_profit

        trade_operations = [
            operation for operation in operations
            if operation.operation_type in relevant_trade_types
        ]
        if trade_operations:
            bucket['has_ledger'] = True
            trade_cash = sum((operation.amount for operation in trade_operations), ZERO)
            purchases = -sum(
                (operation.amount for operation in trade_operations if operation.operation_type == BrokerageCashOperation.BUY and operation.amount < 0),
                ZERO,
            )
            sales = sum(
                (operation.amount for operation in trade_operations if operation.operation_type == BrokerageCashOperation.SELL and operation.amount > 0),
                ZERO,
            )
            net_income = sum(
                (operation.amount for operation in operations if operation.operation_type in relevant_income_types),
                ZERO,
            )
            trade_profit = current_value + trade_cash
            total_profit = trade_profit + net_income
            bucket['purchase_outflows'] += purchases
            bucket['sale_inflows'] += sales
            bucket['trade_profit'] += trade_profit
            bucket['net_income'] += net_income
            bucket['total_profit'] += total_profit
        elif account_transactions:
            bucket['has_transaction_fallback'] = True
            purchases = sum(
                (
                    transaction.quantity * transaction.price + transaction.fees
                    for transaction in account_transactions
                    if transaction.transaction_type == BrokerageTransaction.BUY
                ),
                ZERO,
            )
            sales = sum(
                (
                    transaction.quantity * transaction.price - transaction.fees
                    for transaction in account_transactions
                    if transaction.transaction_type == BrokerageTransaction.SELL
                ),
                ZERO,
            )
            trade_profit = current_value + sales - purchases
            bucket['purchase_outflows'] += purchases
            bucket['sale_inflows'] += sales
            bucket['trade_profit'] += trade_profit
            bucket['total_profit'] += trade_profit
        elif position is not None:
            bucket['purchase_outflows'] += cost_basis
            bucket['trade_profit'] += unrealized_profit
            bucket['total_profit'] += unrealized_profit

    result = []
    for currency, bucket in sorted(totals.items()):
        denominator = bucket['purchase_outflows']
        if bucket['valuation_complete']:
            bucket['return_percent'] = (
                (bucket['total_profit'] / denominator) * ONE_HUNDRED
                if denominator else None
            )
        else:
            bucket['current_value'] = None
            bucket['unrealized_profit'] = None
            bucket['trade_profit'] = None
            bucket['total_profit'] = None
            bucket['return_percent'] = None

        if bucket['has_ledger'] and bucket['has_transaction_fallback']:
            bucket['source'] = 'XTB cash ledger + transakcje'
        elif bucket['has_ledger']:
            bucket['source'] = 'XTB cash ledger'
        elif bucket['has_transaction_fallback']:
            bucket['source'] = 'Transakcje i bieżąca wycena'
        else:
            bucket['source'] = 'Szacunek z pozycji'
        result.append(bucket)
    return result
