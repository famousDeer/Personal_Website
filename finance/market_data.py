import csv
import json
import re
import unicodedata
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
from io import StringIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import transaction
from django.db.models import Max, Min
from django.utils import timezone

from .brokerage import get_quantity
from .models import (
    BrokerageAccount,
    BrokerageDailyPrice,
    BrokerageDividend,
    BrokerageInstrument,
    BrokeragePositionSnapshot,
    BrokeragePriceSnapshot,
    BrokerageTransaction,
)


ALPHA_VANTAGE_URL = 'https://www.alphavantage.co/query'
OPENFIGI_MAPPING_URL = 'https://api.openfigi.com/v3/mapping'
STOOQ_QUOTE_URL = 'https://stooq.pl/q/l/'
STOOQ_DAILY_URL = 'https://stooq.com/q/d/l/'
STOOQ_DAILY_URLS = ('https://stooq.pl/q/d/l/', 'https://stooq.com/q/d/l/')
YAHOO_CHART_URL = 'https://query2.finance.yahoo.com/v8/finance/chart/'
YAHOO_SPARK_URL = 'https://query2.finance.yahoo.com/v7/finance/spark'
YAHOO_SPARK_URLS = (
    YAHOO_SPARK_URL,
    'https://query1.finance.yahoo.com/v7/finance/spark',
)
NBP_TABLE_A_URL = 'https://api.nbp.pl/api/exchangerates/tables/a/?format=json'

YAHOO_XTB_SUFFIXES = {
    'US': '',
    'PL': 'WA',
    'WA': 'WA',
    'NL': 'AS',
    'FR': 'PA',
    'UK': 'L',
    'DE': 'DE',
    'ES': 'MC',
    'IT': 'MI',
    'CH': 'SW',
    'BE': 'BR',
    'PT': 'LS',
    'SE': 'ST',
    'NO': 'OL',
    'DK': 'CO',
    'FI': 'HE',
    'AT': 'VI',
    'CZ': 'PR',
}

# Some GPW ETFs use a broker/Bloomberg ticker that Yahoo does not publish.
# Keep the alias explicit so the quote stays on the same PLN listing instead
# of silently falling back to a foreign-currency listing of the same fund.
YAHOO_SYMBOL_OVERRIDES = {
    'LYPS.PL': 'ETFSP500.WA',
    'LYPS.WA': 'ETFSP500.WA',
}

YAHOO_EXCHANGE_SUFFIXES = {
    'GPW': 'WA',
    'WSE': 'WA',
    'WARSAW': 'WA',
    'XWAR': 'WA',
    'AMS': 'AS',
    'XAMS': 'AS',
    'PAR': 'PA',
    'XPAR': 'PA',
    'LSE': 'L',
    'XLON': 'L',
    'GER': 'DE',
    'XETR': 'DE',
}

ALPHA_VANTAGE_XTB_SUFFIXES = {
    'US': '',
    'PL': 'WAR',
    'WA': 'WAR',
    'NL': 'AMS',
    'FR': 'PAR',
    'UK': 'LON',
    'DE': 'DEX',
}

YAHOO_HEADERS = {
    'Accept': 'application/json,text/plain,*/*',
    'User-Agent': 'Mozilla/5.0 (compatible; Website-Finance/1.0)',
}


class MarketDataError(Exception):
    pass


def _decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None


def _date(value):
    if isinstance(value, date):
        return value
    if value in (None, '', 'None', 'null', 'NULL', '0000-00-00'):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _integer(value):
    if value in (None, '', 'None', 'null', 'NULL'):
        return None
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def _request_json(url, *, data=None, headers=None, timeout=12):
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
        with urlopen(request, timeout=timeout) as response:
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
        request = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urlopen(request, timeout=timeout) as response:
            content = response.read().decode('utf-8-sig')
        if 'get_apikey' in content and 'apikey' in content.lower():
            raise MarketDataError(
                'Stooq wymaga klucza API dla historycznych danych CSV. '
                'Wygeneruj klucz na stronie Stooq i ustaw STOOQ_API_KEY w pliku .env.'
            )
        try:
            dialect = csv.Sniffer().sniff(content[:1024], delimiters=',;')
        except csv.Error:
            dialect = csv.excel
        return list(csv.DictReader(StringIO(content), dialect=dialect))
    except HTTPError as exc:
        raise MarketDataError(f'Dostawca danych zwrócił HTTP {exc.code}: {exc.reason}') from exc
    except URLError as exc:
        raise MarketDataError(f'Nie udało się połączyć z dostawcą danych: {exc.reason}') from exc


def _is_warsaw_market(exchange='', currency='', symbol=''):
    market = (exchange or '').strip().upper()
    clean_symbol = (symbol or '').strip().upper()
    if clean_symbol.endswith(('.PL', '.WA')):
        return True
    # A foreign XTB suffix is stronger evidence than the account currency.
    # This matters for USD/EUR ETFs held inside a PLN-denominated IKE account.
    if '.' in clean_symbol:
        return False
    if market:
        return market in {'GPW', 'WSE', 'WARSAW', 'XWAR'}
    return (currency or '').strip().upper() == 'PLN'


def _stooq_symbol(symbol, exchange='', currency=''):
    clean_symbol = (symbol or '').strip().upper()
    if clean_symbol.endswith('.WA'):
        return f'{clean_symbol[:-3].lower()}.pl'
    if clean_symbol.endswith('.PL'):
        return clean_symbol.lower()
    if '.' not in clean_symbol and _is_warsaw_market(exchange, currency):
        return f'{clean_symbol.lower()}.pl'
    return clean_symbol.lower()


def _ascii_upper(value):
    normalized = unicodedata.normalize('NFKD', value or '')
    return ''.join(char for char in normalized if not unicodedata.combining(char)).upper()


