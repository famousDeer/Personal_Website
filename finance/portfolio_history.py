from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from .brokerage import ZERO, money
from .market_data import MarketDataError, fetch_historical_market_prices, fetch_latest_fx_rates_to_pln
from .models import BrokerageAccount, BrokerageTransaction


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


def _load_price_histories(instruments, start_date, end_date):
    prices_by_instrument = {}
    initial_prices = {}
    sources = set()
    warnings = []

    for instrument in instruments:
        try:
            history = fetch_historical_market_prices(
                symbol=instrument.ticker,
                exchange=instrument.exchange,
                currency=instrument.currency,
                isin=instrument.isin,
                price_symbol=instrument.price_symbol,
                name=instrument.name,
                start_date=start_date,
                end_date=end_date,
            )
        except MarketDataError as exc:
            if instrument.last_price is not None:
                initial_prices[instrument.id] = instrument.last_price
                warnings.append(
                    f'{instrument.ticker}: brak historii cen, użyto ostatniej zapisanej ceny {instrument.last_price}.'
                )
            else:
                warnings.append(f'{instrument.ticker}: pominięto w wykresie, bo nie ma historii cen ani ostatniej ceny. {exc}')
            continue

        price_points = {
            point['date']: point['close']
            for point in history.get('points') or []
            if point.get('date') is not None and point.get('close') is not None
        }
        if not price_points:
            if instrument.last_price is not None:
                initial_prices[instrument.id] = instrument.last_price
                warnings.append(
                    f'{instrument.ticker}: historia cen jest pusta, użyto ostatniej zapisanej ceny {instrument.last_price}.'
                )
            else:
                warnings.append(f'{instrument.ticker}: pominięto w wykresie, bo historia cen jest pusta.')
            continue

        prices_by_instrument[instrument.id] = price_points
        initial_prices[instrument.id] = price_points[min(price_points)]
        if history.get('source'):
            sources.add(history['source'])

    return prices_by_instrument, initial_prices, sorted(sources), warnings


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

    fx_source = ''
    fx_table_date = ''
    if all(currency == target_currency for currency in currencies):
        fx_rates = {target_currency: Decimal('1'), 'PLN': Decimal('1')}
        warnings = []
    else:
        try:
            fx_data = fetch_latest_fx_rates_to_pln(currencies)
        except MarketDataError as exc:
            fx_rates = {'PLN': Decimal('1'), target_currency: Decimal('1')}
            warnings = [f'Nie pobrano aktualnych kursów walut: {exc}']
        else:
            fx_rates = fx_data['rates']
            fx_source = fx_data.get('source') or ''
            fx_table_date = fx_data.get('table_date') or ''
            warnings = []

    price_histories, initial_prices, price_sources, price_warnings = _load_price_histories(
        instruments.values(),
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
