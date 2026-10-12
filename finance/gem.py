"""Strategia GEM (Global Equities Momentum) w widoku kont maklerskich.

Zasada (wariant z ETF-ami UCITS notowanymi w Londynie, w USD):
- co miesiąc porównujemy stopę zwrotu z ostatnich 12 miesięcy funduszy
  akcyjnych (CSPX, IWDA, EIMI) i funduszu bonów skarbowych USA (IB01),
- trzymamy ten z najwyższym wynikiem; jeśli żaden fundusz akcyjny nie bije
  IB01 (momentum absolutne), trzymamy IB01.

Oficjalny sygnał liczymy na ostatnią sesję poprzedniego miesiąca (zmiana
pozycji raz w miesiącu). Wynik „na dziś” pokazuje, kto prowadzi teraz.

Wszystkie fundusze są akumulujące i notowane w USD, więc porównujemy ceny
skorygowane (adjusted close) w tej samej walucie, bez przeliczania na PLN.

Notowania są w StrategyDailyPrice. Pobiera je ``sync_gem_prices`` - z crona
(``manage.py sync_price_history``), przyciskiem w panelu albo automatycznie
po wejściu na stronę, jeśli od zamknięcia ostatniej sesji nie było udanego
pobrania (finance.views.GemRefreshView).
"""
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date as date_type, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .market_data import MarketDataError, StooqClient, YahooFinanceClient, _stooq_api_key
from .models import StrategyDailyPrice


@dataclass(frozen=True)
class GemAsset:
    symbol: str          # symbol w Yahoo Finance
    ticker: str          # jak na liście pozycji (XTB: CSPX.UK)
    name: str
    role: str
    stooq_symbol: str
    is_safe: bool = False
    currency: str = 'USD'


GEM_ASSETS = (
    GemAsset('CSPX.L', 'CSPX', 'iShares Core S&P 500', 'Akcje USA', 'cspx.uk'),
    GemAsset('IWDA.L', 'IWDA', 'iShares Core MSCI World', 'Akcje krajów rozwiniętych', 'iwda.uk'),
    GemAsset('EIMI.L', 'EIMI', 'iShares Core MSCI EM IMI', 'Rynki wschodzące', 'eimi.uk'),
    GemAsset('IB01.L', 'IB01', 'iShares $ Treasury Bond 0-1yr', 'Bony skarbowe USA', 'ib01.uk', is_safe=True),
)
GEM_SYMBOLS = tuple(asset.symbol for asset in GEM_ASSETS)
LOOKBACK_MONTHS = 12
EXTRA_PERIODS = (1, 3, 6)

# Ile historii trzymamy: 12 miesięcy przed końcem poprzedniego miesiąca + zapas.
HISTORY_DAYS = 430
# Przy kolejnym pobraniu dociągamy też kilka ostatnich sesji (poprawki dostawcy).
REFETCH_DAYS = 7
# Cena sprzed 12 miesięcy: pierwsza sesja od tej daty, najwyżej tyle dni później.
BASE_TOLERANCE_DAYS = 7

# Sesja w Londynie kończy się o 16:30 czasu UK (17:30 w Polsce); dajemy zapas.
SESSION_CLOSED_AT = time(18, 30)
AUTO_RETRY_AFTER = timedelta(minutes=30)
CACHE_KEY = 'finance:gem:sync-status'
CACHE_TIMEOUT = 14 * 24 * 3600

HUNDRED = Decimal('100')
CENT = Decimal('0.01')


# ---------------------------------------------------------------------------
# Pobieranie notowań
# ---------------------------------------------------------------------------

def _fetch_points(asset, start_date, end_date):
    errors = []
    try:
        points = YahooFinanceClient().fetch_daily_history(asset.symbol, start_date, end_date)
        return points, YahooFinanceClient.source_name
    except MarketDataError as exc:
        errors.append(str(exc))
    if _stooq_api_key():
        try:
            points = StooqClient().fetch_daily_history(asset.stooq_symbol, start_date, end_date)
            return points, StooqClient.source_name
        except MarketDataError as exc:
            errors.append(str(exc))
    raise MarketDataError('; '.join(errors) or 'brak danych')


def sync_status():
    return cache.get(CACHE_KEY) or {}