def _stooq_name_candidates(name):
    clean_name = _ascii_upper(name)
    clean_name = re.sub(r'[^A-Z0-9]+', ' ', clean_name)
    stop_words = {
        'SA', 'S A', 'S', 'AKCYJNA', 'SPOLKA', 'SPOLKA AKCYJNA', 'PLC',
        'INC', 'CORP', 'CORPORATION', 'LTD', 'LIMITED', 'NV', 'AG', 'SE',
        'THE', 'CO', 'COMPANY', 'GROUP', 'HOLDING', 'HOLDINGS',
    }
    tokens = [token for token in clean_name.split() if token not in stop_words]
    candidates = []
    if tokens:
        candidates.append(tokens[0])
        joined = ''.join(tokens[:2])
        if 2 <= len(joined) <= 16:
            candidates.append(joined)
    return candidates


def _stooq_symbol_candidates(symbol, exchange='', currency='', extra_symbols=()):
    raw_symbols = [symbol, *extra_symbols]
    candidates = []
    if _is_warsaw_market(exchange, currency, symbol):
        expanded_symbols = []
        for raw_symbol in raw_symbols:
            clean_symbol = _ascii_upper(raw_symbol).strip()
            if not clean_symbol:
                continue
            expanded_symbols.append(clean_symbol)
            expanded_symbols.extend(_stooq_name_candidates(clean_symbol))

        for clean_symbol in expanded_symbols:
            base_symbol = clean_symbol.split('.')[0]
            base_symbol = re.sub(r'[^A-Z0-9]+', '', base_symbol)
            if not base_symbol:
                continue
            for candidate in (f'{base_symbol.lower()}.pl', base_symbol.lower(), f'{base_symbol.lower()}.wa'):
                if candidate not in candidates:
                    candidates.append(candidate)
    else:
        for raw_symbol in raw_symbols:
            clean_symbol = (raw_symbol or '').strip()
            if not clean_symbol:
                continue
            candidate = _stooq_symbol(clean_symbol, exchange, currency)
            if candidate not in candidates:
                candidates.append(candidate)

    return candidates


def _stooq_lookup_url(query):
    clean_query = (query or '').strip()
    if not clean_query:
        return 'https://stooq.pl/'
    return f'https://stooq.pl/q/?s={quote_plus(clean_query)}'


def _stooq_history_key_url(symbol):
    clean_symbol = (symbol or '').strip().lower()
    if not clean_symbol:
        clean_symbol = 'kru'
    return f'https://stooq.pl/q/d/?s={quote_plus(clean_symbol)}&get_apikey'


def _stooq_api_key():
    return (getattr(settings, 'STOOQ_API_KEY', '') or '').strip()


def _stooq_history_key_error(symbol):
    return (
        'Stooq wymaga klucza API dla historycznych danych CSV. '
        f'Wejdź na {_stooq_history_key_url(symbol)}, przejdź captcha, skopiuj apikey '
        'i ustaw STOOQ_API_KEY w pliku .env.'
    )


def _stooq_daily_params(params):
    api_key = _stooq_api_key()
    if api_key:
        params = {**params, 'apikey': api_key}
    return urlencode(params)


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


def _datetime_from_timestamp(value):
    try:
        timestamp = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=datetime_timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _exchange_date(observed_at, timezone_name=''):
    if observed_at is None:
        return None
    if timezone_name:
        try:
            return observed_at.astimezone(ZoneInfo(timezone_name)).date()
        except ZoneInfoNotFoundError:
            pass
    return observed_at.date()


def _yahoo_currency(value):
    clean_currency = (value or '').strip()
    if clean_currency == 'GBp':
        return 'GBX'
    return clean_currency.upper()


def _yahoo_symbol_candidates(symbol, exchange='', currency='', extra_symbols=()):
    """Translate broker/XTB symbols into Yahoo's exchange suffix notation."""
    candidates = []

    def add(candidate):
        candidate = (candidate or '').strip().upper()
        candidate = YAHOO_SYMBOL_OVERRIDES.get(candidate, candidate)
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    market = (exchange or '').strip().upper()
    exchange_suffix = YAHOO_EXCHANGE_SUFFIXES.get(market)
    for raw_symbol in (symbol, *extra_symbols):
        clean_symbol = (raw_symbol or '').strip().upper()
        if not clean_symbol or ' ' in clean_symbol:
            continue

        overridden_symbol = YAHOO_SYMBOL_OVERRIDES.get(clean_symbol)
        if overridden_symbol:
            add(overridden_symbol)
            continue

        if '.' in clean_symbol:
            base, suffix = clean_symbol.rsplit('.', 1)
            if suffix in YAHOO_XTB_SUFFIXES:
                yahoo_suffix = YAHOO_XTB_SUFFIXES[suffix]
                if suffix == 'US':
                    base = base.replace('.', '-')
                add(f'{base}.{yahoo_suffix}' if yahoo_suffix else base)
                continue

        if exchange_suffix and '.' not in clean_symbol:
            add(f'{clean_symbol}.{exchange_suffix}')
        elif not market and (currency or '').strip().upper() == 'PLN' and '.' not in clean_symbol:
            add(f'{clean_symbol}.WA')
        add(clean_symbol)

    return candidates


def _alpha_vantage_symbol(symbol):
    clean_symbol = (symbol or '').strip().upper()
    if '.' not in clean_symbol:
        return clean_symbol
    base, suffix = clean_symbol.rsplit('.', 1)
    if suffix not in ALPHA_VANTAGE_XTB_SUFFIXES:
        return clean_symbol
    provider_suffix = ALPHA_VANTAGE_XTB_SUFFIXES[suffix]
    return f'{base}.{provider_suffix}' if provider_suffix else base.replace('.', '-')


