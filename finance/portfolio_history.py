from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from .brokerage import ZERO, money
from .models import BrokerageAccount, BrokerageTransaction

LOCAL_PRICE_SOURCE = 'Dane lokalne'
LOCAL_FX_SOURCE = 'Kursy zapisane przy transakcjach'


def _date_range(start_date, end_date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


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


def _last_price_date(instrument, end_date):
    if instrument.last_price_at:
        return min(instrument.last_price_at.date(), end_date)
    return end_date


def _summary(points):
    if not points:
        return {
            'latest_value': None,
            'change': None,
            'change_percent': None,
        }

    first_value = points[0]['value']
    last_value = points[-1]['value']
    change = last_value - first_value
    change_percent = None
    if first_value:
        change_percent = (change / first_value) * Decimal('100')

    return {
        'latest_value': str(money(last_value)),
        'change': str(money(change)),
        'change_percent': str(change_percent.quantize(Decimal('0.01'))) if change_percent is not None else None,
    }


def _load_price_histories(instruments, transactions, start_date, end_date):
    prices_by_instrument = {}
    initial_prices = {}
    transaction_prices = defaultdict(list)
    missing_prices = []
    warnings = []

    for transaction in transactions:
        price = _transaction_price(transaction)
        if price is not None and transaction.trade_date <= end_date:
            transaction_prices[transaction.instrument_id].append(
                (transaction.trade_date, transaction.id, price)
            )

    for instrument in instruments:
        known_points = list(transaction_prices.get(instrument.id, []))

        if instrument.last_price is not None:
            price_date = _last_price_date(instrument, end_date)
            known_points.append((price_date, 10**12, instrument.last_price))
            if price_date < end_date:
                known_points.append((end_date, 10**12 + 1, instrument.last_price))

        if not known_points:
            missing_prices.append(instrument.ticker)
            continue

        known_points.sort(key=lambda item: (item[0], item[1]))
        seed_price = None
        for point_date, _order, price in reversed(known_points):
            if point_date <= start_date:
                seed_price = price
                break
        if seed_price is None:
            seed_price = known_points[0][2]

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
            'Uzupełnij ostatnią cenę albo dodaj transakcję z ceną, żeby instrument pojawił się na wykresie.'
        )

    sources = [LOCAL_PRICE_SOURCE] if prices_by_instrument or initial_prices else []
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
        if transaction.fx_rate_to_pln:
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
            rates[currency] = Decimal('1')

    warnings = []
    if missing:
        warnings.append(
            f'Brak lokalnego kursu PLN dla walut: {", ".join(sorted(missing))}. '
            'Użyto kursu 1,00; sprawdź kursy zapisane przy transakcjach.'
        )

    return {
        'rates': rates,
        'source': LOCAL_FX_SOURCE,
        'table_date': '',
        'warnings': warnings,
    }


def build_portfolio_value_history(user, start_date, end_date, selected_account=None):
    account_queryset = BrokerageAccount.objects.filter(user=user)
    if selected_account is not None:
        account_queryset = account_queryset.filter(id=selected_account.id)
    account_ids = set(account_queryset.values_list('id', flat=True))

    transaction_queryset = (
        BrokerageTransaction.objects
        .filter(account__user=user, trade_date__lte=end_date)
        .select_related('account', 'instrument')
        .order_by('trade_date', 'id')
    )
    if selected_account is not None:
        transaction_queryset = transaction_queryset.filter(account=selected_account)

    transactions = [
        transaction
        for transaction in transaction_queryset
        if transaction.account_id in account_ids
    ]
    if not transactions:
        target_currency = selected_account.currency if selected_account is not None else 'PLN'
        return {
            'points': [],
            'currency': target_currency,
            'fx_source': '',
            'fx_table_date': '',
            'price_sources': [],
            'warnings': [],
            'summary': _summary([]),
        }

    instruments = {
        transaction.instrument_id: transaction.instrument
        for transaction in transactions
    }
    target_currency = selected_account.currency if selected_account is not None else 'PLN'
    currencies = {instrument.currency for instrument in instruments.values()}
    currencies.add(target_currency)

    fx_data = _load_local_fx_rates(transactions, currencies, target_currency)
    fx_rates = fx_data['rates']
    fx_source = fx_data.get('source') or ''
    fx_table_date = fx_data.get('table_date') or ''
    warnings = list(fx_data.get('warnings') or [])

    price_histories, initial_prices, price_sources, price_warnings = _load_price_histories(
        instruments.values(),
        transactions,
        start_date,
        end_date,
    )
    warnings.extend(price_warnings)

    quantities = defaultdict(lambda: Decimal('0'))
    transactions_by_date = defaultdict(list)
    for transaction in transactions:
        key = (transaction.account_id, transaction.instrument_id)
        if transaction.trade_date < start_date:
            quantities[key] = _apply_quantity(quantities[key], transaction)
        else:
            transactions_by_date[transaction.trade_date].append(transaction)

    last_prices = dict(initial_prices)
    points = []
    for current_date in _date_range(start_date, end_date):
        for instrument_id, price_points in price_histories.items():
            if current_date in price_points:
                last_prices[instrument_id] = price_points[current_date]

        for transaction in transactions_by_date.get(current_date, []):
            key = (transaction.account_id, transaction.instrument_id)
            quantities[key] = _apply_quantity(quantities[key], transaction)

        total = ZERO
        has_value = False
        for (account_id, instrument_id), quantity in quantities.items():
            if quantity <= 0:
                continue

            instrument = instruments.get(instrument_id)
            price = last_prices.get(instrument_id)
            if instrument is None or price is None:
                continue

            if instrument.currency != target_currency and (
                instrument.currency not in fx_rates or target_currency not in fx_rates
            ):
                continue

            value = quantity * price
            total += _convert_value(value, instrument.currency, target_currency, fx_rates)
            has_value = True

        if has_value or transactions_by_date.get(current_date):
            points.append({
                'date': current_date.isoformat(),
                'value': money(total),
            })

    return {
        'points': [
            {
                'date': point['date'],
                'value': str(point['value']),
            }
            for point in points
        ],
        'currency': target_currency,
        'fx_source': fx_source,
        'fx_table_date': fx_table_date,
        'price_sources': price_sources,
        'warnings': warnings,
        'summary': _summary(points),
    }
