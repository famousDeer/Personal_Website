from collections import defaultdict
from decimal import Decimal

from django.utils import timezone

from .models import (
    BrokerageAccount,
    BrokerageCashOperation,
    BrokerageDividend,
    BrokeragePositionSnapshot,
    BrokeragePriceSnapshot,
    BrokerageTransaction,
)


BELKA_TAX_RATE = Decimal('19.00')
ZERO = Decimal('0.00')
ONE_HUNDRED = Decimal('100')


def money(value):
    return value.quantize(Decimal('0.01'))


def _empty_totals(currency=''):
    return {
        'currency': currency,
        # ``value`` is the legacy name for the value of securities.  Keep it
        # separate from cash so existing callers do not silently change their
        # meaning when cash operations become available.
        'value': ZERO,
        'securities_value': ZERO,
        'cash_balance': ZERO,
        'total_value': ZERO,
        'cost': ZERO,
        'unrealized': ZERO,
        'net_contributions': ZERO,
        'net_income': ZERO,
        'day_change': ZERO,
        'as_of': None,
        'activity_as_of': None,
    }


def _latest_datetime(current, candidate):
    if candidate is None:
        return current
    if current is None or candidate > current:
        return candidate
    return current


def _percentage(amount, base):
    if amount is None or not base:
        return None
    return (amount / base) * ONE_HUNDRED


def _calendar_date(value):
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.date()


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


def _latest_position_snapshots(accounts):
    """Return one complete, latest snapshot batch for every snapshotted account."""
    account_ids = [account.id for account in accounts]
    if not account_ids:
        return {}

    snapshots_by_account = defaultdict(list)
    latest_as_of_by_account = {}
    snapshots = (
        BrokeragePositionSnapshot.objects
        .filter(account_id__in=account_ids)
        .select_related('account', 'instrument')
        .order_by('account_id', '-as_of', 'instrument__ticker', 'id')
    )
    for snapshot in snapshots:
        latest_as_of = latest_as_of_by_account.setdefault(snapshot.account_id, snapshot.as_of)
        if snapshot.as_of == latest_as_of:
            snapshots_by_account[snapshot.account_id].append(snapshot)
    return dict(snapshots_by_account)


def _daily_mover_data(positions):
    """Build position-level daily moves from distinct market dates.

    There can be several refreshes during one day.  The comparison point is
    therefore the newest observation from an *earlier calendar date*, rather
    than merely the second newest database row.
    """
    instrument_ids = {position['instrument'].id for position in positions}
    if not instrument_ids:
        return [], [], [], None

    price_points = (
        BrokeragePriceSnapshot.objects
        .filter(instrument_id__in=instrument_ids)
        .select_related('instrument')
        .order_by('instrument_id', '-observed_at', '-id')
    )
    latest_by_instrument = {}
    previous_by_instrument = {}
    latest_date_by_instrument = {}
    for point in price_points:
        instrument_id = point.instrument_id
        if instrument_id not in latest_by_instrument:
            latest_by_instrument[instrument_id] = point
            latest_date_by_instrument[instrument_id] = _calendar_date(point.observed_at)
            continue
        if (
            instrument_id not in previous_by_instrument
            and _calendar_date(point.observed_at) < latest_date_by_instrument[instrument_id]
        ):
            previous_by_instrument[instrument_id] = point

    market_as_of = None
    for point in latest_by_instrument.values():
        market_as_of = _latest_datetime(market_as_of, point.observed_at)

    movers = []
    for position in positions:
        instrument_id = position['instrument'].id
        latest = latest_by_instrument.get(instrument_id)
        previous = previous_by_instrument.get(instrument_id)
        if latest is None or previous is None:
            continue

        change = latest.price - previous.price
        change_percent = _percentage(change, previous.price)
        # XTB reports the position value in the account currency, while the
        # quoted price can be in another currency (for example a German ETF
        # held on a PLN IKE).  Reuse the conversion implied by the imported
        # position snapshot so the monetary impact is not mislabeled as PLN.
        conversion_rate = Decimal('1')
        reference_price = position.get('current_price')
        reference_value = position.get('current_value')
        if reference_price and reference_value is not None and position['quantity']:
            conversion_rate = reference_value / (position['quantity'] * reference_price)
        impact = position['quantity'] * change * conversion_rate
        movers.append({
            'instrument': position['instrument'],
            'account': position['account'],
            'quantity': position['quantity'],
            'latest_price': latest.price,
            'previous_price': previous.price,
            'change': money(change),
            # Aliases make the payload explicit for both templates and JSON
            # consumers while ``change`` remains the primary display value.
            'amount': money(change),
            'change_amount': money(change),
            'change_percent': money(change_percent) if change_percent is not None else None,
            'percent': money(change_percent) if change_percent is not None else None,
            'impact': money(impact),
            'currency': position['currency'],
            'price_currency': position['instrument'].currency,
            'observed_at': latest.observed_at,
            'previous_observed_at': previous.observed_at,
            'source': latest.source,
        })

    movers.sort(
        key=lambda item: (
            -abs(item['impact']),
            item['instrument'].ticker,
            item['account'].name,
        )
    )
    gainers = sorted(
        (item for item in movers if item['change_percent'] is not None and item['change_percent'] > 0),
        key=lambda item: (-item['change_percent'], -item['impact']),
    )
    losers = sorted(
        (item for item in movers if item['change_percent'] is not None and item['change_percent'] < 0),
        key=lambda item: (item['change_percent'], item['impact']),
    )
    return movers, gainers, losers, market_as_of


