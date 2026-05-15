import csv
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .brokerage import get_quantity
from .models import BrokerageAccount, BrokerageDividend, BrokerageInstrument


ALPHA_VANTAGE_URL = 'https://www.alphavantage.co/query'
OPENFIGI_MAPPING_URL = 'https://api.openfigi.com/v3/mapping'
STOOQ_QUOTE_URL = 'https://stooq.pl/q/l/'
STOOQ_DAILY_URL = 'https://stooq.com/q/d/l/'


class MarketDataError(Exception):
    pass


def _decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None


def _request_json(url, *, data=None, headers=None):
    request_headers = headers or {}
    if data is not None:
        request_headers = {'Content-Type': 'application/json', **request_headers}
        request = Request(
            url,
            data=json.dumps(data).encode('utf-8'),
            headers=request_headers,
            method='POST',
        )
    else:
        request = Request(url, headers=request_headers)

    try:
        with urlopen(request, timeout=12) as response:
            return json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        raise MarketDataError(f'Dostawca danych zwrócił HTTP {exc.code}: {exc.reason}') from exc
    except URLError as exc:
        raise MarketDataError(f'Nie udało się połączyć z dostawcą danych: {exc.reason}') from exc
    except json.JSONDecodeError as exc:
        raise MarketDataError('Dostawca danych zwrócił nieprawidłową odpowiedź JSON.') from exc


def _get_json(url):
    return _request_json(url)


def _read_csv_url(url, timeout=12):
    try:
        with urlopen(url, timeout=timeout) as response:
            content = response.read().decode('utf-8-sig')
        return list(csv.DictReader(StringIO(content)))
    except HTTPError as exc:
        raise MarketDataError(f'Dostawca danych zwrócił HTTP {exc.code}: {exc.reason}') from exc
    except URLError as exc:
        raise MarketDataError(f'Nie udało się połączyć z dostawcą danych: {exc.reason}') from exc


def _is_warsaw_market(exchange='', currency=''):
    market = (exchange or '').strip().upper()
    return market in {'GPW', 'WSE', 'WARSAW', 'XWAR'} or (currency or '').strip().upper() == 'PLN'


def _stooq_symbol(symbol, exchange='', currency=''):
    clean_symbol = (symbol or '').strip().upper()
    if clean_symbol.endswith('.WA'):
        return f'{clean_symbol[:-3].lower()}.pl'
    if clean_symbol.endswith('.PL'):
        return clean_symbol.lower()
    if '.' not in clean_symbol and _is_warsaw_market(exchange, currency):
        return f'{clean_symbol.lower()}.pl'
    return clean_symbol.lower()


def _stooq_symbol_candidates(symbol, exchange='', currency=''):
    primary = _stooq_symbol(symbol, exchange, currency)
    candidates = [primary]
    clean_symbol = (symbol or '').strip().upper()

    if _is_warsaw_market(exchange, currency):
        base_symbol = clean_symbol.split('.')[0]
        for candidate in (f'{base_symbol.lower()}.pl', base_symbol.lower()):
            if candidate not in candidates:
                candidates.append(candidate)

    return candidates


def _stooq_lookup_url(query):
    clean_query = (query or '').strip()
    if not clean_query:
        return 'https://stooq.pl/'
    return f'https://stooq.pl/q/?s={quote_plus(clean_query)}'


def _row_preview(row):
    if not row:
        return 'pusta odpowiedź'
    return ', '.join(f'{key}={value}' for key, value in list(row.items())[:8])


def _extract_stooq_price(row):
    for key in ('Close', 'Zamkniecie', 'Zamknięcie', 'Last', 'Kurs', 'Price'):
        price = _decimal(row.get(key))
        if price is not None:
            return price
    return None


class AlphaVantageClient:
    source_name = 'Alpha Vantage'

    def __init__(self, api_key):
        if not api_key:
            raise MarketDataError('Brak ALPHA_VANTAGE_API_KEY w konfiguracji.')
        self.api_key = api_key

    def _get(self, params):
        params = {**params, 'apikey': self.api_key}
        payload = _get_json(f"{ALPHA_VANTAGE_URL}?{urlencode(params)}")

        if 'Error Message' in payload:
            raise MarketDataError(payload['Error Message'])
        if 'Note' in payload:
            raise MarketDataError(payload['Note'])
        return payload

    def fetch_dividends(self, symbol):
        payload = self._get({'function': 'DIVIDENDS', 'symbol': symbol})
        if not isinstance(payload, dict):
            raise MarketDataError(f'Nieprawidłowa odpowiedź dywidend Alpha Vantage dla symbolu {symbol}.')
        return payload.get('data') or []

    def fetch_quote(self, symbol):
        payload = self._get({'function': 'GLOBAL_QUOTE', 'symbol': symbol})
        quote = payload.get('Global Quote') or {}
        price = _decimal(quote.get('05. price'))
        if price is None:
            raise MarketDataError(f'Nie udało się pobrać ceny Alpha Vantage dla symbolu {symbol}.')
        return price