def sync_gem_prices(*, today=None, force=False):
    """Dociąga brakujące sesje dla ETF-ów GEM. Zwraca podsumowanie.

    ``force`` pobiera pełne ~14 miesięcy od nowa (np. po zmianie dostawcy).
    """
    today = today or timezone.localdate()
    oldest = today - timedelta(days=HISTORY_DAYS)
    result = {'created': 0, 'updated': 0, 'synced': 0, 'failed': []}
    if not settings.GEM_PRICES_ENABLED:
        return result

    for asset in GEM_ASSETS:
        last_date = (
            StrategyDailyPrice.objects.filter(symbol=asset.symbol)
            .order_by('-trading_date').values_list('trading_date', flat=True).first()
        )
        start = oldest if (force or last_date is None or last_date < oldest) else last_date - timedelta(days=REFETCH_DAYS)
        try:
            points, source = _fetch_points(asset, start, today)
        except MarketDataError as exc:
            result['failed'].append(f'{asset.ticker}: {exc}')
            continue

        with transaction.atomic():
            existing = {
                price.trading_date: price
                for price in StrategyDailyPrice.objects.select_for_update().filter(
                    symbol=asset.symbol, trading_date__gte=start,
                )
            }
            for point in points:
                close = point.get('close')
                if close is None or close <= 0:
                    continue
                adjusted = point.get('adjusted_close')
                price = existing.get(point['date'])
                if price is None:
                    StrategyDailyPrice.objects.create(
                        symbol=asset.symbol, trading_date=point['date'], close=close,
                        adjusted_close=adjusted, currency=asset.currency, source=source,
                    )
                    result['created'] += 1
                elif (price.close, price.adjusted_close, price.source) != (close, adjusted, source):
                    price.close, price.adjusted_close, price.source = close, adjusted, source
                    price.save(update_fields=['close', 'adjusted_close', 'source', 'updated_at'])
                    result['updated'] += 1
            StrategyDailyPrice.objects.filter(
                symbol=asset.symbol, trading_date__lt=oldest - timedelta(days=31),
            ).delete()
        result['synced'] += 1

    now = timezone.now()
    status = sync_status()
    status['attempt_at'] = now
    if result['synced']:
        status['success_at'] = now
    status['failed'] = result['failed']
    cache.set(CACHE_KEY, status, CACHE_TIMEOUT)
    return result


def last_session_close(now=None):
    """Chwila zamknięcia ostatniej sesji w Londynie (bez świąt), czas lokalny."""
    now = timezone.localtime(now or timezone.now())
    day = now.date()
    if now.time() < SESSION_CLOSED_AT:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return timezone.make_aware(datetime.combine(day, SESSION_CLOSED_AT), timezone.get_current_timezone())


def needs_refresh(now=None):
    """Czy strona ma sama dociągnąć notowania.

    Tak, jeśli od zamknięcia ostatniej sesji nie było udanego pobrania
    i od ostatniej próby minęło co najmniej AUTO_RETRY_AFTER (żeby awaria
    dostawcy nie oznaczała próby przy każdym wejściu na stronę).
    Po udanym pobraniu w dzień wolny od sesji nie pytamy ponownie.
    """
    if not settings.GEM_PRICES_ENABLED:
        return False
    now = now or timezone.now()
    status = sync_status()
    success_at = status.get('success_at')
    if success_at and success_at >= last_session_close(now):
        return False
    attempt_at = status.get('attempt_at')
    return not (attempt_at and now - attempt_at < AUTO_RETRY_AFTER)


# ---------------------------------------------------------------------------
# Sygnał
# ---------------------------------------------------------------------------

def add_months(day, months):
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    # Ostatni dzień miesiąca, jeśli dnia brakuje (31.03 - 1 mies. = 28/29.02).
    for candidate in (day.day, 30, 29, 28):
        try:
            return date_type(year, month, candidate)
        except ValueError:
            continue
    raise ValueError(day)


class PriceSeries:
    def __init__(self, rows):
        self.dates = [row[0] for row in rows]
        self.values = [row[1] for row in rows]

    def __bool__(self):
        return bool(self.dates)

    def index_on_or_before(self, day):
        index = bisect_right(self.dates, day) - 1
        return index if index >= 0 else None

    def index_on_or_after(self, day):
        index = bisect_left(self.dates, day)
        return index if index < len(self.dates) else None

    def value_on_or_before(self, day):
        """Cena z ostatniej sesji ≤ ``day`` (jak ``ffill`` w pandas)."""
        index = self.index_on_or_before(day)
        return self.values[index] if index is not None else None

    def period_return(self, end_day, months):
        """Stopa zwrotu za ``months`` miesięcy kończąca się na ostatniej sesji ≤ ``end_day``.

        Cena bazowa to pierwsza sesja od dnia ``base_target`` - tak jak
        ``data.iloc[0]`` w skrypcie z yfinance.
        """
        end_index = self.index_on_or_before(end_day)
        if end_index is None:
            return None
        target = base_target(self.dates[end_index], months)
        base_index = self.index_on_or_after(target)
        if (
            base_index is None or base_index >= end_index
            or (self.dates[base_index] - target).days > BASE_TOLERANCE_DAYS
        ):
            return None
        base = self.values[base_index]
        if not base:
            return None
        return self.values[end_index] / base - 1


