from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING
from math import ceil, exp, log, log1p, sqrt
from statistics import median
from typing import Iterable, Sequence

from django.utils import timezone


TWO_PLACES = Decimal('0.01')
MAX_FORECAST_DAYS = 3650


@dataclass(frozen=True)
class PantryForecast:
    status: str
    model: str
    rate: Decimal
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
    suggested_quantity: Decimal
    suggested_packages: int
    is_due: bool
    at_or_below_minimum: bool


def _as_float(value) -> float:
    return max(float(value or 0), 0.0)


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


def _recency_weight(day: date, today: date, half_life_days: int = 21) -> float:
    age = max((today - day).days, 0)
    return 0.5 ** (age / max(half_life_days, 1))


def _weighted_mean(
    values: dict[date, float],
    observed_dates: Sequence[date],
    today: date,
    *,
    half_life_days: int = 21,
) -> float:
    weighted_total = 0.0
    weights = 0.0
    for day in observed_dates:
        weight = _recency_weight(day, today, half_life_days=half_life_days)
        weighted_total += values.get(day, 0.0) * weight
        weights += weight
    return weighted_total / weights if weights else 0.0


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
    if shopping_weekday is None or deadline <= today:
        return max(deadline, today)
    aligned = deadline - timedelta(days=(deadline.weekday() - shopping_weekday) % 7)
    return aligned if aligned >= today else today


def infer_typical_shopping_weekday(dates: Iterable[date]) -> int | None:
    unique_dates = sorted(set(dates))
    if len(unique_dates) < 3:
        return None
    counts = Counter(day.weekday() for day in unique_dates)
    weekday, count = counts.most_common(1)[0]
    # Do not invent a routine when purchase dates are almost evenly distributed.
    return weekday if count >= max(2, int(len(unique_dates) * 0.35 + 0.999999)) else None


def _tracks_packages(product) -> bool:
    return bool(
        getattr(product, 'barcode', '')
        or getattr(product, 'current_package_count', 0) > 0
        or getattr(product, 'unit', '') in {'szt', 'opak'}
    )


def _purchase_suggestion(product, rate: float, daily_std: float, at_minimum: bool):
    minimum = _as_float(getattr(product, 'minimum_quantity', 0))
    current = _as_float(getattr(product, 'current_quantity', 0))
    lead_days = max(int(getattr(product, 'restock_lead_days', 0) or 0), 0)
    coverage_days = max(lead_days + 7, 7)
    safety_stock = 1.04 * daily_std * sqrt(coverage_days)
    target = minimum + rate * coverage_days + safety_stock
    shortfall = max(target - current, 0.0)

    package_size = _as_float(getattr(product, 'quantity_per_scan', 0))
    if _tracks_packages(product) and package_size > 0:
        packages = int((Decimal(str(shortfall)) / Decimal(str(package_size))).to_integral_value(
            rounding=ROUND_CEILING,
        )) if shortfall > 0 else 0
        if at_minimum and packages == 0:
            packages = 1
        quantity = (Decimal(packages) * Decimal(str(package_size))).quantize(TWO_PLACES)
        return quantity, packages

    if at_minimum and rate <= 0:
        shortfall = max(shortfall, minimum - current)
        if shortfall <= 0:
            shortfall = max(minimum, 1.0)
    elif at_minimum and shortfall <= 0:
        shortfall = max(package_size, 1.0)
    return Decimal(str(shortfall)).quantize(TWO_PLACES), 0