class StooqClient:
    source_name = 'Stooq'

    def fetch_quote(self, symbol, exchange='', currency=''):
        errors = []
        for stooq_symbol in _stooq_symbol_candidates(symbol, exchange, currency):
            params = urlencode({'s': stooq_symbol, 'f': 'sd2t2ohlcv', 'h': '', 'e': 'csv'})
            rows = _read_csv_url(f'{STOOQ_QUOTE_URL}?{params}')
            if not rows:
                errors.append(f'{stooq_symbol}: brak danych')
                continue

            price = _extract_stooq_price(rows[0])
            if price is not None:
                return price

            errors.append(f'{stooq_symbol}: brak ceny ({_row_preview(rows[0])})')

        raise MarketDataError(f'Nie udało się pobrać ceny Stooq. Próby: {"; ".join(errors)}.')

    def fetch_daily_close(self, symbol, trade_date, exchange='', currency=''):
        errors = []
        for stooq_symbol in _stooq_symbol_candidates(symbol, exchange, currency):
            date_from = (trade_date - timedelta(days=7)).strftime('%Y%m%d')
            date_to = trade_date.strftime('%Y%m%d')
            params = urlencode({'s': stooq_symbol, 'd1': date_from, 'd2': date_to, 'i': 'd'})
            rows = _read_csv_url(f'{STOOQ_DAILY_URL}?{params}')
            valid_rows = [row for row in rows if row.get('Date') and row.get('Date') != 'No data']
            if not valid_rows:
                errors.append(f'{stooq_symbol}: brak dziennych danych')
                continue

            price = _extract_stooq_price(valid_rows[-1])
            if price is not None:
                return price

            errors.append(f'{stooq_symbol}: brak ceny zamknięcia ({_row_preview(valid_rows[-1])})')

        raise MarketDataError(f'Brak dziennych danych Stooq. Próby: {"; ".join(errors)}.')


class OpenFigiClient:
    source_name = 'OpenFIGI'

    def __init__(self, api_key=''):
        self.api_key = api_key

    def _headers(self):
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['X-OPENFIGI-APIKEY'] = self.api_key
        return headers

    def _market_filters(self, exchange='', currency=''):
        filters = {}
        market = (exchange or '').strip().upper()
        if market in {'GPW', 'WSE', 'WARSAW', 'XWAR'}:
            filters['micCode'] = 'XWAR'
        elif len(market) == 4:
            filters['micCode'] = market
        elif market:
            filters['exchCode'] = market

        if currency:
            filters['currency'] = currency.strip().upper()

        return filters

    def search_by_isin(self, isin, exchange='', currency=''):
        clean_isin = (isin or '').strip().upper()
        if not clean_isin:
            raise MarketDataError('Podaj ISIN instrumentu.')

        base_job = {'idType': 'ID_ISIN', 'idValue': clean_isin}
        filtered_job = {**base_job, **self._market_filters(exchange, currency)}
        jobs = [filtered_job, base_job] if filtered_job != base_job else [base_job]

        payload = _request_json(OPENFIGI_MAPPING_URL, data=jobs, headers=self._headers())
        if not isinstance(payload, list):
            raise MarketDataError('OpenFIGI zwróciło nieprawidłową odpowiedź.')

        records = []
        for result in payload:
            records.extend(result.get('data') or [])

        record = self._select_record(records, exchange, currency)
        if not record:
            raise MarketDataError(f'Nie znaleziono instrumentu dla ISIN {clean_isin}.')

        symbol = record.get('ticker')
        if not symbol:
            raise MarketDataError(f'OpenFIGI nie zwróciło tickera dla ISIN {clean_isin}.')

        return {
            'symbol': symbol,
            'name': record.get('name') or record.get('securityDescription') or '',
            'isin': record.get('isin') or clean_isin,
            'exchange': record.get('micCode') or record.get('exchCode') or '',
            'currency': currency or '',
            'figi': record.get('figi', ''),
        }

    def _select_record(self, records, exchange='', currency=''):
        if not records:
            return None

        market = (exchange or '').strip().upper()
        preferred_mic = 'XWAR' if market in {'GPW', 'WSE', 'WARSAW', 'XWAR'} else market

        def score(record):
            value = 0
            if preferred_mic and record.get('micCode', '').upper() == preferred_mic:
                value += 10
            if preferred_mic and record.get('exchCode', '').upper() == preferred_mic:
                value += 6
            if record.get('marketSector', '').lower() == 'equity':
                value += 3
            if record.get('securityType2', '').lower() in {'common stock', 'etp', 'fund'}:
                value += 2
            if record.get('ticker'):
                value += 1
            return value

        return max(records, key=score)