def build_portfolio_summary(user, selected_account=None):
    accounts_queryset = BrokerageAccount.objects.filter(user=user)
    if selected_account is not None:
        accounts_queryset = accounts_queryset.filter(id=selected_account.id)
    accounts = list(accounts_queryset)
    account_ids = [account.id for account in accounts]

    transactions = (
        BrokerageTransaction.objects
        .filter(account_id__in=account_ids)
        .select_related('account', 'instrument')
        .order_by('trade_date', 'id')
    )

    lots = defaultdict(list)
    realized_tax_by_currency = defaultdict(lambda: ZERO)
    realized_gain_by_currency = defaultdict(lambda: ZERO)

    for brokerage_transaction in transactions:
        key = (brokerage_transaction.account_id, brokerage_transaction.instrument_id)

        if brokerage_transaction.transaction_type == BrokerageTransaction.BUY:
            total_cost = brokerage_transaction.gross_value + brokerage_transaction.fees
            lots[key].append({
                'quantity': brokerage_transaction.quantity,
                'unit_cost': total_cost / brokerage_transaction.quantity,
                'transaction': brokerage_transaction,
            })
            continue

        quantity_to_sell = brokerage_transaction.quantity
        cost_basis = ZERO
        while quantity_to_sell > 0 and lots[key]:
            lot = lots[key][0]
            consumed_quantity = min(quantity_to_sell, lot['quantity'])
            cost_basis += consumed_quantity * lot['unit_cost']
            lot['quantity'] -= consumed_quantity
            quantity_to_sell -= consumed_quantity
            if lot['quantity'] <= 0:
                lots[key].pop(0)

        proceeds = (brokerage_transaction.quantity * brokerage_transaction.price) - brokerage_transaction.fees
        gain = proceeds - cost_basis
        realized_gain_by_currency[brokerage_transaction.instrument.currency] += gain
        if not brokerage_transaction.account.is_tax_exempt and gain > 0:
            realized_tax_by_currency[brokerage_transaction.instrument.currency] += (
                gain * (BELKA_TAX_RATE / ONE_HUNDRED)
            )

    positions = []
    account_totals = {
        account.id: {
            'account': account,
            'currencies': defaultdict(_empty_totals),
        }
        for account in accounts
    }
    currency_totals = defaultdict(_empty_totals)
    planned_dividend_net_by_currency = defaultdict(lambda: ZERO)

    def register_position(*, account, instrument, quantity, cost, current_value, unrealized,
                          currency, source, as_of, profit_percent, current_price):
        account_currency_totals = account_totals[account.id]['currencies'][currency]
        account_currency_totals['currency'] = currency
        account_currency_totals['cost'] += cost
        account_currency_totals['securities_value'] += current_value
        account_currency_totals['value'] += current_value
        account_currency_totals['unrealized'] += unrealized
        account_currency_totals['as_of'] = _latest_datetime(account_currency_totals['as_of'], as_of)

        portfolio_currency_totals = currency_totals[currency]
        portfolio_currency_totals['currency'] = currency
        portfolio_currency_totals['cost'] += cost
        portfolio_currency_totals['securities_value'] += current_value
        portfolio_currency_totals['value'] += current_value
        portfolio_currency_totals['unrealized'] += unrealized
        portfolio_currency_totals['as_of'] = _latest_datetime(portfolio_currency_totals['as_of'], as_of)

        positions.append({
            'account': account,
            'instrument': instrument,
            'quantity': quantity,
            'average_cost': cost / quantity if quantity else ZERO,
            'cost': money(cost),
            'current_price': current_price,
            'current_value': money(current_value),
            'unrealized': money(unrealized),
            'profit': money(unrealized),
            'profit_percent': money(profit_percent) if profit_percent is not None else None,
            'currency': currency,
            'source': source,
            'as_of': as_of,
        })

    snapshots_by_account = _latest_position_snapshots(accounts)
    snapshotted_account_ids = set(snapshots_by_account)

    # Imported broker snapshots are authoritative for their account.  Every
    # row must come from the exact newest ``as_of`` batch; mixing rows from two
    # imports could otherwise resurrect positions that have since been sold.
    for account_id, snapshots in snapshots_by_account.items():
        for snapshot in snapshots:
            quantity = snapshot.quantity
            if quantity <= 0:
                continue

            snapshot_value = snapshot.market_value
            current_value = snapshot_value
            profit = snapshot.profit
            cost = None
            if profit is not None:
                cost = snapshot_value - profit
            elif snapshot.profit_percent is not None:
                multiplier = Decimal('1') + (snapshot.profit_percent / ONE_HUNDRED)
                if multiplier:
                    cost = snapshot_value / multiplier
                    profit = snapshot_value - cost

            if cost is None:
                # Some exports omit P/L.  A matching FIFO basis is preferable
                # to presenting the entire market value as profit.
                open_lots = lots.get((snapshot.account_id, snapshot.instrument_id), [])
                if open_lots:
                    cost = sum((lot['quantity'] * lot['unit_cost'] for lot in open_lots), ZERO)
                    profit = snapshot_value - cost
                else:
                    cost = snapshot_value
                    profit = ZERO

            current_price = snapshot.current_price
            if current_price is None and quantity:
                current_price = snapshot_value / quantity
            valuation_source = snapshot.source or 'snapshot'
            valuation_as_of = snapshot.as_of

            # A later market refresh should advance imported positions without
            # discarding the exact broker snapshot.  For foreign listings use
            # the FX conversion implied by XTB's account value at import time.
            instrument = snapshot.instrument
            if (
                instrument.last_price is not None
                and instrument.last_price_at is not None
                and instrument.last_price_at > snapshot.as_of
                and snapshot.current_price
            ):
                implied_conversion = snapshot_value / (quantity * snapshot.current_price)
                current_price = instrument.last_price
                current_value = quantity * current_price * implied_conversion
                profit = current_value - cost
                valuation_as_of = instrument.last_price_at
                valuation_source = instrument.market_data_source or 'dane rynkowe'
                if implied_conversion != Decimal('1'):
                    valuation_source = f'{valuation_source} · kurs z XTB'

            profit_percent = _percentage(profit, cost)

            register_position(
                account=snapshot.account,
                instrument=instrument,
                quantity=quantity,
                cost=cost,
                current_value=current_value,
                unrealized=profit,
                currency=snapshot.currency,
                source=valuation_source,
                as_of=valuation_as_of,
                profit_percent=profit_percent,
                current_price=current_price,
            )

    # Accounts without a broker snapshot retain the original FIFO behaviour.
    for (account_id, _instrument_id), open_lots in lots.items():
        if account_id in snapshotted_account_ids:
            continue
        quantity = sum((lot['quantity'] for lot in open_lots), Decimal('0'))
        if quantity <= 0:
            continue

        first_transaction = open_lots[0]['transaction']
        position_account = first_transaction.account
        instrument = first_transaction.instrument
        currency = instrument.currency
        cost = sum((lot['quantity'] * lot['unit_cost'] for lot in open_lots), ZERO)
        current_value = quantity * instrument.last_price if instrument.last_price is not None else None
        unrealized = current_value - cost if current_value is not None else None

        # Preserve the old behaviour for missing market prices: the position is
        # returned, but it does not add a fabricated value or profit to totals.
        totals_current_value = current_value if current_value is not None else ZERO
        totals_unrealized = unrealized if unrealized is not None else ZERO
        register_position(
            account=position_account,
            instrument=instrument,
            quantity=quantity,
            cost=cost,
            current_value=totals_current_value,
            unrealized=totals_unrealized,
            currency=currency,
            source=instrument.market_data_source or 'transactions',
            as_of=instrument.last_price_at,
            profit_percent=_percentage(unrealized, cost),
            current_price=instrument.last_price,
        )
        if current_value is None:
            positions[-1]['current_value'] = None
            positions[-1]['unrealized'] = None
            positions[-1]['profit'] = None

    cash_operations = (
        BrokerageCashOperation.objects
        .filter(account_id__in=account_ids)
        .select_related('account')
    )
    external_contribution_types = {
        BrokerageCashOperation.DEPOSIT,
        BrokerageCashOperation.WITHDRAWAL,
    }
    account_funding_types = external_contribution_types | {
        BrokerageCashOperation.INTERNAL_TRANSFER,
    }
    income_types = {
        BrokerageCashOperation.DIVIDEND,
        BrokerageCashOperation.WITHHOLDING_TAX,
        BrokerageCashOperation.INTEREST,
        BrokerageCashOperation.INTEREST_TAX,
    }
    for operation in cash_operations:
        account_currency_totals = account_totals[operation.account_id]['currencies'][operation.currency]
        account_currency_totals['currency'] = operation.currency
        account_currency_totals['cash_balance'] += operation.amount
        account_currency_totals['activity_as_of'] = _latest_datetime(
            account_currency_totals['activity_as_of'], operation.occurred_at,
        )

        portfolio_currency_totals = currency_totals[operation.currency]
        portfolio_currency_totals['currency'] = operation.currency
        portfolio_currency_totals['cash_balance'] += operation.amount
        portfolio_currency_totals['activity_as_of'] = _latest_datetime(
            portfolio_currency_totals['activity_as_of'], operation.occurred_at,
        )

        # A transfer between the user's brokerage accounts is real funding for
        # the receiving account (and an outflow for the sending account), but
        # it must not inflate capital contributed to the whole portfolio.
        if operation.operation_type in account_funding_types:
            account_currency_totals['net_contributions'] += operation.amount
        if (
            operation.operation_type in external_contribution_types
            or (
                selected_account is not None
                and operation.operation_type == BrokerageCashOperation.INTERNAL_TRANSFER
            )
        ):
            portfolio_currency_totals['net_contributions'] += operation.amount
        if operation.operation_type in income_types:
            account_currency_totals['net_income'] += operation.amount
            portfolio_currency_totals['net_income'] += operation.amount

    daily_movers, top_gainers, top_losers, market_as_of = _daily_mover_data(positions)
    movers_by_position = {
        (mover['account'].id, mover['instrument'].id): mover
        for mover in daily_movers
    }
    for position in positions:
        mover = movers_by_position.get((position['account'].id, position['instrument'].id))
        position['day_change'] = mover['change'] if mover else None
        position['day_change_percent'] = mover['change_percent'] if mover else None
        position['day_impact'] = mover['impact'] if mover else None
        position['day_observed_at'] = mover['observed_at'] if mover else None

    for mover in daily_movers:
        currency = mover['currency']
        impact = mover['impact']
        account_currency_totals = account_totals[mover['account'].id]['currencies'][currency]
        account_currency_totals['day_change'] += impact
        currency_totals[currency]['day_change'] += impact

    today = timezone.localdate()
    dividends = []
    dividend_queryset = (
        BrokerageDividend.objects
        .filter(account_id__in=account_ids, payment_date__gte=today)
        .select_related('account', 'instrument')
        .order_by('payment_date', 'instrument__ticker')
    )

    for dividend in dividend_queryset:
        quantity_date = dividend.ex_dividend_date or today
        quantity = get_quantity(dividend.account, dividend.instrument, quantity_date)
        gross_total = quantity * dividend.gross_amount_per_share
        tax_rate = ZERO if dividend.account.is_tax_exempt else dividend.tax_rate
        tax_amount = gross_total * (tax_rate / ONE_HUNDRED)
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

    def formatted_totals(item):
        total_value = item['securities_value'] + item['cash_balance']
        previous_value = total_value - item['day_change']
        return {
            'currency': item['currency'],
            'value': money(item['value']),
            'securities_value': money(item['securities_value']),
            'cash_balance': money(item['cash_balance']),
            'total_value': money(total_value),
            'cost': money(item['cost']),
            'unrealized': money(item['unrealized']),
            'profit_percent': (
                money(_percentage(item['unrealized'], item['cost']))
                if item['cost'] else None
            ),
            'net_contributions': money(item['net_contributions']),
            'net_income': money(item['net_income']),
            'day_change': money(item['day_change']),
            'day_change_percent': (
                money(_percentage(item['day_change'], previous_value))
                if previous_value else None
            ),
            'as_of': item['as_of'],
            'activity_as_of': item['activity_as_of'],
        }

    formatted_account_totals = []
    for totals in account_totals.values():
        account = totals['account']
        currencies = []
        for currency, item in totals['currencies'].items():
            item['currency'] = currency
            currencies.append(formatted_totals(item))
        currencies.sort(key=lambda item: item['currency'])

        native_item = totals['currencies'].get(account.currency)
        native = formatted_totals(native_item or _empty_totals(account.currency))
        formatted_account_totals.append({
            'account': account,
            'currency': account.currency,
            'currencies': currencies,
            'securities_value': native['securities_value'],
            'cash_balance': native['cash_balance'],
            'total_value': native['total_value'],
            'cost': native['cost'],
            'unrealized': native['unrealized'],
            'profit_percent': native['profit_percent'],
            'net_contributions': native['net_contributions'],
            'net_income': native['net_income'],
            'day_change': native['day_change'],
            'day_change_percent': native['day_change_percent'],
            'as_of': native['as_of'],
            'activity_as_of': native['activity_as_of'],
        })

    all_currencies = (
        set(currency_totals)
        | set(planned_dividend_net_by_currency)
        | set(realized_gain_by_currency)
        | set(realized_tax_by_currency)
    )
    formatted_currency_totals = []
    for currency in all_currencies:
        totals = currency_totals[currency]
        totals['currency'] = currency
        formatted = formatted_totals(totals)
        formatted.update({
            'planned_dividend_net': money(planned_dividend_net_by_currency[currency]),
            'realized_gain': money(realized_gain_by_currency[currency]),
            'estimated_sell_tax': money(realized_tax_by_currency[currency]),
        })
        formatted_currency_totals.append(formatted)
    formatted_currency_totals.sort(key=lambda item: item['currency'])

    securities_by_currency = {
        item['currency']: item['securities_value']
        for item in formatted_currency_totals
    }
    for position in positions:
        securities_value_for_currency = securities_by_currency.get(position['currency'], ZERO)
        if position['current_value'] is not None and securities_value_for_currency:
            position['allocation_percent'] = money(
                _percentage(position['current_value'], securities_value_for_currency)
            )
        else:
            position['allocation_percent'] = None

    positions_count = len(positions)
    priced_positions_count = sum(
        1 for position in positions if position['current_value'] is not None
    )
    profitable_positions_count = sum(
        1
        for position in positions
        if position['unrealized'] is not None and position['unrealized'] > 0
    )
    losing_positions_count = sum(
        1
        for position in positions
        if position['unrealized'] is not None and position['unrealized'] < 0
    )
    valuation_dates = [
        position['as_of']
        for position in positions
        if position['as_of'] is not None
    ]
    history_error_instrument_ids = {
        position['instrument'].id
        for position in positions
        if position['instrument'].history_sync_error
    }
    portfolio_health = {
        'positions_count': positions_count,
        'instrument_count': len({position['instrument'].id for position in positions}),
        'priced_positions_count': priced_positions_count,
        'missing_price_count': positions_count - priced_positions_count,
        'profitable_positions_count': profitable_positions_count,
        'losing_positions_count': losing_positions_count,
        'neutral_positions_count': (
            positions_count - profitable_positions_count - losing_positions_count
        ),
        'daily_coverage_count': len(daily_movers),
        'valuation_coverage_percent': (
            money((Decimal(priced_positions_count) / Decimal(positions_count)) * ONE_HUNDRED)
            if positions_count else None
        ),
        'daily_coverage_percent': (
            money((Decimal(len(daily_movers)) / Decimal(positions_count)) * ONE_HUNDRED)
            if positions_count else None
        ),
        'oldest_valuation_at': min(valuation_dates) if valuation_dates else None,
        'latest_valuation_at': max(valuation_dates) if valuation_dates else None,
        'history_error_count': len(history_error_instrument_ids),
    }

    # Top-level figures are primarily useful for a selected, single-currency
    # account.  Currency and account KPI collections remain the authoritative
    # representation when several currencies are selected.
    securities_value = sum((item['securities_value'] for item in formatted_currency_totals), ZERO)
    cash_balance = sum((item['cash_balance'] for item in formatted_currency_totals), ZERO)
    total_value = sum((item['total_value'] for item in formatted_currency_totals), ZERO)
    net_contributions = sum((item['net_contributions'] for item in formatted_currency_totals), ZERO)
    net_income = sum((item['net_income'] for item in formatted_currency_totals), ZERO)
    unrealized = sum((item['unrealized'] for item in formatted_currency_totals), ZERO)
    day_change = sum((item['day_change'] for item in formatted_currency_totals), ZERO)
    previous_total_value = total_value - day_change

    return {
        'accounts': accounts,
        'account_totals': formatted_account_totals,
        'currency_totals': formatted_currency_totals,
        'positions': sorted(positions, key=lambda item: (item['account'].name, item['instrument'].ticker)),
        'upcoming_dividends': dividends,
        'securities_value': money(securities_value),
        'cash_balance': money(cash_balance),
        'total_value': money(total_value),
        'net_contributions': money(net_contributions),
        'net_income': money(net_income),
        'unrealized': money(unrealized),
        'day_change': money(day_change),
        'day_change_percent': (
            money(_percentage(day_change, previous_total_value))
            if previous_total_value else None
        ),
        'daily_movers': daily_movers,
        'top_gainers': top_gainers,
        'top_losers': top_losers,
        'market_as_of': market_as_of,
        'portfolio_health': portfolio_health,
    }