class YahooFinanceClient:
    """Small no-dependency client for delayed/current chart data.

    The bulk Spark endpoint keeps a whole portfolio refresh to one request and
    returns the same market metadata and recent closes as the Chart endpoint.
    """

    source_name = 'Yahoo Finance'
    # Spark rejects larger real-world symbol sets with HTTP 400. Keeping the
    # batch below that boundary still refreshes this portfolio in two calls.
    max_symbols_per_request = 20

    def _parse_chart(self, chart, requested_symbol, *, include_current=True):
        if not isinstance(chart, dict):
            raise MarketDataError(f'Yahoo Finance nie zwróciło danych dla {requested_symbol}.')

        meta = chart.get('meta') or {}
        timestamps = chart.get('timestamp') or []
        indicators = chart.get('indicators') or {}
        quote_rows = indicators.get('quote') or []
        quote_row = quote_rows[0] if quote_rows else {}
        adjusted_rows = indicators.get('adjclose') or []
        adjusted_row = adjusted_rows[0] if adjusted_rows else {}
        closes = quote_row.get('close') or []
        adjusted_closes = adjusted_row.get('adjclose') or []
        opens = quote_row.get('open') or []
        highs = quote_row.get('high') or []
        lows = quote_row.get('low') or []
        volumes = quote_row.get('volume') or []
        timezone_name = meta.get('exchangeTimezoneName') or ''
        points = []

        for index, timestamp in enumerate(timestamps):
            close = _decimal(closes[index] if index < len(closes) else None)
            observed_at = _datetime_from_timestamp(timestamp)
            if close is None or observed_at is None:
                continue
            points.append({
                'date': _exchange_date(observed_at, timezone_name),
                'observed_at': observed_at,
                'open': _decimal(opens[index] if index < len(opens) else None),
                'high': _decimal(highs[index] if index < len(highs) else None),
                'low': _decimal(lows[index] if index < len(lows) else None),
                'close': close,
                'adjusted_close': _decimal(
                    adjusted_closes[index] if index < len(adjusted_closes) else None
                ),
                'volume': _integer(volumes[index] if index < len(volumes) else None),
            })

        regular_price = _decimal(meta.get('regularMarketPrice')) if include_current else None
        regular_at = _datetime_from_timestamp(meta.get('regularMarketTime')) if include_current else None
        if regular_price is not None and regular_at is not None:
            regular_date = _exchange_date(regular_at, timezone_name)
            if points and points[-1]['date'] == regular_date:
                points[-1]['close'] = regular_price
                points[-1]['observed_at'] = regular_at
            else:
                points.append({
                    'date': regular_date,
                    'observed_at': regular_at,
                    'open': None,
                    'high': None,
                    'low': None,
                    'close': regular_price,
                    'adjusted_close': regular_price,
                    'volume': None,
                })

        price = regular_price
        if price is None and points:
            price = points[-1]['close']
        observed_at = regular_at
        if observed_at is None and points:
            observed_at = points[-1]['observed_at']
        if price is None:
            raise MarketDataError(f'Yahoo Finance nie zwróciło ceny dla {requested_symbol}.')

        return {
            'price': price,
            'observed_at': observed_at,
            'points': points,
            'symbol': (meta.get('symbol') or requested_symbol).strip().upper(),
            'currency': _yahoo_currency(meta.get('currency')),
            'exchange': (meta.get('exchangeName') or '').strip().upper(),
            'name': meta.get('longName') or meta.get('shortName') or '',
        }

    def _chart(self, symbol, params, *, include_current=True):
        encoded_symbol = quote(symbol, safe='.-^=')
        payload = _request_json(
            f'{YAHOO_CHART_URL}{encoded_symbol}?{urlencode(params)}',
            headers=YAHOO_HEADERS,
            timeout=8,
        )
        chart_payload = payload.get('chart') if isinstance(payload, dict) else None
        if not isinstance(chart_payload, dict):
            raise MarketDataError(f'Yahoo Finance zwróciło nieprawidłową odpowiedź dla {symbol}.')
        error = chart_payload.get('error')
        if error:
            description = error.get('description') if isinstance(error, dict) else str(error)
            raise MarketDataError(f'Yahoo Finance: {description or "brak danych"}.')
        results = chart_payload.get('result') or []
        if not results:
            raise MarketDataError(f'Yahoo Finance nie znalazło symbolu {symbol}.')
        return self._parse_chart(results[0], symbol, include_current=include_current)

    def fetch_quote(self, symbol):
        return self._chart(
            symbol,
            {'interval': '1d', 'range': '5d', 'includePrePost': 'false', 'events': 'div,splits'},
        )

    def fetch_quotes(self, symbols):
        clean_symbols = []
        for symbol in symbols:
            clean_symbol = (symbol or '').strip().upper()
            if clean_symbol and clean_symbol not in clean_symbols:
                clean_symbols.append(clean_symbol)

        quotes = {}
        for offset in range(0, len(clean_symbols), self.max_symbols_per_request):
            batch = clean_symbols[offset:offset + self.max_symbols_per_request]
            spark = None
            last_error = None
            params = urlencode({'symbols': ','.join(batch), 'range': '5d', 'interval': '1d'})
            for spark_url in YAHOO_SPARK_URLS:
                try:
                    payload = _request_json(
                        f'{spark_url}?{params}',
                        headers=YAHOO_HEADERS,
                        timeout=8,
                    )
                except MarketDataError as exc:
                    last_error = exc
                    continue
                candidate_spark = payload.get('spark') if isinstance(payload, dict) else None
                if not isinstance(candidate_spark, dict):
                    last_error = MarketDataError(
                        'Yahoo Finance zwróciło nieprawidłową odpowiedź zbiorczą.'
                    )
                    continue
                error = candidate_spark.get('error')
                if error and not candidate_spark.get('result'):
                    description = error.get('description') if isinstance(error, dict) else str(error)
                    last_error = MarketDataError(
                        f'Yahoo Finance: {description or "błąd zapytania zbiorczego"}.'
                    )
                    continue
                spark = candidate_spark
                break

            if spark is None:
                raise last_error or MarketDataError('Yahoo Finance nie zwróciło danych zbiorczych.')
            for item in spark.get('result') or []:
                requested_symbol = (item.get('symbol') or '').strip().upper()
                responses = item.get('response') or []
                if not requested_symbol or not responses:
                    continue
                try:
                    quotes[requested_symbol] = self._parse_chart(responses[0], requested_symbol)
                except MarketDataError:
                    continue
        return quotes

    def fetch_daily_history(self, symbol, start_date, end_date, *, allow_empty=False):
        period_start = datetime.combine(start_date, time.min, tzinfo=datetime_timezone.utc)
        period_end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=datetime_timezone.utc)
        result = self._chart(
            symbol,
            {
                'interval': '1d',
                'period1': int(period_start.timestamp()),
                'period2': int(period_end.timestamp()),
                'includePrePost': 'false',
                'events': 'div,splits',
            },
            include_current=False,
        )
        points = [point for point in result['points'] if start_date <= point['date'] <= end_date]
        if not points and not allow_empty:
            raise MarketDataError(f'Yahoo Finance nie zwróciło historii dla {symbol}.')
        return points


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
        if 'Information' in payload:
            raise MarketDataError(payload['Information'])
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

    def fetch_daily_history(self, symbol, outputsize='compact'):
        payload = self._get({
            'function': 'TIME_SERIES_DAILY',
            'symbol': symbol,
            'outputsize': outputsize,
        })
        series = payload.get('Time Series (Daily)') or {}
        if not series:
            raise MarketDataError(f'Nie udało się pobrać historycznych cen Alpha Vantage dla symbolu {symbol}.')

        points = []
        for day, values in series.items():
            point_date = _date(day)
            close = _decimal(values.get('4. close'))
            if point_date is None or close is None:
                continue
            points.append({
                'date': point_date,
                'open': _decimal(values.get('1. open')),
                'high': _decimal(values.get('2. high')),
                'low': _decimal(values.get('3. low')),
                'close': close,
                'volume': _integer(values.get('5. volume')),
            })

        if not points:
            raise MarketDataError(f'Alpha Vantage nie zwróciło poprawnych punktów cenowych dla symbolu {symbol}.')
        return sorted(points, key=lambda item: item['date'])


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
        if not _stooq_api_key():
            raise MarketDataError(_stooq_history_key_error(symbol))

        errors = []
        for stooq_symbol in _stooq_symbol_candidates(symbol, exchange, currency):
            date_from = (trade_date - timedelta(days=7)).strftime('%Y%m%d')
            date_to = trade_date.strftime('%Y%m%d')
            params = _stooq_daily_params({'s': stooq_symbol, 'd1': date_from, 'd2': date_to, 'i': 'd'})
            for daily_url in STOOQ_DAILY_URLS:
                rows = _read_csv_url(f'{daily_url}?{params}')
                valid_rows = [row for row in rows if row.get('Date') and row.get('Date') != 'No data']
                if not valid_rows:
                    errors.append(f'{stooq_symbol}: brak dziennych danych')
                    continue

                price = _extract_stooq_price(valid_rows[-1])
                if price is not None:
                    return price

                errors.append(f'{stooq_symbol}: brak ceny zamknięcia ({_row_preview(valid_rows[-1])})')

        raise MarketDataError(f'Brak dziennych danych Stooq. Próby: {"; ".join(errors)}.')

    def fetch_daily_history(self, symbol, start_date, end_date, exchange='', currency='', extra_symbols=()):
        if not _stooq_api_key():
            raise MarketDataError(_stooq_history_key_error(symbol))

        errors = []
        for stooq_symbol in _stooq_symbol_candidates(symbol, exchange, currency, extra_symbols=extra_symbols):
            params = _stooq_daily_params({
                's': stooq_symbol,
                'd1': start_date.strftime('%Y%m%d'),
                'd2': end_date.strftime('%Y%m%d'),
                'i': 'd',
            })
            for daily_url in STOOQ_DAILY_URLS:
                rows = _read_csv_url(f'{daily_url}?{params}')
                valid_rows = [row for row in rows if row.get('Date') and row.get('Date') != 'No data']
                if not valid_rows:
                    errors.append(f'{stooq_symbol}: brak dziennych danych')
                    continue

                points = []
                for row in valid_rows:
                    point_date = _date(row.get('Date'))
                    close = _extract_stooq_price(row)
                    if point_date is None or close is None:
                        continue
                    points.append({
                        'date': point_date,
                        'open': _decimal(row.get('Open') or row.get('Otwarcie')),
                        'high': _decimal(row.get('High') or row.get('Najwyzszy') or row.get('Najwyższy')),
                        'low': _decimal(row.get('Low') or row.get('Najnizszy') or row.get('Najniższy')),
                        'close': close,
                        'volume': _integer(row.get('Volume') or row.get('Wolumen')),
                    })

                if points:
                    return sorted(points, key=lambda item: item['date'])
                errors.append(f'{stooq_symbol}: brak poprawnych cen ({_row_preview(valid_rows[-1])})')

        raise MarketDataError(f'Brak historycznych danych Stooq. Próby: {"; ".join(errors)}.')


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