def get_market_data_client():
    return AlphaVantageClient(getattr(settings, 'ALPHA_VANTAGE_API_KEY', ''))


def get_openfigi_client():
    return OpenFigiClient(getattr(settings, 'OPENFIGI_API_KEY', ''))


def resolve_instrument_by_isin(isin, exchange='', currency=''):
    return get_openfigi_client().search_by_isin(isin, exchange, currency)


def fetch_latest_market_price(symbol='', exchange='', currency='', isin='', price_symbol=''):
    resolved = None
    quote_symbol = price_symbol.strip().upper() if price_symbol else ''
    if isin:
        resolved = resolve_instrument_by_isin(isin, exchange, currency)
        symbol = resolved['symbol']
        exchange = resolved.get('exchange', exchange)
        currency = resolved.get('currency', currency)
    if not quote_symbol:
        quote_symbol = symbol

    if not quote_symbol:
        raise MarketDataError('Brak symbolu albo ISIN do pobrania ceny.')

    source = ''
    quote_errors = []
    if _is_warsaw_market(exchange, currency):
        try:
            stooq_client = StooqClient()
            price = stooq_client.fetch_quote(quote_symbol, exchange, currency)
            source = stooq_client.source_name
        except MarketDataError as exc:
            price = None
            quote_errors.append(str(exc))
    else:
        price = None

    if price is None and not _is_warsaw_market(exchange, currency):
        alpha_key = getattr(settings, 'ALPHA_VANTAGE_API_KEY', '')
        if alpha_key:
            try:
                alpha_client = AlphaVantageClient(alpha_key)
                price = alpha_client.fetch_quote(quote_symbol)
                source = alpha_client.source_name
            except MarketDataError as exc:
                price = None
                quote_errors.append(str(exc))

    if price is None and price_symbol:
        try:
            stooq_client = StooqClient()
            price = stooq_client.fetch_quote(quote_symbol, exchange, currency)
            source = stooq_client.source_name
        except MarketDataError as exc:
            price = None
            quote_errors.append(str(exc))

    if price is None:
        search_term = resolved.get('name') if resolved else ''
        search_term = search_term or symbol or quote_symbol
        details = f' Szczegóły: {"; ".join(quote_errors)}' if quote_errors else ''
        raise MarketDataError(
            f'OpenFIGI znalazło symbol {symbol or quote_symbol}, ale nie udało się pobrać ceny rynkowej '
            f'dla symbolu ceny {quote_symbol}. W Stooq wyszukaj instrument po nazwie: '
            f'{_stooq_lookup_url(search_term)} i wpisz znaleziony symbol w polu "Symbol ceny". '
            f'Możesz też wpisać cenę ręcznie.{details}'
        )

    return {
        'price': price,
        'source': source,
        'symbol': symbol,
        'price_symbol': quote_symbol if quote_symbol != symbol else '',
        'name': resolved.get('name', '') if resolved else '',
        'isin': resolved.get('isin', isin) if resolved else isin,
        'exchange': resolved.get('exchange', exchange) if resolved else exchange,
        'currency': resolved.get('currency', currency) if resolved else currency,
    }


def fetch_transaction_market_price(isin, trade_date=None, trade_time=None, exchange='', currency=''):
    resolved = resolve_instrument_by_isin(isin, exchange, currency)
    symbol = resolved['symbol']
    resolved_exchange = resolved.get('exchange', exchange)
    resolved_currency = resolved.get('currency', currency)

    if trade_date and _is_warsaw_market(resolved_exchange, resolved_currency):
        stooq_client = StooqClient()
        try:
            price = stooq_client.fetch_daily_close(symbol, trade_date, resolved_exchange, resolved_currency)
            source = f'{stooq_client.source_name} dzienne zamknięcie'
        except MarketDataError:
            price = stooq_client.fetch_quote(symbol, resolved_exchange, resolved_currency)
            source = f'{stooq_client.source_name} najnowsza cena'
        return {
            'price': price,
            'source': source,
            'symbol': symbol,
            'name': resolved.get('name', ''),
            'isin': resolved.get('isin', isin),
            'exchange': resolved_exchange,
            'currency': resolved_currency,
        }

    return fetch_latest_market_price(symbol=symbol, exchange=resolved_exchange, currency=resolved_currency, isin='')