def forecast_pantry_product(
    product,
    *,
    today: date | None = None,
    max_history_days: int = 90,
    shopping_weekday: int | None = None,
    movements=None,
    category_prior_rate: Decimal | float | None = None,
) -> PantryForecast:
    today = today or timezone.localdate()
    all_movements = _movement_list(product, movements)
    earliest_movement = min(
        (movement.occurred_on for movement in all_movements if movement.occurred_on <= today),
        default=today,
    )
    natural_start = min(_product_created_on(product, today), earliest_movement)
    start = max(natural_start, today - timedelta(days=max(max_history_days, 14) - 1))

    daily_consumed = defaultdict(float)
    daily_purchased = defaultdict(float)
    daily_adjusted = defaultdict(float)
    consume_events = []
    for movement in all_movements:
        if movement.occurred_on < start or movement.occurred_on > today:
            continue
        quantity = _as_float(movement.quantity)
        if movement.movement_type == 'consume':
            daily_consumed[movement.occurred_on] += quantity
            if quantity > 0:
                consume_events.append(movement)
        elif movement.movement_type == 'purchase':
            daily_purchased[movement.occurred_on] += quantity
        elif movement.movement_type == 'adjust':
            # Korekta stanu (np. w edycji produktu) ma znak i nie jest ani
            # zużyciem, ani zakupem - liczy się tylko przy odtwarzaniu zapasu.
            daily_adjusted[movement.occurred_on] += quantity

    calendar_days = _date_range(start, today)
    observed_dates = []
    inferred_end_stock = _as_float(getattr(product, 'current_quantity', 0))
    for day in reversed(calendar_days):
        consumed = daily_consumed.get(day, 0.0)
        purchased = daily_purchased.get(day, 0.0)
        adjusted = daily_adjusted.get(day, 0.0)
        inferred_start_stock = max(inferred_end_stock - purchased - adjusted + consumed, 0.0)
        if inferred_end_stock > 0 or inferred_start_stock > 0 or consumed > 0 or purchased > 0:
            observed_dates.append(day)
        inferred_end_stock = inferred_start_stock
    observed_dates.sort()

    current = _as_float(getattr(product, 'current_quantity', 0))
    minimum = _as_float(getattr(product, 'minimum_quantity', 0))
    at_or_below_minimum = current <= minimum + 1e-9
    event_count = len(consume_events)
    history_days = len(observed_dates)

    if not consume_events or not observed_dates:
        prior_rate = _as_float(category_prior_rate)
        suggested_quantity, suggested_packages = _purchase_suggestion(
            product, prior_rate, 0.0, at_or_below_minimum,
        )
        return PantryForecast(
            status='no_history', model='none',
            rate=Decimal(str(prior_rate)).quantize(TWO_PLACES), confidence='low',
            confidence_score=0, history_days=history_days, event_count=0,
            minimum_date_from=today if at_or_below_minimum else None,
            minimum_date_to=today if at_or_below_minimum else None,
            buy_date=today if at_or_below_minimum else None,
            days_to_minimum_from=0 if at_or_below_minimum else None,
            days_to_minimum_to=0 if at_or_below_minimum else None,
            trend='unknown', trend_percent=None,
            suggested_quantity=suggested_quantity,
            suggested_packages=suggested_packages,
            is_due=at_or_below_minimum,
            at_or_below_minimum=at_or_below_minimum,
        )

    positive_days = [quantity for quantity in daily_consumed.values() if quantity > 0]
    cap = _robust_cap(positive_days)
    demand = {
        day: min(daily_consumed.get(day, 0.0), cap)
        for day in observed_dates
    }
    event_days = sum(1 for value in demand.values() if value > 0)
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
            half_life_days=14,
        )
        event_dates = [day for day in observed_dates if demand.get(day, 0.0) > 0]
        event_size = _weighted_mean(
            demand,
            event_dates,
            today,
            half_life_days=42,
        )
        event_values = [demand[day] for day in event_dates]
        rate = event_probability * event_size
    else:
        rate = _weighted_mean(demand, observed_dates, today)

    is_cold_start = event_days < 3 or history_days < 14
    prior_rate = _as_float(category_prior_rate)
    if is_cold_start and prior_rate > 0:
        own_weight = event_days / (event_days + 8.0)
        rate = own_weight * rate + (1.0 - own_weight) * prior_rate

    variance = sum((demand.get(day, 0.0) - rate) ** 2 for day in observed_dates) / max(history_days, 1)
    daily_std = sqrt(max(variance, 0.0))
    coefficient_of_variation = daily_std / rate if rate > 0 else 3.0

    span_score = min(history_days / 60.0, 1.0) * 30.0
    event_score = min(event_days / 12.0, 1.0) * 45.0
    stability_score = max(0.0, 1.0 - min(coefficient_of_variation, 2.0) / 2.0) * 20.0
    coverage_score = min(event_days / 8.0, 1.0) * 5.0
    confidence_score = int(round(span_score + event_score + stability_score + coverage_score))
    status = 'cold_start' if is_cold_start else 'ready'
    if status == 'cold_start':
        confidence_score = min(confidence_score, 39)
    confidence = 'high' if confidence_score >= 75 else 'medium' if confidence_score >= 45 else 'low'

    trend, trend_percent = _trend(demand, observed_dates, today)
    weekday_factors = _weekday_factors(demand, observed_dates, rate, event_days)
    usable_stock = max(current - minimum, 0.0)

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
    lead_days = max(int(getattr(product, 'restock_lead_days', 0) or 0), 0)
    raw_buy_date = (
        minimum_date_from - timedelta(days=lead_days)
        if minimum_date_from is not None
        else None
    )
    buy_date = (
        _align_to_shopping_day(raw_buy_date, today, shopping_weekday)
        if raw_buy_date is not None
        else None
    )
    if status == 'cold_start' and not at_or_below_minimum:
        buy_date = None
    is_due = at_or_below_minimum or bool(buy_date and buy_date <= today)
    suggested_quantity, suggested_packages = _purchase_suggestion(
        product, rate, daily_std, at_or_below_minimum,
    )

    return PantryForecast(
        status=status,
        model=model,
        rate=Decimal(str(rate)).quantize(TWO_PLACES),
        confidence=confidence,
        confidence_score=confidence_score,
        history_days=history_days,
        event_count=event_count,
        minimum_date_from=minimum_date_from,
        minimum_date_to=minimum_date_to,
        buy_date=buy_date,
        days_to_minimum_from=days_from,
        days_to_minimum_to=days_to,
        trend=trend,
        trend_percent=trend_percent,
        suggested_quantity=suggested_quantity,
        suggested_packages=suggested_packages,
        is_due=is_due,
        at_or_below_minimum=at_or_below_minimum,
    )