def fetch_latest_market_price(symbol='', exchange='', currency='', isin='', price_symbol='', use_yahoo=True):
    resolved = None
    quote_symbol = price_symbol.strip().upper() if price_symbol else ''
    if isin and not quote_symbol:
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
    if use_yahoo and price_symbol:
        yahoo_client = YahooFinanceClient()
        yahoo_candidates = _yahoo_symbol_candidates(
            quote_symbol,
            exchange,
            currency,
            extra_symbols=(symbol,),
        )
        try:
            yahoo_quotes = yahoo_client.fetch_quotes(yahoo_candidates)
        except MarketDataError as exc:
            quote_errors.append(str(exc))
        else:
            for candidate in yahoo_candidates:
                yahoo_quote = yahoo_quotes.get(candidate)
                if yahoo_quote is None:
                    continue
                return {
                    'price': yahoo_quote['price'],
                    'source': yahoo_client.source_name,
                    'symbol': symbol or quote_symbol,
                    'price_symbol': yahoo_quote['symbol'],
                    'name': yahoo_quote.get('name', ''),
                    'isin': isin,
                    'exchange': yahoo_quote.get('exchange') or exchange,
                    'currency': yahoo_quote.get('currency') or currency,
                    'observed_at': yahoo_quote.get('observed_at'),
                    'history_points': yahoo_quote.get('points') or [],
                }
            quote_errors.append(
                f'Yahoo Finance nie znalazło żadnego z symboli: {", ".join(yahoo_candidates)}.'
            )

    if _is_warsaw_market(exchange, currency, quote_symbol):
        try:
            stooq_client = StooqClient()
            price = stooq_client.fetch_quote(quote_symbol, exchange, currency)
            source = stooq_client.source_name
        except MarketDataError as exc:
            price = None
            quote_errors.append(str(exc))
    else:
        price = None

    if price is None and not _is_warsaw_market(exchange, currency, quote_symbol):
        alpha_key = getattr(settings, 'ALPHA_VANTAGE_API_KEY', '')
        if alpha_key:
            try:
                alpha_client = AlphaVantageClient(alpha_key)
                price = alpha_client.fetch_quote(_alpha_vantage_symbol(quote_symbol))
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
        if resolved:
            prefix = f'OpenFIGI rozpoznało {symbol or quote_symbol}, ale nie pobrano ceny'
        else:
            prefix = f'Nie pobrano ceny dla symbolu {quote_symbol}'
        raise MarketDataError(f'{prefix}.{details}')

    return {
        'price': price,
        'source': source,
        'symbol': symbol,
        'price_symbol': quote_symbol if quote_symbol != symbol else '',
        'name': resolved.get('name', '') if resolved else '',
        'isin': resolved.get('isin', isin) if resolved else isin,
        'exchange': resolved.get('exchange', exchange) if resolved else exchange,
        'currency': resolved.get('currency', currency) if resolved else currency,
        'observed_at': None,
        'history_points': [],
    }