def merge_duplicate_instrument(instrument, target_symbol):
    duplicate = (
        BrokerageInstrument.objects
        .filter(user=instrument.user, ticker=target_symbol)
        .exclude(id=instrument.id)
        .first()
    )
    if duplicate is None:
        return instrument, False

    with transaction.atomic():
        instrument.transactions.update(instrument=duplicate)
        instrument.dividends.update(instrument=duplicate)

        if not duplicate.isin and instrument.isin:
            duplicate.isin = instrument.isin
        if not duplicate.price_symbol and instrument.price_symbol:
            duplicate.price_symbol = instrument.price_symbol
        if not duplicate.exchange and instrument.exchange:
            duplicate.exchange = instrument.exchange
        if not duplicate.currency and instrument.currency:
            duplicate.currency = instrument.currency
        if duplicate.last_price is None and instrument.last_price is not None:
            duplicate.last_price = instrument.last_price
            duplicate.last_price_at = instrument.last_price_at
            duplicate.market_data_source = instrument.market_data_source
        duplicate.save(update_fields=['isin', 'price_symbol', 'exchange', 'currency', 'last_price', 'last_price_at', 'market_data_source'])

        instrument.delete()

    return duplicate, True


def refresh_market_data_for_user(user):
    alpha_client = None
    alpha_key = getattr(settings, 'ALPHA_VANTAGE_API_KEY', '')
    if alpha_key:
        alpha_client = AlphaVantageClient(alpha_key)
    updated_quotes = 0
    updated_dividends = 0
    merged_instruments = 0
    failed_quotes = []
    failed_dividends = []
    sources = set()
    today = timezone.localdate()

    instruments = BrokerageInstrument.objects.filter(user=user)
    accounts = BrokerageAccount.objects.filter(user=user)

    for instrument in instruments:
        try:
            market_data = fetch_latest_market_price(
                symbol=instrument.ticker,
                exchange=instrument.exchange,
                currency=instrument.currency,
                isin=instrument.isin,
                price_symbol=instrument.price_symbol,
            )
        except MarketDataError as exc:
            failed_quotes.append(f'{instrument.ticker}: {exc}')
            continue

        instrument, merged = merge_duplicate_instrument(instrument, market_data['symbol'])
        if merged:
            merged_instruments += 1

        instrument.ticker = market_data['symbol']
        instrument.last_price = market_data['price']
        instrument.last_price_at = timezone.now()
        instrument.market_data_source = market_data['source']
        if market_data.get('exchange'):
            instrument.exchange = market_data['exchange']
        if market_data.get('currency'):
            instrument.currency = market_data['currency']
        if market_data.get('price_symbol') and not instrument.price_symbol:
            instrument.price_symbol = market_data['price_symbol']
        instrument.save(update_fields=['ticker', 'price_symbol', 'last_price', 'last_price_at', 'market_data_source', 'exchange', 'currency'])
        updated_quotes += 1
        sources.add(market_data['source'])

        if alpha_client is None:
            continue

        try:
            dividend_items = alpha_client.fetch_dividends(instrument.ticker)
        except MarketDataError as exc:
            failed_dividends.append(f'{instrument.ticker}: {exc}')
            continue

        for item in dividend_items:
            payment_date = item.get('payment_date')
            amount = _decimal(item.get('amount'))
            if not payment_date or amount is None:
                continue

            ex_dividend_date = item.get('ex_dividend_date') or None
            if payment_date < today.isoformat():
                continue

            for account in accounts:
                quantity_date = ex_dividend_date or today
                if get_quantity(account, instrument, quantity_date) <= 0:
                    continue

                _, created = BrokerageDividend.objects.update_or_create(
                    account=account,
                    instrument=instrument,
                    payment_date=payment_date,
                    defaults={
                        'ex_dividend_date': ex_dividend_date,
                        'gross_amount_per_share': amount,
                        'currency': instrument.currency,
                        'tax_rate': BrokerageDividend._meta.get_field('tax_rate').default,
                        'status': BrokerageDividend.PLANNED,
                        'source': alpha_client.source_name,
                    },
                )
                if created:
                    updated_dividends += 1
                    sources.add(alpha_client.source_name)

    return {
        'updated_quotes': updated_quotes,
        'updated_dividends': updated_dividends,
        'merged_instruments': merged_instruments,
        'failed_quotes': failed_quotes,
        'failed_dividends': failed_dividends,
        'source': ', '.join(sorted(sources)) or 'brak źródła',
    }