def _forecast_key(product):
    return getattr(product, 'pk', None) or id(product)


def _category_prior_key(product):
    category = str(getattr(product, 'category', '') or '').strip().casefold()
    if not category:
        return None
    return category, getattr(product, 'unit', ''), _tracks_packages(product)


def forecast_pantry_products(
    products: Sequence,
    *,
    today: date | None = None,
    shopping_weekday: int | None = None,
) -> dict:
    """Forecast a user's products together so sparse histories can use local priors."""
    today = today or timezone.localdate()
    products = list(products)
    preliminary = {}
    for product in products:
        movements = getattr(product, 'forecast_movements', None)
        preliminary[_forecast_key(product)] = forecast_pantry_product(
            product,
            today=today,
            shopping_weekday=shopping_weekday,
            movements=movements,
        )

    grouped_rates = defaultdict(list)
    for product in products:
        forecast = preliminary[_forecast_key(product)]
        key = _category_prior_key(product)
        if key is None or forecast.status != 'ready' or forecast.rate <= 0:
            continue
        rate = float(forecast.rate)
        package_size = _as_float(getattr(product, 'quantity_per_scan', 0))
        if key[2] and package_size > 0:
            rate /= package_size
        grouped_rates[key].append((rate, max(forecast.confidence_score, 10)))

    priors = {
        key: sum(rate * weight for rate, weight in values) / sum(weight for _, weight in values)
        for key, values in grouped_rates.items()
    }
    forecasts = dict(preliminary)
    for product in products:
        key = _category_prior_key(product)
        forecast_key = _forecast_key(product)
        if key not in priors or preliminary[forecast_key].status == 'ready':
            continue
        prior_rate = priors[key]
        package_size = _as_float(getattr(product, 'quantity_per_scan', 0))
        if key[2] and package_size > 0:
            prior_rate *= package_size
        forecasts[forecast_key] = forecast_pantry_product(
            product,
            today=today,
            shopping_weekday=shopping_weekday,
            movements=getattr(product, 'forecast_movements', None),
            category_prior_rate=prior_rate,
        )
    return forecasts