def _resolve_history_symbol(symbol='', exchange='', currency='', isin='', price_symbol=''):
    resolved = None
    resolution_error = ''
    clean_symbol = (symbol or '').strip().upper()
    quote_symbol = price_symbol.strip().upper() if price_symbol else clean_symbol

    if isin:
        try:
            resolved = resolve_instrument_by_isin(isin, exchange, currency)
        except MarketDataError as exc:
            resolution_error = str(exc)
        else:
            clean_symbol = resolved['symbol']
            exchange = resolved.get('exchange', exchange)
            currency = resolved.get('currency', currency)
            if not price_symbol:
                quote_symbol = clean_symbol

    if not quote_symbol:
        quote_symbol = clean_symbol

    if not quote_symbol:
        raise MarketDataError('Brak symbolu albo ISIN do pobrania historycznych cen.')

    return {
        'symbol': clean_symbol or quote_symbol,
        'quote_symbol': quote_symbol,
        'exchange': exchange,
        'currency': currency,
        'name': resolved.get('name', '') if resolved else '',
        'resolved': resolved,
        'resolution_error': resolution_error,
    }


def fetch_historical_market_prices(
    symbol='',
    exchange='',
    currency='',
    isin='',
    price_symbol='',
    name='',
    days=365,
    limit=800,
    start_date=None,
    end_date=None,
):
    resolved = _resolve_history_symbol(symbol, exchange, currency, isin, price_symbol)
    quote_symbol = resolved['quote_symbol']
    exchange = resolved['exchange']
    currency = resolved['currency']
    end_date = end_date or timezone.localdate()
    start_date = start_date or (end_date - timedelta(days=days))
    days = max((end_date - start_date).days, 1)
    errors = []
    stooq_extra_symbols = [value for value in (symbol, name, resolved.get('symbol'), resolved.get('name')) if value and value != quote_symbol]

    if price_symbol:
        yahoo_client = YahooFinanceClient()
        yahoo_candidates = _yahoo_symbol_candidates(
            quote_symbol,
            exchange,
            currency,
            extra_symbols=(symbol, resolved.get('symbol')),
        )
        for candidate in yahoo_candidates:
            try:
                points = yahoo_client.fetch_daily_history(candidate, start_date, end_date)
            except MarketDataError as exc:
                errors.append(str(exc))
                continue
            return {
                'points': points[-limit:],
                'source': yahoo_client.source_name,
                'symbol': resolved['symbol'],
                'price_symbol': candidate,
                'resolution_error': resolved['resolution_error'],
            }

    if _is_warsaw_market(exchange, currency, quote_symbol):
        try:
            stooq_client = StooqClient()
            points = stooq_client.fetch_daily_history(
                quote_symbol,
                start_date,
                end_date,
                exchange,
                currency,
                extra_symbols=stooq_extra_symbols,
            )
            return {
                'points': points[-limit:],
                'source': stooq_client.source_name,
                'symbol': resolved['symbol'],
                'price_symbol': quote_symbol if quote_symbol != resolved['symbol'] else '',
                'resolution_error': resolved['resolution_error'],
            }
        except MarketDataError as exc:
            errors.append(str(exc))

    if not _is_warsaw_market(exchange, currency, quote_symbol):
        alpha_key = getattr(settings, 'ALPHA_VANTAGE_API_KEY', '')
        if alpha_key:
            try:
                alpha_client = AlphaVantageClient(alpha_key)
                outputsize = 'full' if days > 140 else 'compact'
                points = alpha_client.fetch_daily_history(_alpha_vantage_symbol(quote_symbol), outputsize=outputsize)
                points = [point for point in points if start_date <= point['date'] <= end_date]
                return {
                    'points': points[-limit:],
                    'source': alpha_client.source_name,
                    'symbol': resolved['symbol'],
                    'price_symbol': quote_symbol if quote_symbol != resolved['symbol'] else '',
                    'resolution_error': resolved['resolution_error'],
                }
            except MarketDataError as exc:
                errors.append(str(exc))

    if price_symbol:
        try:
            stooq_client = StooqClient()
            points = stooq_client.fetch_daily_history(
                quote_symbol,
                start_date,
                end_date,
                exchange,
                currency,
                extra_symbols=stooq_extra_symbols,
            )
            return {
                'points': points[-limit:],
                'source': stooq_client.source_name,
                'symbol': resolved['symbol'],
                'price_symbol': quote_symbol if quote_symbol != resolved['symbol'] else '',
                'resolution_error': resolved['resolution_error'],
            }
        except MarketDataError as exc:
            errors.append(str(exc))

    details = f' Szczegóły: {"; ".join(errors)}' if errors else ''
    if resolved['resolution_error']:
        details = f' OpenFIGI: {resolved["resolution_error"]}.{details}'
    raise MarketDataError(
        f'Nie udało się pobrać historycznych cen dla symbolu {quote_symbol}. '
        f'Spróbuj uzupełnić symbol ceny przy instrumencie albo sprawdź propozycje Stooq po nazwie: '
        f'{_stooq_lookup_url(name or resolved.get("name") or quote_symbol)}.{details}'
    )