def base_target(end_date, months):
    """Dzień, od którego liczymy okno: jak skrypt uruchomiony dzień po sesji.

    Skrypt z yfinance (``start = dziś - 12 mies.``, ``end = dziś`` bez dzisiejszej
    sesji) uruchomiony 1.10 liczy od pierwszej sesji od 1.10 rok wcześniej do
    sesji 30.09 - tutaj tak samo.
    """
    return add_months(end_date + timedelta(days=1), -months)


def _chart(series, as_of):
    """Stopa zwrotu od początku okna 12 mies. dzień po dniu - jak wykres w skrypcie.

    Wspólne dni wszystkich funduszy, brakująca sesja uzupełniona poprzednią
    ceną (``ffill``). Zwraca None, gdy któryś fundusz nie ma pełnej historii.
    """
    if as_of is None or not all(series.values()):
        return None
    target = base_target(as_of, LOOKBACK_MONTHS)
    starts = [item.index_on_or_after(target) for item in series.values()]
    if any(index is None for index in starts):
        return None
    start = max(item.dates[index] for item, index in zip(series.values(), starts))
    if (start - target).days > BASE_TOLERANCE_DAYS or start >= as_of:
        return None
    days = sorted({day for item in series.values() for day in item.dates if start <= day <= as_of})
    datasets = []
    for asset in GEM_ASSETS:
        item = series[asset.symbol]
        base = item.value_on_or_before(start)
        datasets.append({
            'ticker': asset.ticker,
            'name': asset.name,
            'safe': asset.is_safe,
            # Tak samo zaokrąglone jak wyniki w rankingu (_percent), żeby końce linii zgadzały się z liczbami obok.
            'values': [float(_percent(item.value_on_or_before(day) / base - 1)) for day in days],
        })
    # Tabela pod wykresem: ostatnia sesja każdego miesiąca i dzień końcowy.
    month_rows = [index for index, day in enumerate(days) if index + 1 == len(days) or days[index + 1].month != day.month]
    return {
        'labels': [day.isoformat() for day in days],
        'datasets': datasets,
        'start': start,
        'end': as_of,
        'table_rows': month_rows,
    }


def _percent(value):
    return None if value is None else (value * HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)


def _base_ticker(value):
    return re.split(r'[.\s]', (value or '').strip().upper(), maxsplit=1)[0]


def _bars(rows):
    """Położenie słupków 12 mies. na wspólnej osi z zerem (w procentach szerokości)."""
    values = [row['r12'] for row in rows if row['r12'] is not None]
    if not values:
        return None
    low, high = min(min(values), Decimal('0')), max(max(values), Decimal('0'))
    span = (high - low) or Decimal('1')
    zero = (-low / span * HUNDRED).quantize(CENT)
    for row in rows:
        value = row['r12']
        if value is None:
            row['bar'] = None
            continue
        start = ((min(value, Decimal('0')) - low) / span * HUNDRED).quantize(CENT)
        width = (abs(value) / span * HUNDRED).quantize(CENT)
        row['bar'] = {'left': start, 'width': max(width, Decimal('0.6')), 'negative': value < 0}
    return zero


def _holdings(positions):
    """Pozycje w ETF-ach GEM: {ticker: [pozycje]} (po symbolu bez giełdy)."""
    tickers = {asset.ticker for asset in GEM_ASSETS}
    held = {}
    for position in positions or ():
        instrument = position.get('instrument')
        if instrument is None:
            continue
        for value in (instrument.ticker, instrument.price_symbol):
            base = _base_ticker(value)
            if base in tickers:
                held.setdefault(base, []).append(position)
                break
    return held


def _winner(rows, key):
    candidates = [row for row in rows if row[key] is not None]
    if len(candidates) != len(rows):
        return None
    return max(candidates, key=lambda row: row[key])


