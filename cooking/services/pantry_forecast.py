"""Prognoza zużycia i zakupów w spiżarni.

Jak to liczymy
==============

1. **W sztukach.** Produkt ze skanera (jogurt 400 g) zapisuje stan w gramach,
   ale kupuje się go w opakowaniach. Prognoza zamienia wszystkie ruchy na
   opakowania (400 g = 1 szt.), liczy w nich tempo i w nich podaje zakup.
   Produkty w sztukach liczą się w sztukach, a ważone bez znanego opakowania -
   w swojej jednostce.

2. **Nauka.** Dopóki produkt nie ma co najmniej ``LEARNING_MIN_DAYS`` dni
   historii i ``LEARNING_MIN_EVENTS`` dni ze zużyciem, niczego nie
   ekstrapolujemy. Na listę trafia dopiero wtedy, gdy stan spadnie do progu
   minimalnego, i to w ilości równej temu progowi (co najmniej jedno
   opakowanie). Dwa jogurty zjedzone w dwa dni to za mało, żeby wyliczyć
   tygodniowe zakupy - stary model robił z tego 9 opakowań.

3. **Tempo.** Po nauce: średnie zużycie na dzień, w którym produkt był w domu
   (dni bez zapasu nie są "zerowym zużyciem" - nie było czego zużyć), z 180
   dni, nowsze dni ważą więcej (półokres ``PACE_HALF_LIFE_DAYS``).
   Pojedyncze skoki (impreza) są przycinane, a produkty używane co jakiś czas
   mają osobny model (prawdopodobieństwo użycia × wielkość użycia).

4. **Ile kupić.** Polityka okresowego przeglądu (R, S), standard w
   zaopatrzeniu sklepów: przy każdych zakupach uzupełnij zapas do poziomu
   S = minimum + zużycie do kolejnych zakupów (R dni + "kup z wyprzedzeniem")
   + zapas bezpieczeństwa. R to rytm zakupów domu (mediana odstępów między
   dniami zakupów), a zapas bezpieczeństwa wynika z tego, jak bardzo
   zużycie w takich okresach waha się w historii.

5. **Bez skoków.** Propozycja nie przekracza 1,5 × największego zakupu
   tego produktu z ostatnich 90 dni. Jeśli dom zaczyna jeść więcej, ilość
   rośnie stopniowo z tygodnia na tydzień (2 -> 3 -> 5 -> 8) zamiast skakać.

Parametry dobrane na symulacji gospodarstwa (jogurty, mleko, mąka, papier,
ketchup, piwo tylko w soboty, nagły wzrost zużycia): przy zakupach co tydzień
nowy model pokrywa 98-100% zużycia, trzyma mniej zapasu niż stary i nie
proponuje absurdalnych ilości - stary w fazie nauki proponował do 22 opakowań.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from math import ceil, exp, log, log1p, sqrt
from statistics import median, pstdev
from typing import Iterable, Sequence

from django.utils import timezone


TWO_PLACES = Decimal('0.01')
MAX_FORECAST_DAYS = 3650

HISTORY_DAYS = 180            # ile historii bierze prognoza
LEARNING_MIN_DAYS = 28        # nauka: co najmniej 4 tygodnie obserwacji...
LEARNING_MIN_EVENTS = 6       # ...i 6 dni, w których coś zużyto
PACE_HALF_LIFE_DAYS = 30      # waga dnia sprzed 30 dni = połowa dzisiejszej
PACE_PRIOR_DAYS = 14          # tempo "ciągnięte" do średniej z całej historii jak 14 dni danych
DEFAULT_REVIEW_DAYS = 7       # rytm zakupów, gdy nie da się go odczytać
SERVICE_FACTOR = 1.04         # ~85% szans, że zapas wystarczy do kolejnych zakupów
GROWTH_LIMIT = 1.5            # maks. krotność największego zakupu z ostatnich 90 dni
GROWTH_LOOKBACK_DAYS = 90

PIECE_UNITS = {'szt', 'opak'}


@dataclass(frozen=True)
class PantryForecast:
    status: str                 # 'no_history' | 'cold_start' (nauka) | 'ready'
    model: str                  # 'none' | 'regular' | 'intermittent'
    rate: Decimal               # tempo w jednostce produktu na dzień
    confidence: str
    confidence_score: int
    history_days: int
    event_count: int
    minimum_date_from: date | None
    minimum_date_to: date | None
    buy_date: date | None
    days_to_minimum_from: int | None
    days_to_minimum_to: int | None
    trend: str
    trend_percent: Decimal | None
    suggested_quantity: Decimal  # w jednostce produktu
    suggested_packages: int      # w opakowaniach (0 = produkt bez opakowań)
    is_due: bool
    at_or_below_minimum: bool
    counts_packages: bool = False          # na liście w sztukach zamiast g/ml
    weekly_usage: Decimal = Decimal('0.00')  # w szt. (albo jednostce produktu)
    learning_days_left: int = 0
    learning_events_left: int = 0
    purchase_basis: str = 'none'           # 'minimum' | 'forecast' | 'none'
    review_days: int = DEFAULT_REVIEW_DAYS

    @property
    def is_learning(self) -> bool:
        return self.status != 'ready'


def _as_float(value) -> float:
    return max(float(value or 0), 0.0)


def _signed_float(value) -> float:
    return float(value or 0)


@dataclass(frozen=True)
class _Units:
    """W czym liczy prognoza.

    ``scale`` - ile jednostek produktu ma jedna jednostka licząca
    (jogurt: 400 g = 1 szt.). ``step`` - co ile jednostek liczących się
    kupuje (1 opakowanie; jajka po 10 szt.; 0 = dowolna ilość).
    """

    scale: float
    step: float
    counts_packages: bool


def _units(product) -> _Units:
    unit = getattr(product, 'unit', '')
    package_size = _as_float(getattr(product, 'quantity_per_scan', 0))
    if unit in PIECE_UNITS:
        return _Units(scale=1.0, step=max(package_size, 1.0), counts_packages=False)
    if package_size > 0 and (
        getattr(product, 'barcode', '') or getattr(product, 'current_package_count', 0) > 0
    ):
        return _Units(scale=package_size, step=1.0, counts_packages=True)
    return _Units(scale=1.0, step=0.0, counts_packages=False)


def _movement_list(product, movements=None) -> list:
    if movements is not None:
        return list(movements)
    prefetched = getattr(product, '_prefetched_objects_cache', {}).get('movements')
    if prefetched is not None:
        return list(prefetched)
    return list(product.movements.all())


def _product_created_on(product, today: date) -> date:
    created_at = getattr(product, 'created_at', None)
    if not created_at:
        return today
    if timezone.is_aware(created_at):
        return timezone.localtime(created_at).date()
    return created_at.date()


def _date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _robust_cap(positive_values: Sequence[float]) -> float:
    if len(positive_values) < 3:
        return max(positive_values, default=0.0)
    centre = float(median(positive_values))
    deviations = [abs(value - centre) for value in positive_values]
    mad = float(median(deviations))
    return max(centre + max(3.0 * mad, 2.0 * centre), centre)


def _recency_weight(day: date, today: date, half_life_days: int = PACE_HALF_LIFE_DAYS) -> float:
    age = max((today - day).days, 0)
    return 0.5 ** (age / max(half_life_days, 1))


def _weighted_mean(
    values: dict[date, float],
    observed_dates: Sequence[date],
    today: date,
    *,
    half_life_days: int = PACE_HALF_LIFE_DAYS,
    prior_days: float = 0.0,
) -> float:
    """Średnia ważona świeżością; ``prior_days`` przyciąga ją do średniej zwykłej.

    Gdy ostatnio produkt był w domu tylko przez kilka dni, sama średnia ważona
    potrafi skoczyć (kilka jogurtów zjedzonych jednego dnia). Średnia z całej
    historii działa wtedy jak kotwica o wadze ``prior_days`` dni danych.
    """
    weighted_total = 0.0
    weights = 0.0
    for day in observed_dates:
        weight = _recency_weight(day, today, half_life_days=half_life_days)
        weighted_total += values.get(day, 0.0) * weight
        weights += weight
    if not weights:
        return 0.0
    if prior_days > 0 and observed_dates:
        plain = sum(values.get(day, 0.0) for day in observed_dates) / len(observed_dates)
        return (weighted_total + prior_days * plain) / (weights + prior_days)
    return weighted_total / weights


def _window_mean(
    values: dict[date, float],
    observed_dates: Sequence[date],
    today: date,
    min_age: int,
    max_age: int,
) -> tuple[float, int]:
    selected = [
        day for day in observed_dates
        if min_age <= (today - day).days <= max_age
    ]
    if not selected:
        return 0.0, 0
    return sum(values.get(day, 0.0) for day in selected) / len(selected), len(selected)


def _trend(values: dict[date, float], observed_dates: Sequence[date], today: date):
    recent, recent_days = _window_mean(values, observed_dates, today, 0, 13)
    previous, previous_days = _window_mean(values, observed_dates, today, 14, 41)
    if recent_days < 5 or previous_days < 7:
        return 'unknown', None
    if previous <= 0:
        if recent <= 0:
            return 'stable', Decimal('0.00')
        return 'rising', Decimal('100.00')
    percent = max(min(((recent - previous) / previous) * 100.0, 999.0), -999.0)
    if percent > 15:
        label = 'rising'
    elif percent < -15:
        label = 'falling'
    else:
        label = 'stable'
    return label, Decimal(str(abs(percent))).quantize(TWO_PLACES)


def _weekday_factors(
    values: dict[date, float],
    observed_dates: Sequence[date],
    rate: float,
    event_days: int,
) -> dict[int, float]:
    if len(observed_dates) < 42 or event_days < 6 or rate <= 0:
        return {weekday: 1.0 for weekday in range(7)}

    totals = defaultdict(float)
    counts = Counter()
    for day in observed_dates:
        totals[day.weekday()] += values.get(day, 0.0)
        counts[day.weekday()] += 1

    factors = {}
    for weekday in range(7):
        count = counts[weekday]
        weekday_mean = totals[weekday] / count if count else rate
        # Four prior observations shrink noisy weekday effects towards 1.0.
        factor = ((weekday_mean / rate) * count + 4.0) / (count + 4.0)
        factors[weekday] = min(max(factor, 0.5), 1.5)

    normalizer = sum(factors.values()) / 7.0
    return {weekday: factor / normalizer for weekday, factor in factors.items()}


def _days_until_threshold(
    usable_stock: float,
    rate: float,
    scale: float,
    today: date,
    weekday_factors: dict[int, float],
) -> int | None:
    if usable_stock <= 0:
        return 0
    if rate <= 0 or scale <= 0:
        return None

    cumulative = 0.0
    for day_number in range(1, MAX_FORECAST_DAYS + 1):
        future_day = today + timedelta(days=day_number)
        cumulative += rate * scale * weekday_factors.get(future_day.weekday(), 1.0)
        if cumulative + 1e-9 >= usable_stock:
            return day_number
    return int((usable_stock / (rate * scale)) + 0.999999)


def _intermittent_days_range(
    usable_stock: float,
    event_probability: float,
    event_size: float,
    event_values: Sequence[float],
    confidence_score: int,
) -> tuple[int | None, int | None]:
    """Return the 10th–90th percentile of time to an intermittent stockout.

    Waiting for ``r`` consumption events follows an exact negative-binomial
    distribution. Computing its CDF recursively is still cheap for the capped
    forecast horizon and, importantly, remains accurate for rare products that
    need only one or two further uses to cross their minimum.
    """
    if usable_stock <= 0:
        return 0, 0
    if event_probability <= 0 or event_size <= 0:
        return None, None

    event_variance = (
        sum((value - event_size) ** 2 for value in event_values) / len(event_values)
        if event_values
        else 0.0
    )
    event_cv = sqrt(max(event_variance, 0.0)) / event_size
    size_spread = min(event_cv, 1.5) * (1.0 + (100 - confidence_score) / 200.0) * 0.5
    early_event_size = event_size * (1.0 + size_spread)
    late_event_size = event_size * max(1.0 - size_spread, 0.25)
    early_required_events = max(ceil(usable_stock / early_event_size), 1)
    late_required_events = max(ceil(usable_stock / late_event_size), early_required_events)

    early = _negative_binomial_wait_quantile(
        early_required_events,
        event_probability,
        0.10,
    )
    late = _negative_binomial_wait_quantile(
        late_required_events,
        event_probability,
        0.90,
    )
    return early, max(early, late)


def _negative_binomial_wait_quantile(
    required_events: int,
    event_probability: float,
    quantile: float,
) -> int:
    """Exact quantile of Bernoulli trials needed to observe ``required_events``."""
    required_events = max(int(required_events), 1)
    event_probability = min(max(event_probability, 1e-12), 1.0)
    quantile = min(max(quantile, 0.0), 1.0)
    if event_probability >= 1.0:
        return min(required_events, MAX_FORECAST_DAYS)

    # P(N=n) = C(n-1, r-1) * p**r * (1-p)**(n-r).
    # Keeping the recurrence in log-space prevents underflow for small p.
    log_probability = required_events * log(event_probability)
    log_failure = log1p(-event_probability)
    cumulative = 0.0
    for days in range(required_events, MAX_FORECAST_DAYS + 1):
        if days > required_events:
            previous_days = days - 1
            log_probability += (
                log(previous_days)
                - log(days - required_events)
                + log_failure
            )
        if log_probability > -745.0:
            cumulative += exp(log_probability)
        if cumulative + 1e-12 >= quantile:
            return days
    return MAX_FORECAST_DAYS


def _align_to_shopping_day(deadline: date, today: date, shopping_weekday: int | None) -> date:
    """Pierwszy dzień zakupów w dniu ``deadline`` albo po nim."""
    deadline = max(deadline, today)
    if shopping_weekday is None:
        return deadline
    return deadline + timedelta(days=(shopping_weekday - deadline.weekday()) % 7)


def infer_typical_shopping_weekday(dates: Iterable[date]) -> int | None:
    unique_dates = sorted(set(dates))
    if len(unique_dates) < 3:
        return None
    counts = Counter(day.weekday() for day in unique_dates)
    weekday, count = counts.most_common(1)[0]
    # Do not invent a routine when purchase dates are almost evenly distributed.
    return weekday if count >= max(2, int(len(unique_dates) * 0.35 + 0.999999)) else None


def infer_shopping_interval(dates: Iterable[date]) -> int:
    """Co ile dni dom robi zakupy: mediana odstępów między dniami zakupów.

    Przy mniej niż czterech dniach zakupów zakładamy tydzień. Wynik mieści się
    w 2-14 dniach, żeby jeden długi wyjazd nie rozciągnął zakupów na miesiąc.
    """
    unique_dates = sorted(set(dates))
    if len(unique_dates) < 4:
        return DEFAULT_REVIEW_DAYS
    gaps = [(later - earlier).days for earlier, later in zip(unique_dates, unique_dates[1:])]
    return int(min(max(round(median(gaps)), 2), 14))


def _learning_units(minimum_cu: float, units: _Units) -> float:
    """Ile kupić w fazie nauki: próg minimalny, co najmniej jedno opakowanie."""
    if units.step > 0:
        return float(max(ceil(minimum_cu / units.step - 1e-9), 1))
    return minimum_cu


def _to_product_quantity(amount: float, units: _Units) -> tuple[Decimal, int]:
    """(ilość w jednostce produktu, liczba opakowań) dla ``amount`` opakowań/jednostek."""
    if units.step > 0:
        packages = int(amount)
        quantity = Decimal(str(packages * units.step * units.scale)).quantize(TWO_PLACES)
        return quantity, packages
    return Decimal(str(max(amount, 0.0))).quantize(TWO_PLACES), 0


def _learning_suggestion(product, minimum_cu: float, units: _Units) -> tuple[Decimal, int]:
    amount = _learning_units(minimum_cu, units)
    if units.step <= 0 and amount <= 0:
        # Produkt ważony bez progu i bez znanego opakowania: jedna "porcja",
        # jaką zwykle dodaje skaner albo formularz.
        amount = max(_as_float(getattr(product, 'quantity_per_scan', 0)), 1.0)
    return _to_product_quantity(amount, units)


def _round_purchase(need_cu: float, units: _Units) -> float:
    """Brakująca ilość zaokrąglona w górę do pełnych opakowań.

    Zaokrąglanie w dół (1,2 opak. -> 1) wyglądało oszczędnie, ale w symulacji
    podwajało braki produktów używanych rzadko i porcjami (mąka, ketchup).
    """
    if units.step <= 0:
        return max(need_cu, 0.0)
    return float(max(ceil(need_cu / units.step - 1e-9), 0))


def _chunk_totals(series: Sequence[float], length: int) -> list[float]:
    """Sumy zużycia w kolejnych, nienachodzących okresach po ``length`` dni (od końca)."""
    length = max(int(length), 1)
    totals = []
    end = len(series)
    while end - length >= 0:
        totals.append(sum(series[end - length:end]))
        end -= length
    return totals


def forecast_pantry_product(
    product,
    *,
    today: date | None = None,
    max_history_days: int = HISTORY_DAYS,
    shopping_weekday: int | None = None,
    review_days: int | None = None,
    movements=None,
) -> PantryForecast:
    today = today or timezone.localdate()
    units = _units(product)
    review_days = max(int(review_days or DEFAULT_REVIEW_DAYS), 1)
    lead_days = max(int(getattr(product, 'restock_lead_days', 0) or 0), 0)

    all_movements = _movement_list(product, movements)
    earliest_movement = min(
        (movement.occurred_on for movement in all_movements if movement.occurred_on <= today),
        default=today,
    )
    natural_start = min(_product_created_on(product, today), earliest_movement)
    start = max(natural_start, today - timedelta(days=max(max_history_days, 14) - 1))

    # Wszystko w jednostkach liczących (dla jogurtu 400 g: w opakowaniach).
    daily_consumed = defaultdict(float)
    daily_purchased = defaultdict(float)
    daily_adjusted = defaultdict(float)
    consume_events = 0
    for movement in all_movements:
        if movement.occurred_on < start or movement.occurred_on > today:
            continue
        if movement.movement_type == 'consume':
            quantity = _as_float(movement.quantity) / units.scale
            daily_consumed[movement.occurred_on] += quantity
            if quantity > 0:
                consume_events += 1
        elif movement.movement_type == 'purchase':
            daily_purchased[movement.occurred_on] += _as_float(movement.quantity) / units.scale
        elif movement.movement_type == 'adjust':
            # Korekta ma znak (-500 g to zmniejszenie stanu) i nie jest ani
            # zużyciem, ani zakupem - liczy się tylko przy odtwarzaniu zapasu.
            daily_adjusted[movement.occurred_on] += _signed_float(movement.quantity) / units.scale

    # Odtwarzamy stan dzień po dniu wstecz od dzisiejszego. Dni, w których
    # produktu nie było, nie są "zerowym zużyciem" - pomijamy je.
    current_cu = _as_float(getattr(product, 'current_quantity', 0)) / units.scale
    minimum_cu = _as_float(getattr(product, 'minimum_quantity', 0)) / units.scale
    observed_dates = []
    inferred_end_stock = current_cu
    for day in reversed(_date_range(start, today)):
        consumed = daily_consumed.get(day, 0.0)
        purchased = daily_purchased.get(day, 0.0)
        adjusted = daily_adjusted.get(day, 0.0)
        inferred_start_stock = max(inferred_end_stock - purchased - adjusted + consumed, 0.0)
        if inferred_end_stock > 1e-9 or inferred_start_stock > 1e-9 or consumed > 0 or purchased > 0:
            observed_dates.append(day)
        inferred_end_stock = inferred_start_stock
    observed_dates.sort()

    at_or_below_minimum = current_cu <= minimum_cu + 1e-9
    history_days = len(observed_dates)
    span_days = (today - observed_dates[0]).days + 1 if observed_dates else 0
    event_days = sum(1 for day in observed_dates if daily_consumed.get(day, 0.0) > 0)
    learning_days_left = max(LEARNING_MIN_DAYS - span_days, 0)
    learning_events_left = max(LEARNING_MIN_EVENTS - event_days, 0)

    if learning_days_left or learning_events_left:
        # Faza nauki: bez ekstrapolacji. Tempo liczymy tylko informacyjnie.
        total = sum(daily_consumed.get(day, 0.0) for day in observed_dates)
        provisional_rate = total / history_days if history_days and total > 0 else 0.0
        suggested_quantity, suggested_packages = _learning_suggestion(product, minimum_cu, units)
        return PantryForecast(
            status='cold_start' if consume_events else 'no_history',
            model='none' if not consume_events else 'regular',
            rate=Decimal(str(provisional_rate * units.scale)).quantize(TWO_PLACES),
            confidence='low',
            confidence_score=0 if not consume_events else min(int(event_days * 5), 30),
            history_days=history_days,
            event_count=consume_events,
            minimum_date_from=today if at_or_below_minimum else None,
            minimum_date_to=today if at_or_below_minimum else None,
            buy_date=today if at_or_below_minimum else None,
            days_to_minimum_from=0 if at_or_below_minimum else None,
            days_to_minimum_to=0 if at_or_below_minimum else None,
            trend='unknown',
            trend_percent=None,
            suggested_quantity=suggested_quantity,
            suggested_packages=suggested_packages,
            is_due=at_or_below_minimum,
            at_or_below_minimum=at_or_below_minimum,
            counts_packages=units.counts_packages,
            learning_days_left=learning_days_left,
            learning_events_left=learning_events_left,
            purchase_basis='minimum',
            review_days=review_days,
        )

    # --- Tempo: dni z zapasem, nowsze ważą więcej, skoki przycięte -------------
    positive_days = [quantity for quantity in daily_consumed.values() if quantity > 0]
    cap = _robust_cap(positive_days)
    demand = {
        day: min(daily_consumed.get(day, 0.0), cap)
        for day in observed_dates
    }
    demand_frequency = event_days / max(history_days, 1)
    model = 'regular' if demand_frequency >= 0.35 else 'intermittent'

    event_probability = None
    event_size = None
    event_values = []
    if model == 'intermittent':
        probability_values = {day: 1.0 if demand.get(day, 0.0) > 0 else 0.0 for day in observed_dates}
        event_probability = _weighted_mean(
            probability_values,
            observed_dates,
            today,
            half_life_days=PACE_HALF_LIFE_DAYS,
            prior_days=PACE_PRIOR_DAYS,
        )
        event_dates = [day for day in observed_dates if demand.get(day, 0.0) > 0]
        event_size = _weighted_mean(
            demand,
            event_dates,
            today,
            half_life_days=PACE_HALF_LIFE_DAYS * 2,
            prior_days=PACE_PRIOR_DAYS / 2,
        )
        event_values = [demand[day] for day in event_dates]
        rate = event_probability * event_size
    else:
        rate = _weighted_mean(demand, observed_dates, today, prior_days=PACE_PRIOR_DAYS)

    variance = sum((demand.get(day, 0.0) - rate) ** 2 for day in observed_dates) / max(history_days, 1)
    daily_std = sqrt(max(variance, 0.0))
    coefficient_of_variation = daily_std / rate if rate > 0 else 3.0

    span_score = min(span_days / 90.0, 1.0) * 30.0
    event_score = min(event_days / 20.0, 1.0) * 45.0
    stability_score = max(0.0, 1.0 - min(coefficient_of_variation, 2.0) / 2.0) * 20.0
    coverage_score = min(event_days / 8.0, 1.0) * 5.0
    confidence_score = int(round(span_score + event_score + stability_score + coverage_score))
    confidence = 'high' if confidence_score >= 75 else 'medium' if confidence_score >= 45 else 'low'

    trend, trend_percent = _trend(demand, observed_dates, today)
    weekday_factors = _weekday_factors(demand, observed_dates, rate, event_days)

    # --- Kiedy zapas spadnie do minimum (zakres 10.-90. percentyla) -----------
    usable_stock = max(current_cu - minimum_cu, 0.0)
    data_penalty = 1.0 - confidence_score / 100.0
    uncertainty = min(
        0.72,
        max(0.12, 0.12 + 0.22 * min(coefficient_of_variation, 2.5) + 0.28 * data_penalty),
    )
    if model == 'intermittent':
        days_from, days_to = _intermittent_days_range(
            usable_stock,
            event_probability,
            event_size,
            event_values,
            confidence_score,
        )
    else:
        days_from = _days_until_threshold(
            usable_stock, rate, 1.0 + uncertainty, today, weekday_factors,
        )
        days_to = _days_until_threshold(
            usable_stock, rate, max(1.0 - uncertainty, 0.25), today, weekday_factors,
        )
    if days_from is not None and days_to is not None and days_to < days_from:
        days_from, days_to = days_to, days_from
    minimum_date_from = today + timedelta(days=days_from) if days_from is not None else None
    minimum_date_to = today + timedelta(days=days_to) if days_to is not None else None

    # --- Ile kupić: uzupełnienie do poziomu S przy najbliższych zakupach -------
    horizon = review_days + lead_days
    in_stock_series = [demand.get(day, 0.0) for day in observed_dates]
    chunk_totals = _chunk_totals(in_stock_series, horizon)
    if len(chunk_totals) >= 3:
        horizon_std = pstdev(chunk_totals)
    else:
        horizon_std = daily_std * sqrt(horizon)
    safety_stock = min(SERVICE_FACTOR * horizon_std, rate * review_days)
    order_up_to = minimum_cu + rate * horizon + safety_stock

    days_to_trip = (
        (shopping_weekday - today.weekday()) % 7
        if shopping_weekday is not None
        else 0
    )
    stock_at_trip = max(current_cu - rate * days_to_trip, 0.0)
    need = order_up_to - stock_at_trip
    threshold = 1e-6
    buy_now = at_or_below_minimum or need >= threshold

    if buy_now:
        buy_date = today if at_or_below_minimum else today + timedelta(days=days_to_trip)
        amount = _round_purchase(need, units)
        largest_purchase = max(
            (
                quantity for day, quantity in daily_purchased.items()
                if (today - day).days < GROWTH_LOOKBACK_DAYS
            ),
            default=0.0,
        )
        if largest_purchase > 0:
            largest_units = largest_purchase / units.step if units.step > 0 else largest_purchase
            amount = min(amount, max(GROWTH_LIMIT * largest_units, 1.0 if units.step > 0 else 0.0))
            if units.step > 0:
                amount = float(ceil(amount - 1e-9))
        if at_or_below_minimum:
            amount = max(amount, _learning_units(minimum_cu, units))
        if units.step > 0:
            amount = max(amount, 1.0)
        suggested_quantity, suggested_packages = _to_product_quantity(amount, units)
    else:
        suggested_quantity, suggested_packages = Decimal('0.00'), 0
        if rate > 0:
            days_until_need = ceil((stock_at_trip - order_up_to + threshold) / rate)
            buy_date = _align_to_shopping_day(
                today + timedelta(days=days_to_trip + max(days_until_need, 1)),
                today,
                shopping_weekday,
            )
        else:
            buy_date = None

    return PantryForecast(
        status='ready',
        model=model,
        rate=Decimal(str(rate * units.scale)).quantize(TWO_PLACES),
        confidence=confidence,
        confidence_score=confidence_score,
        history_days=history_days,
        event_count=consume_events,
        minimum_date_from=minimum_date_from,
        minimum_date_to=minimum_date_to,
        buy_date=buy_date,
        days_to_minimum_from=days_from,
        days_to_minimum_to=days_to,
        trend=trend,
        trend_percent=trend_percent,
        suggested_quantity=suggested_quantity,
        suggested_packages=suggested_packages,
        is_due=buy_now,
        at_or_below_minimum=at_or_below_minimum,
        counts_packages=units.counts_packages,
        weekly_usage=Decimal(str(rate * 7)).quantize(TWO_PLACES),
        purchase_basis='forecast',
        review_days=review_days,
    )


def _forecast_key(product):
    return getattr(product, 'pk', None) or id(product)


def forecast_pantry_products(
    products: Sequence,
    *,
    today: date | None = None,
    shopping_weekday: int | None = None,
    review_days: int | None = None,
) -> dict:
    """Prognozy dla wielu produktów (i grup) naraz, klucz = ``pk``."""
    today = today or timezone.localdate()
    return {
        _forecast_key(product): forecast_pantry_product(
            product,
            today=today,
            shopping_weekday=shopping_weekday,
            review_days=review_days,
            movements=getattr(product, 'forecast_movements', None),
        )
        for product in products
    }