def fetch_latest_fx_rates_to_pln(currencies):
    requested_currencies = {
        (currency or '').strip().upper()
        for currency in currencies
        if (currency or '').strip()
    }
    rates = {'PLN': Decimal('1')}
    needed_currencies = requested_currencies - {'PLN'}
    if not needed_currencies:
        return {
            'rates': rates,
            'source': 'PLN',
            'table_date': timezone.localdate().isoformat(),
        }

    payload = _get_json(NBP_TABLE_A_URL)
    if not isinstance(payload, list) or not payload:
        raise MarketDataError('NBP zwrócił nieprawidłową tabelę kursów walut.')

    table = payload[0]
    for row in table.get('rates') or []:
        code = (row.get('code') or '').strip().upper()
        if code in needed_currencies:
            rate = _decimal(row.get('mid'))
            if rate is not None:
                rates[code] = rate

    missing = sorted(needed_currencies - set(rates))
    if missing:
        raise MarketDataError(f'NBP nie zwrócił kursu dla walut: {", ".join(missing)}.')

    source = f"NBP tabela {table.get('no')}" if table.get('no') else 'NBP'
    return {
        'rates': rates,
        'source': source,
        'table_date': table.get('effectiveDate') or '',
    }


def fetch_transaction_market_price(isin, trade_date=None, trade_time=None, exchange='', currency=''):
    resolved = resolve_instrument_by_isin(isin, exchange, currency)
    symbol = resolved['symbol']
    resolved_exchange = resolved.get('exchange', exchange)
    resolved_currency = resolved.get('currency', currency)

    if trade_date and _is_warsaw_market(resolved_exchange, resolved_currency, symbol):
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
        instrument.cash_operations.update(instrument=duplicate)

        # Imported position batches must survive symbol canonicalisation.  If
        # an account happened to contain both aliases in one batch, consolidate
        # them instead of violating the (account, instrument, as_of) constraint.
        for snapshot in instrument.position_snapshots.all():
            matching_snapshot = BrokeragePositionSnapshot.objects.filter(
                account=snapshot.account,
                instrument=duplicate,
                as_of=snapshot.as_of,
            ).first()
            if matching_snapshot is None:
                snapshot.instrument = duplicate
                snapshot.save(update_fields=['instrument'])
                continue

            matching_snapshot.quantity += snapshot.quantity
            matching_snapshot.market_value += snapshot.market_value
            if matching_snapshot.profit is not None and snapshot.profit is not None:
                matching_snapshot.profit += snapshot.profit
                cost = matching_snapshot.market_value - matching_snapshot.profit
                matching_snapshot.profit_percent = (
                    (matching_snapshot.profit / cost) * Decimal('100') if cost else None
                )
            else:
                matching_snapshot.profit = None
                matching_snapshot.profit_percent = None
            if snapshot.current_price is not None:
                matching_snapshot.current_price = snapshot.current_price
            if snapshot.source and snapshot.source not in matching_snapshot.source:
                matching_snapshot.source = ', '.join(filter(None, [matching_snapshot.source, snapshot.source]))
            matching_snapshot.save(update_fields=[
                'quantity', 'market_value', 'current_price', 'profit', 'profit_percent', 'source',
            ])
            snapshot.delete()

        for price_snapshot in instrument.price_snapshots.all():
            BrokeragePriceSnapshot.objects.update_or_create(
                instrument=duplicate,
                observed_at=price_snapshot.observed_at,
                defaults={
                    'price': price_snapshot.price,
                    'source': price_snapshot.source,
                },
            )
            price_snapshot.delete()

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


def _active_instrument_ids(user, accounts):
    active_ids = set()
    for account in accounts:
        latest_as_of = (
            BrokeragePositionSnapshot.objects
            .filter(account=account)
            .aggregate(latest=Max('as_of'))['latest']
        )
        if latest_as_of is not None:
            active_ids.update(
                BrokeragePositionSnapshot.objects
                .filter(account=account, as_of=latest_as_of, quantity__gt=0)
                .values_list('instrument_id', flat=True)
            )
            continue

        account_instruments = (
            BrokerageInstrument.objects
            .filter(user=user, transactions__account=account)
            .distinct()
        )
        for instrument in account_instruments:
            if get_quantity(account, instrument) > 0:
                active_ids.add(instrument.id)
    return active_ids


def _save_price_snapshot(instrument, observed_at, price, source):
    observed_at = observed_at or timezone.now()
    observed_date = timezone.localtime(observed_at).date()
    existing = (
        BrokeragePriceSnapshot.objects
        .filter(
            instrument=instrument,
            source=source,
            observed_at__date=observed_date,
        )
        .order_by('-observed_at', '-id')
        .first()
    )
    if existing is None:
        BrokeragePriceSnapshot.objects.create(
            instrument=instrument,
            observed_at=observed_at,
            price=price,
            source=source,
        )
        return
    existing.observed_at = observed_at
    existing.price = price
    existing.save(update_fields=['observed_at', 'price'])


def _upsert_daily_price(instrument, point, *, currency, provider_symbol, source, today):
    adjusted_close = point.get('adjusted_close')
    defaults = {
        'open': point.get('open'),
        'high': point.get('high'),
        'low': point.get('low'),
        'close': point['close'],
        'volume': point.get('volume'),
        'currency': currency,
        'provider_symbol': provider_symbol,
        'source': source,
        'is_final': point['date'] < today,
    }
    existing = BrokerageDailyPrice.objects.filter(
        instrument=instrument,
        trading_date=point['date'],
    ).first()
    if existing is None:
        defaults['adjusted_close'] = adjusted_close or point['close']
        BrokerageDailyPrice.objects.create(
            instrument=instrument,
            trading_date=point['date'],
            **defaults,
        )
        return True

    if adjusted_close is not None:
        defaults['adjusted_close'] = adjusted_close
    elif existing.adjusted_close is None:
        defaults['adjusted_close'] = point['close']
    for field, value in defaults.items():
        setattr(existing, field, value)
    existing.save(update_fields=[*defaults.keys(), 'updated_at'])
    return False