def build_gem_summary(positions=None, *, today=None):
    """Dane do panelu GEM. ``positions`` - pozycje z build_portfolio_summary."""
    today = today or timezone.localdate()
    rows_by_symbol = {symbol: [] for symbol in GEM_SYMBOLS}
    fetched_at = None
    for symbol, day, close, adjusted, updated_at in (
        StrategyDailyPrice.objects.filter(symbol__in=GEM_SYMBOLS, trading_date__lte=today)
        .order_by('symbol', 'trading_date')
        .values_list('symbol', 'trading_date', 'close', 'adjusted_close', 'updated_at')
    ):
        rows_by_symbol[symbol].append((day, adjusted if adjusted is not None else close))
        fetched_at = updated_at if fetched_at is None or updated_at > fetched_at else fetched_at

    series = {symbol: PriceSeries(rows) for symbol, rows in rows_by_symbol.items()}
    latest_dates = [item.dates[-1] for item in series.values() if item]
    as_of = min(latest_dates) if latest_dates else None
    # Oficjalny sygnał: ostatnia sesja poprzedniego miesiąca (względem dziś,
    # więc 1. dnia miesiąca rano liczy się już zamknięcie z wczoraj).
    signal_date = today.replace(day=1) - timedelta(days=1)
    signal_sessions = [
        item.dates[index] for item in series.values()
        if item and (index := item.index_on_or_before(signal_date)) is not None
    ]
    signal_session = min(signal_sessions) if len(signal_sessions) == len(series) else None

    rows = []
    for asset in GEM_ASSETS:
        item = series[asset.symbol]
        returns = {months: (item.period_return(as_of, months) if item else None) for months in EXTRA_PERIODS}
        r12 = item.period_return(as_of, LOOKBACK_MONTHS) if item else None
        signal_r12 = item.period_return(signal_session, LOOKBACK_MONTHS) if item and signal_session else None
        last_index = item.index_on_or_before(as_of) if item and as_of else None
        rows.append({
            'asset': asset,
            'price': item.values[last_index] if last_index is not None else None,
            'price_date': item.dates[last_index] if last_index is not None else None,
            'r12': _percent(r12),
            'signal_r12': _percent(signal_r12),
            'r1': _percent(returns[1]),
            'r3': _percent(returns[3]),
            'r6': _percent(returns[6]),
            'has_data': bool(item),
        })

    winner = _winner(rows, 'r12')
    signal_winner = _winner(rows, 'signal_r12')
    zero = _bars(rows)
    safe_row = next(row for row in rows if row['asset'].is_safe)
    hurdle = None
    if zero is not None and safe_row.get('bar'):
        bar = safe_row['bar']
        hurdle = bar['left'] if bar['negative'] else bar['left'] + bar['width']

    ranked = sorted(rows, key=lambda row: (row['r12'] is None, -(row['r12'] or 0)))
    for position, row in enumerate(ranked, start=1):
        row['rank'] = position if row['r12'] is not None else None
        row['is_winner'] = winner is not None and row is winner
        row['is_signal'] = signal_winner is not None and row is signal_winner
        row['beats_hurdle'] = (
            not row['asset'].is_safe and row['r12'] is not None and safe_row['r12'] is not None
            and row['r12'] > safe_row['r12']
        )

    margin = runner_up = None
    if winner is not None:
        others = [row for row in ranked if row is not winner and row['r12'] is not None]
        if others:
            runner_up = others[0]
            margin = winner['r12'] - runner_up['r12']
    # Momentum absolutne na koniec miesiąca: o ile zwycięzca bije bony (IB01)
    # albo - w trybie ochronnym - o ile najlepsze akcje są od nich słabsze.
    signal_vs_safe = None
    if signal_winner is not None:
        best_equity = max(
            (row['signal_r12'] for row in rows if not row['asset'].is_safe and row['signal_r12'] is not None),
            default=None,
        )
        if best_equity is not None and safe_row['signal_r12'] is not None:
            signal_vs_safe = best_equity - safe_row['signal_r12']

    held = _holdings(positions)
    target = signal_winner or winner
    if not held:
        holding_status = 'none'
    elif target is None:
        holding_status = 'unknown'
    elif set(held) == {target['asset'].ticker}:
        holding_status = 'match'
    elif target['asset'].ticker in held:
        holding_status = 'mixed'
    else:
        holding_status = 'switch'

    missing = [row['asset'].ticker for row in rows if row['r12'] is None]
    status = sync_status()
    return {
        'assets': GEM_ASSETS,
        'rows': ranked,
        'winner': winner,
        'signal_winner': signal_winner,
        'signal_changed': bool(winner and signal_winner and winner is not signal_winner),
        'safe_mode': bool(winner and winner['asset'].is_safe),
        'signal_safe_mode': bool(signal_winner and signal_winner['asset'].is_safe),
        'margin': margin,
        'runner_up': runner_up,
        'signal_vs_safe': signal_vs_safe,
        'zero': zero,
        'hurdle': hurdle,
        'safe_row': safe_row,
        'as_of': as_of,
        'signal_date': signal_session,
        'fetched_at': fetched_at,
        'missing': missing,
        'is_empty': not latest_dates,
        'is_complete': not missing,
        'held_tickers': sorted(held),
        'holding_status': holding_status,
        'target': target,
        'lookback': LOOKBACK_MONTHS,
        'last_error': '; '.join(status.get('failed') or []),
        'auto_refresh': needs_refresh(),
        'chart': _chart(series, as_of),
    }
