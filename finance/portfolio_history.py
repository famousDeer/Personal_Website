from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .brokerage import ZERO, build_portfolio_summary, money
from .market_data import MarketDataError, fetch_latest_fx_rates_to_pln
from .models import (
    BrokerageAccount,
    BrokerageCashOperation,
    BrokerageDailyPrice,
    BrokeragePositionSnapshot,
    BrokerageTransaction,
)


LOCAL_PRICE_SOURCE = 'Dane lokalne'
SNAPSHOT_PRICE_SOURCE = 'Snapshoty XTB'
LOCAL_FX_SOURCE = 'Kursy zapisane przy transakcjach'


def _date_range(start_date, end_date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def _local_date(value):
    if timezone.is_aware(value):
        return timezone.localtime(value).date()
    return value.date()


def _apply_quantity(quantity, transaction):
    if transaction.transaction_type == BrokerageTransaction.BUY:
        return quantity + transaction.quantity
    return max(quantity - transaction.quantity, Decimal('0'))


def _convert_value(value, source_currency, target_currency, fx_rates):
    source_currency = (source_currency or '').upper()
    target_currency = (target_currency or '').upper()
    if source_currency == target_currency:
        return value
    source_rate = fx_rates[source_currency]
    target_rate = fx_rates[target_currency]
    return value * source_rate / target_rate


def _transaction_price(transaction):
    return transaction.market_price if transaction.market_price is not None else transaction.price


def _last_price_date(instrument):
    if instrument.last_price_at:
        return _local_date(instrument.last_price_at)
    return None


def _summary(points):
    complete_points = [
        point for point in points
        if point.get('total_value') is not None
    ]
    if not complete_points:
        return {
            'latest_value': None,
            'latest_total_value': None,
            'latest_securities_value': None,
            'latest_cash_value': None,
            'change': None,
            'total_change': None,
            'change_percent': None,
            'total_change_percent': None,
        }

    first_value = complete_points[0]['total_value']
    latest = complete_points[-1]
    last_value = latest['total_value']
    change = last_value - first_value
    change_percent = None
    if first_value:
        change_percent = (change / first_value) * Decimal('100')

    serialized_change_percent = (
        str(change_percent.quantize(Decimal('0.01')))
        if change_percent is not None else None
    )
    return {
        # Keep the original keys for existing clients. ``value`` now means the
        # whole account value instead of securities only.
        'latest_value': str(money(last_value)),
        'latest_total_value': str(money(last_value)),
        'latest_securities_value': str(money(latest['securities_value'])),
        'latest_cash_value': str(money(latest['cash_value'])),
        'change': str(money(change)),
        'total_change': str(money(change)),
        'change_percent': serialized_change_percent,
        'total_change_percent': serialized_change_percent,
    }


def _load_price_histories(instruments, transactions, snapshots, start_date, end_date):
    instruments = list(instruments)
    prices_by_instrument = {}
    initial_prices = {}
    transaction_prices = defaultdict(list)
    snapshot_prices = defaultdict(list)
    missing_prices = []
    warnings = []

    for transaction in transactions:
        price = _transaction_price(transaction)
        if price is not None and transaction.trade_date <= end_date:
            transaction_prices[transaction.instrument_id].append(
                (transaction.trade_date, transaction.id, price)
            )

    for snapshot in snapshots:
        if snapshot.current_price is not None:
            snapshot_prices[snapshot.instrument_id].append(
                (_local_date(snapshot.as_of), 10**10 + snapshot.id, snapshot.current_price)
            )

    daily_prices = defaultdict(list)
    instrument_ids = [instrument.id for instrument in instruments]
    for point in (
        BrokerageDailyPrice.objects
        .filter(instrument_id__in=instrument_ids, trading_date__lte=end_date)
        .order_by('instrument_id', 'trading_date', 'id')
    ):
        daily_prices[point.instrument_id].append(
            (point.trading_date, 10**9 + point.id, point.close)
        )

    for instrument in instruments:
        known_points = list(transaction_prices.get(instrument.id, []))
        known_points.extend(daily_prices.get(instrument.id, []))
        known_points.extend(snapshot_prices.get(instrument.id, []))

        if instrument.last_price is not None:
            price_date = _last_price_date(instrument)
            if price_date is None:
                # A manually entered price without an observation timestamp is
                # current, not a historical seed.
                price_date = end_date
            if price_date <= end_date:
                known_points.append((price_date, 10**12, instrument.last_price))

        if not known_points:
            missing_prices.append(instrument.ticker)
            continue

        known_points.sort(key=lambda item: (item[0], item[1]))
        seed_price = None
        for point_date, _order, price in reversed(known_points):
            if point_date <= start_date:
                seed_price = price
                break

        if seed_price is not None:
            initial_prices[instrument.id] = seed_price

        price_points = {}
        for point_date, _order, price in known_points:
            if start_date <= point_date <= end_date:
                price_points[point_date] = price

        if price_points:
            prices_by_instrument[instrument.id] = price_points

    if missing_prices:
        listed_tickers = ', '.join(sorted(missing_prices)[:6])
        remaining = len(missing_prices) - 6
        suffix = f' i {remaining} kolejnych' if remaining > 0 else ''
        warnings.append(
            f'Brak lokalnej ceny dla: {listed_tickers}{suffix}. '
            'Pozycje bez ceny są oznaczone jako niepełne.'
        )

    sources = []
    if prices_by_instrument or initial_prices:
        sources.append(LOCAL_PRICE_SOURCE)
    if snapshots:
        sources.append(SNAPSHOT_PRICE_SOURCE)
    return prices_by_instrument, initial_prices, sources, warnings


def _load_local_fx_rates(transactions, currencies, target_currency):
    normalized_currencies = {
        (currency or '').upper()
        for currency in currencies
        if currency
    }
    target_currency = (target_currency or '').upper()

    if all(currency == target_currency for currency in normalized_currencies):
        return {
            'rates': {target_currency: Decimal('1'), 'PLN': Decimal('1')},
            'source': '',
            'table_date': '',
            'warnings': [],
        }

    rate_points = defaultdict(list)
    for transaction in transactions:
        currency = (transaction.instrument.currency or '').upper()
        # The transaction model historically defaulted foreign FX to 1.0.
        # That value is not a credible EUR/USD-to-PLN rate.
        if (
            transaction.fx_rate_to_pln
            and not (
                currency != 'PLN'
                and transaction.fx_rate_to_pln == Decimal('1.000000')
            )
        ):
            rate_points[currency].append(
                (transaction.trade_date, transaction.id, transaction.fx_rate_to_pln)
            )

    rates = {'PLN': Decimal('1')}
    missing = []
    for currency in normalized_currencies:
        if currency == 'PLN':
            continue
        points = sorted(rate_points.get(currency, []), key=lambda item: (item[0], item[1]))
        if points:
            rates[currency] = points[-1][2]
        else:
            missing.append(currency)

    warnings = []
    if missing:
        warnings.append(
            f'Brak kursu do PLN dla walut: {", ".join(sorted(missing))}. '
            'Dni zawierające te waluty oznaczono jako niepełne.'
        )

    return {
        'rates': rates,
        'source': LOCAL_FX_SOURCE if any(currency != 'PLN' for currency in rates) else '',
        'table_date': '',
        'warnings': warnings,
    }


def _load_fx_rates(transactions, currencies, target_currency):
    normalized_currencies = {
        (currency or '').strip().upper()
        for currency in currencies
        if (currency or '').strip()
    }
    target_currency = (target_currency or '').strip().upper()
    normalized_currencies.add(target_currency)

    if all(currency == target_currency for currency in normalized_currencies):
        return {
            'rates': {target_currency: Decimal('1'), 'PLN': Decimal('1')},
            'source': '',
            'table_date': '',
            'mode': 'native',
            'warnings': [],
        }

    try:
        result = fetch_latest_fx_rates_to_pln(normalized_currencies)
        return {
            **result,
            'mode': 'latest_nbp',
            'warnings': [],
        }
    except MarketDataError as exc:
        fallback = _load_local_fx_rates(transactions, normalized_currencies, target_currency)
        fallback['mode'] = 'transaction_fallback'
        fallback['warnings'] = [
            f'Nie udało się pobrać najnowszych kursów NBP ({exc}). '
            'Użyto dostępnych kursów zapisanych przy transakcjach.'
        ] + fallback['warnings']
        return fallback


def _snapshot_batches_by_date(snapshots):
    batches = defaultdict(list)
    for snapshot in snapshots:
        batch_date = _local_date(snapshot.as_of)
        batches[(batch_date, snapshot.account_id, snapshot.as_of)].append(snapshot)

    latest_batches = defaultdict(dict)
    for (batch_date, account_id, as_of), rows in batches.items():
        existing = latest_batches[batch_date].get(account_id)
        if existing is None or as_of > existing[0].as_of:
            latest_batches[batch_date][account_id] = rows
    return latest_batches


def _apply_transaction_state(quantities, anchors, transaction):
    key = (transaction.account_id, transaction.instrument_id)
    new_quantity = _apply_quantity(quantities[key], transaction)
    if new_quantity <= 0:
        quantities.pop(key, None)
        anchors.pop(key, None)
    else:
        quantities[key] = new_quantity


def _apply_snapshot_batch(quantities, anchors, account_id, snapshots):
    for key in [key for key in quantities if key[0] == account_id]:
        quantities.pop(key, None)
        anchors.pop(key, None)

    for snapshot in snapshots:
        if snapshot.quantity <= 0:
            continue
        key = (snapshot.account_id, snapshot.instrument_id)
        quantities[key] = snapshot.quantity
        implied_conversion = None
        if snapshot.current_price:
            implied_conversion = (
                snapshot.market_value / (snapshot.quantity * snapshot.current_price)
            )
        anchors[key] = {
            'date': _local_date(snapshot.as_of),
            'quantity': snapshot.quantity,
            'market_value': snapshot.market_value,
            'implied_conversion': implied_conversion,
            'currency': snapshot.currency,
        }


def _convert_component(value, source_currency, target_currency, fx_rates, missing_fx):
    source_currency = (source_currency or '').upper()
    target_currency = (target_currency or '').upper()
    if source_currency != target_currency and (
        source_currency not in fx_rates or target_currency not in fx_rates
    ):
        missing_fx.add(source_currency or '?')
        return None
    return _convert_value(value, source_currency, target_currency, fx_rates)


def _current_summary_values(user, selected_account, target_currency, fx_rates, missing_fx):
    summary = build_portfolio_summary(user, selected_account=selected_account)
    securities_value = ZERO
    cash_value = ZERO
    for item in summary['currency_totals']:
        converted_securities = _convert_component(
            item['securities_value'], item['currency'], target_currency, fx_rates, missing_fx,
        )
        converted_cash = _convert_component(
            item['cash_balance'], item['currency'], target_currency, fx_rates, missing_fx,
        )
        if converted_securities is None or converted_cash is None:
            return None
        securities_value += converted_securities
        cash_value += converted_cash
    return {
        'securities_value': money(securities_value),
        'cash_value': money(cash_value),
        'total_value': money(securities_value + cash_value),
    }


def build_portfolio_value_history(user, start_date, end_date, selected_account=None):
    account_queryset = BrokerageAccount.objects.filter(user=user)
    if selected_account is not None:
        account_queryset = account_queryset.filter(id=selected_account.id)
    accounts = list(account_queryset)
    account_ids = {account.id for account in accounts}
    target_currency = selected_account.currency if selected_account is not None else 'PLN'

    transaction_queryset = (
        BrokerageTransaction.objects
        .filter(account_id__in=account_ids, trade_date__lte=end_date)
        .select_related('account', 'instrument')
        .order_by('trade_date', 'id')
    )
    transactions = list(transaction_queryset)

    snapshots = [
        snapshot
        for snapshot in (
            BrokeragePositionSnapshot.objects
            .filter(account_id__in=account_ids)
            .select_related('account', 'instrument')
            .order_by('as_of', 'id')
        )
        if _local_date(snapshot.as_of) <= end_date
    ]
    cash_operations = [
        operation
        for operation in (
            BrokerageCashOperation.objects
            .filter(account_id__in=account_ids)
            .select_related('account')
            .order_by('occurred_at', 'id')
        )
        if _local_date(operation.occurred_at) <= end_date
    ]

    instruments = {
        transaction.instrument_id: transaction.instrument
        for transaction in transactions
    }
    instruments.update({
        snapshot.instrument_id: snapshot.instrument
        for snapshot in snapshots
    })

    currencies = {target_currency}
    currencies.update(account.currency for account in accounts)
    currencies.update(instrument.currency for instrument in instruments.values())
    currencies.update(snapshot.currency for snapshot in snapshots)
    currencies.update(operation.currency for operation in cash_operations)

    fx_data = _load_fx_rates(transactions, currencies, target_currency)
    fx_rates = fx_data['rates']
    warnings = list(fx_data.get('warnings') or [])

    price_histories, initial_prices, price_sources, price_warnings = _load_price_histories(
        instruments.values(), transactions, snapshots, start_date, end_date,
    )
    warnings.extend(price_warnings)

    quantities = defaultdict(lambda: Decimal('0'))
    anchors = {}
    transactions_by_date = defaultdict(list)
    for transaction in transactions:
        transactions_by_date[transaction.trade_date].append(transaction)

    snapshots_by_date = _snapshot_batches_by_date(snapshots)
    cash_by_date = defaultdict(list)
    for operation in cash_operations:
        cash_by_date[_local_date(operation.occurred_at)].append(operation)

    # Reconstruct the opening state in chronological order. A broker snapshot
    # replaces the whole account state for its timestamp; later transactions
    # are applied on top of that state.
    opening_event_dates = sorted(
        {
            transaction.trade_date
            for transaction in transactions
            if transaction.trade_date < start_date
        }
        | {
            day for day in snapshots_by_date if day < start_date
        }
    )
    activity_seen = False
    for event_date in opening_event_dates:
        for transaction in transactions_by_date.get(event_date, []):
            _apply_transaction_state(quantities, anchors, transaction)
            activity_seen = True
        for account_id, batch in snapshots_by_date.get(event_date, {}).items():
            _apply_snapshot_batch(quantities, anchors, account_id, batch)
            activity_seen = True

    cash_balances = defaultdict(lambda: Decimal('0'))
    for operation in cash_operations:
        if _local_date(operation.occurred_at) < start_date:
            cash_balances[(operation.account_id, operation.currency)] += operation.amount
            activity_seen = True

    last_prices = dict(initial_prices)
    points = []
    missing_fx = set()
    missing_prices = set()
    latest_position_count = 0
    latest_priced_position_count = 0

    for current_date in _date_range(start_date, end_date):
        for instrument_id, price_points in price_histories.items():
            if current_date in price_points:
                last_prices[instrument_id] = price_points[current_date]

        for transaction in transactions_by_date.get(current_date, []):
            _apply_transaction_state(quantities, anchors, transaction)
            activity_seen = True

        for account_id, batch in snapshots_by_date.get(current_date, {}).items():
            _apply_snapshot_batch(quantities, anchors, account_id, batch)
            activity_seen = True

        for operation in cash_by_date.get(current_date, []):
            cash_balances[(operation.account_id, operation.currency)] += operation.amount
            activity_seen = True

        securities_value = ZERO
        securities_complete = True
        position_count = 0
        priced_position_count = 0
        for (account_id, instrument_id), quantity in quantities.items():
            if quantity <= 0:
                continue
            position_count += 1
            instrument = instruments.get(instrument_id)
            if instrument is None:
                securities_complete = False
                continue

            anchor = anchors.get((account_id, instrument_id))
            if anchor is not None and current_date == anchor['date']:
                value = anchor['market_value'] * (quantity / anchor['quantity'])
                value_currency = anchor['currency']
            elif anchor is not None and anchor['implied_conversion'] is not None:
                price = last_prices.get(instrument_id)
                if price is None:
                    missing_prices.add(instrument.ticker)
                    securities_complete = False
                    continue
                value = quantity * price * anchor['implied_conversion']
                value_currency = anchor['currency']
            elif anchor is not None:
                # The broker still supplied an exact market value, even when
                # the per-share price was absent. Carry that value until a
                # newer snapshot is imported instead of dropping the position.
                value = anchor['market_value'] * (quantity / anchor['quantity'])
                value_currency = anchor['currency']
            else:
                price = last_prices.get(instrument_id)
                if price is None:
                    missing_prices.add(instrument.ticker)
                    securities_complete = False
                    continue
                value = quantity * price
                value_currency = instrument.currency

            converted = _convert_component(
                value, value_currency, target_currency, fx_rates, missing_fx,
            )
            if converted is None:
                securities_complete = False
                continue
            securities_value += converted
            priced_position_count += 1

        cash_value = ZERO
        cash_complete = True
        for (_account_id, currency), balance in cash_balances.items():
            if not balance:
                continue
            converted = _convert_component(
                balance, currency, target_currency, fx_rates, missing_fx,
            )
            if converted is None:
                cash_complete = False
                continue
            cash_value += converted

        latest_position_count = position_count
        latest_priced_position_count = priced_position_count
        if not activity_seen and not quantities and not any(cash_balances.values()):
            continue

        is_complete = securities_complete and cash_complete
        points.append({
            'date': current_date.isoformat(),
            'securities_value': money(securities_value) if securities_complete else None,
            'cash_value': money(cash_value) if cash_complete else None,
            'total_value': (
                money(securities_value + cash_value)
                if is_complete else None
            ),
            'is_complete': is_complete,
        })

    # The state card is authoritative for today. Reuse that exact computation
    # for the final chart point and only add the NBP conversion layer. This is
    # the invariant that prevents the chart and "Stan portfela" from drifting.
    if end_date == timezone.localdate():
        current_values = _current_summary_values(
            user, selected_account, target_currency, fx_rates, missing_fx,
        )
        if current_values is not None:
            current_point = {
                'date': end_date.isoformat(),
                **current_values,
                'is_complete': True,
            }
            if points and points[-1]['date'] == end_date.isoformat():
                points[-1] = current_point
            else:
                points.append(current_point)

    if missing_fx:
        warnings.append(
            f'Brak wiarygodnego kursu dla: {", ".join(sorted(missing_fx))}. '
            'Niepełne dni nie są prezentowane jako pełna wartość portfela.'
        )
    if missing_prices:
        warnings.append(
            f'Brak ceny dla pozycji: {", ".join(sorted(missing_prices)[:8])}. '
            'Niepełne dni mają przerwę na wykresie.'
        )

    serialized_points = []
    for point in points:
        serialized_points.append({
            'date': point['date'],
            # ``value`` remains as a compatibility alias for total_value.
            'value': str(point['total_value']) if point['total_value'] is not None else None,
            'securities_value': (
                str(point['securities_value'])
                if point['securities_value'] is not None else None
            ),
            'cash_value': str(point['cash_value']) if point['cash_value'] is not None else None,
            'total_value': str(point['total_value']) if point['total_value'] is not None else None,
            'is_complete': point['is_complete'],
        })

    complete_days = sum(1 for point in points if point['is_complete'])
    return {
        'points': serialized_points,
        'currency': target_currency,
        'fx_source': fx_data.get('source') or '',
        'fx_table_date': fx_data.get('table_date') or '',
        'fx_mode': fx_data.get('mode') or '',
        'price_sources': price_sources,
        'warnings': list(dict.fromkeys(warnings)),
        'coverage': {
            'is_complete': not missing_fx and not missing_prices,
            'complete_days': complete_days,
            'days': len(points),
            'positions': latest_position_count,
            'priced_positions': latest_priced_position_count,
            'missing_fx': sorted(missing_fx),
            'missing_prices': sorted(missing_prices),
        },
        'summary': _summary(points),
    }