def _first_purchase_dates(user, instrument_ids=None):
    purchases = BrokerageTransaction.objects.filter(
        account__user=user,
        transaction_type=BrokerageTransaction.BUY,
    )
    if instrument_ids is not None:
        purchases = purchases.filter(instrument_id__in=instrument_ids)
    return {
        row['instrument_id']: row['first_purchase']
        for row in (
            purchases
            .values('instrument_id')
            .annotate(first_purchase=Min('trade_date'))
        )
    }


def sync_price_history_for_user(user, *, instrument_ids=None, force=False):
    """Persist daily OHLC history once, then only fetch uncovered dates."""
    first_purchase_dates = _first_purchase_dates(user, instrument_ids)
    instruments = {
        instrument.id: instrument
        for instrument in BrokerageInstrument.objects.filter(
            user=user,
            id__in=first_purchase_dates,
        )
    }
    now = timezone.now()
    today = timezone.localdate(now)
    retry_after = now - timedelta(hours=6)
    result = {
        'instruments_synced': 0,
        'points_created': 0,
        'points_updated': 0,
        'already_current': 0,
        'failed': [],
    }
    yahoo_client = YahooFinanceClient()

    for instrument_id, first_purchase_date in first_purchase_dates.items():
        instrument = instruments.get(instrument_id)
        if instrument is None:
            continue

        fully_covered = (
            instrument.history_synced_from is not None
            and instrument.history_synced_from <= first_purchase_date
            and instrument.history_synced_through is not None
            and instrument.history_synced_through >= today
        )
        if fully_covered and not force:
            result['already_current'] += 1
            continue
        if (
            instrument.history_sync_error
            and instrument.history_sync_at
            and instrument.history_sync_at >= retry_after
            and not force
        ):
            result['failed'].append(f'{instrument.ticker}: {instrument.history_sync_error}')
            continue

        if force or instrument.history_synced_from is None:
            missing_ranges = [(first_purchase_date, today)]
        else:
            missing_ranges = []
            if instrument.history_synced_from > first_purchase_date:
                missing_ranges.append(
                    (first_purchase_date, instrument.history_synced_from - timedelta(days=1))
                )
            if instrument.history_synced_through is None:
                missing_ranges.append((instrument.history_synced_from, today))
            elif instrument.history_synced_through < today:
                tail_start = instrument.history_synced_through + timedelta(days=1)
                if instrument.daily_prices.filter(
                    trading_date=instrument.history_synced_through,
                    is_final=False,
                ).exists():
                    # Re-read the last in-progress session once so its final
                    # OHLC and adjusted close are not left provisional forever.
                    tail_start = instrument.history_synced_through
                missing_ranges.append(
                    (tail_start, today)
                )

        missing_ranges = [
            (start_date, end_date)
            for start_date, end_date in missing_ranges
            if start_date <= end_date
        ]
        if not missing_ranges:
            result['already_current'] += 1
            continue

        candidates = _yahoo_symbol_candidates(
            instrument.price_symbol or instrument.ticker,
            instrument.exchange,
            instrument.currency,
            extra_symbols=(instrument.ticker,),
        )
        # The exchange-qualified candidate is authoritative. A bare fallback
        # can be a completely different security (for example ASB.WA vs the
        # US ticker ASB), which is worse than an explicit synchronization
        # error. Known same-instrument exceptions belong in the alias table.
        candidates = candidates[:1]
        if not candidates:
            error = 'Brak symbolu dostawcy dla historii cen.'
            instrument.history_sync_at = now
            instrument.history_sync_error = error
            instrument.save(update_fields=['history_sync_at', 'history_sync_error'])
            result['failed'].append(f'{instrument.ticker}: {error}')
            continue

        provider_symbol = ''
        points = None
        candidate_errors = []
        for candidate in candidates:
            candidate_points = []
            try:
                for start_date, end_date in missing_ranges:
                    candidate_points.extend(
                        yahoo_client.fetch_daily_history(
                            candidate,
                            start_date,
                            end_date,
                            allow_empty=True,
                        )
                    )
            except MarketDataError as exc:
                candidate_errors.append(f'{candidate}: {exc}')
                continue

            if candidate_points or instrument.history_synced_from is not None:
                provider_symbol = candidate
                points = candidate_points
                break
            candidate_errors.append(f'{candidate}: brak historii cen')

        if points is None:
            error = '; '.join(candidate_errors)[:500] or 'Yahoo Finance nie zwróciło historii.'
            instrument.history_sync_at = now
            instrument.history_sync_error = error
            instrument.save(update_fields=['history_sync_at', 'history_sync_error'])
            result['failed'].append(f'{instrument.ticker}: {error}')
            continue

        with transaction.atomic():
            for point in points:
                created = _upsert_daily_price(
                    instrument,
                    point,
                    currency=instrument.currency,
                    provider_symbol=provider_symbol,
                    source=yahoo_client.source_name,
                    today=today,
                )
                if created:
                    result['points_created'] += 1
                else:
                    result['points_updated'] += 1

            instrument.price_symbol = provider_symbol
            instrument.history_synced_from = (
                min(instrument.history_synced_from, first_purchase_date)
                if instrument.history_synced_from else first_purchase_date
            )
            instrument.history_synced_through = today
            instrument.history_sync_at = now
            instrument.history_sync_error = ''
            instrument.save(update_fields=[
                'price_symbol',
                'history_synced_from',
                'history_synced_through',
                'history_sync_at',
                'history_sync_error',
            ])
        result['instruments_synced'] += 1

    return result


def stored_daily_price_history(instrument, start_date, end_date, *, final_only=False):
    prices = instrument.daily_prices.filter(
        trading_date__gte=start_date,
        trading_date__lte=end_date,
    )
    if final_only:
        prices = prices.filter(is_final=True)
    return [
        {
            'date': price.trading_date,
            'open': price.open,
            'high': price.high,
            'low': price.low,
            'close': price.close,
            'adjusted_close': price.adjusted_close or price.close,
            'volume': price.volume,
        }
        for price in prices.order_by('trading_date')
    ]


def refresh_market_data_for_user(user, *, refresh_dividends=False):
    alpha_client = None
    alpha_key = getattr(settings, 'ALPHA_VANTAGE_API_KEY', '')
    if refresh_dividends and alpha_key:
        alpha_client = AlphaVantageClient(alpha_key)
    updated_quotes = 0
    updated_dividends = 0
    merged_instruments = 0
    failed_quotes = []
    failed_dividends = []
    sources = set()
    today = timezone.localdate()

    all_instruments = list(BrokerageInstrument.objects.filter(user=user))
    accounts = list(BrokerageAccount.objects.filter(user=user))
    active_ids = _active_instrument_ids(user, accounts)
    instruments = (
        [instrument for instrument in all_instruments if instrument.id in active_ids]
        if active_ids else all_instruments
    )
    inactive_skipped = len(all_instruments) - len(instruments)

    yahoo_client = YahooFinanceClient()
    yahoo_candidates = {}
    bulk_symbols = []
    for instrument in instruments:
        if not instrument.price_symbol:
            continue
        candidates = _yahoo_symbol_candidates(
            instrument.price_symbol,
            instrument.exchange,
            instrument.currency,
            extra_symbols=(instrument.ticker,),
        )
        # XTB suffix translation is deterministic. Sending provider-invalid
        # aliases (for example ASB next to ASB.WA) can invalidate a bulk call.
        candidates = candidates[:1]
        yahoo_candidates[instrument.id] = candidates
        for candidate in candidates:
            if candidate not in bulk_symbols:
                bulk_symbols.append(candidate)

    yahoo_quotes = {}
    yahoo_bulk_error = None
    if bulk_symbols:
        try:
            yahoo_quotes = yahoo_client.fetch_quotes(bulk_symbols)
        except MarketDataError as exc:
            yahoo_bulk_error = exc

    for instrument in instruments:
        market_data = None
        candidates = yahoo_candidates.get(instrument.id, [])
        if yahoo_bulk_error is None:
            for candidate in candidates:
                yahoo_quote = yahoo_quotes.get(candidate)
                if yahoo_quote is None:
                    continue
                market_data = {
                    'price': yahoo_quote['price'],
                    'source': yahoo_client.source_name,
                    'symbol': instrument.ticker,
                    'price_symbol': yahoo_quote['symbol'],
                    'name': yahoo_quote.get('name', ''),
                    'isin': instrument.isin,
                    'exchange': yahoo_quote.get('exchange') or instrument.exchange,
                    'currency': yahoo_quote.get('currency') or instrument.currency,
                    'observed_at': yahoo_quote.get('observed_at'),
                    'history_points': yahoo_quote.get('points') or [],
                }
                break

        individual_errors = []
        if market_data is None and candidates and yahoo_bulk_error is None:
            # A partial Spark response should not make one otherwise valid
            # position stale. Retry only that instrument through Chart. A full
            # bulk outage is returned immediately to keep the web request short.
            for candidate in candidates:
                try:
                    yahoo_quote = yahoo_client.fetch_quote(candidate)
                except MarketDataError as exc:
                    individual_errors.append(str(exc))
                    continue
                market_data = {
                    'price': yahoo_quote['price'],
                    'source': yahoo_client.source_name,
                    'symbol': instrument.ticker,
                    'price_symbol': yahoo_quote['symbol'],
                    'name': yahoo_quote.get('name', ''),
                    'isin': instrument.isin,
                    'exchange': yahoo_quote.get('exchange') or instrument.exchange,
                    'currency': yahoo_quote.get('currency') or instrument.currency,
                    'observed_at': yahoo_quote.get('observed_at'),
                    'history_points': yahoo_quote.get('points') or [],
                }
                break

        if market_data is None and not candidates:
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
        elif market_data is None:
            error = yahoo_bulk_error or (individual_errors[-1] if individual_errors else None)
            failed_quotes.append(
                f'{instrument.ticker}: {error or "Yahoo Finance nie zwróciło ceny."}'
            )
            continue

        if market_data['source'] != yahoo_client.source_name:
            instrument, merged = merge_duplicate_instrument(instrument, market_data['symbol'])
            if merged:
                merged_instruments += 1

        quote_observed_at = market_data.get('observed_at') or timezone.now()
        if market_data['source'] != yahoo_client.source_name:
            instrument.ticker = market_data['symbol']
        instrument.last_price = market_data['price']
        instrument.last_price_at = quote_observed_at
        instrument.market_data_source = market_data['source']
        if market_data.get('exchange'):
            instrument.exchange = market_data['exchange']
        if market_data.get('currency'):
            instrument.currency = market_data['currency']
        if market_data.get('price_symbol'):
            instrument.price_symbol = market_data['price_symbol']
        instrument.save(update_fields=['ticker', 'price_symbol', 'last_price', 'last_price_at', 'market_data_source', 'exchange', 'currency'])

        history_points = market_data.get('history_points') or []
        for point in history_points:
            _save_price_snapshot(
                instrument,
                point.get('observed_at'),
                point['close'],
                market_data['source'],
            )
            _upsert_daily_price(
                instrument,
                point,
                currency=market_data.get('currency') or instrument.currency,
                provider_symbol=market_data.get('price_symbol') or instrument.price_symbol or instrument.ticker,
                source=market_data['source'],
                today=today,
            )
        if not history_points:
            _save_price_snapshot(
                instrument,
                quote_observed_at,
                market_data['price'],
                market_data['source'],
            )
        # Spark intentionally supplies only lightweight raw closes.  Do not
        # advance ``history_synced_through`` here: the incremental Chart sync
        # still needs to replace these rows with adjusted closes for reliable
        # split/dividend-aware return statistics.
        updated_quotes += 1
        sources.add(market_data['source'])

        if alpha_client is None:
            continue

        try:
            dividend_items = alpha_client.fetch_dividends(_alpha_vantage_symbol(instrument.price_symbol or instrument.ticker))
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
        'inactive_skipped': inactive_skipped,
        'dividends_checked': refresh_dividends,
        'source': ', '.join(sorted(sources)) or 'brak źródła',
    }
