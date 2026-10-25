"""Zdrowie i forma z Garmin Connect: sen, tętno spoczynkowe, HRV, Body Battery,
kroki, stres, gotowość do treningu, VO2max, status treningowy, prognozy biegu.

Jeden wiersz DailyHealth na dzień. Każde źródło pobierane osobno - brak jednego
(np. zegarek bez HRV) nie blokuje reszty. Surowe odpowiedzi (przycięte do
podsumowań) zostają w `raw`, więc po zmianie nazw pól u Garmina wystarczy
`sync_garmin --reparse`, bez ponownego pobierania.
"""
import logging
import re
import time
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from ..models import ActivitySettings, DailyHealth

logger = logging.getLogger(__name__)

# Domyślnie odświeżamy dziś i wczoraj (sen „wczoraj w nocy” dochodzi rano),
# nie częściej niż co 2 godziny - cron chodzi co godzinę.
DEFAULT_DAYS = 2
INTERVAL = timedelta(hours=2)
MAX_LIST = 30
# Luki z ostatnich 30 dni (brak wiersza, brak podsumowania/snu albo błąd przy
# pobieraniu) uzupełniamy po kilka dni na synchronizację, każdy dzień najwyżej
# raz dziennie i najwyżej 3 razy - dzień bez zegarka na ręce nie wraca w kółko.
BACKFILL_DAYS = 30
GAP_DAYS_PER_RUN = 7
MAX_ATTEMPTS = 3
ESSENTIAL = ('summary', 'sleep')


def pause():
    """Odstęp między zapytaniami przy większym pobieraniu (import, luki) -
    seria kilkudziesięciu zapytań naraz kończyła się chwilową blokadą
    Garmina i pustymi dniami. W testach 0."""
    if settings.GARMIN_REQUEST_PAUSE:
        time.sleep(settings.GARMIN_REQUEST_PAUSE)


def _int(value):
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None   # Garmin oznacza brak danych liczbą ujemną


def _positive(value):
    number = _int(value)
    return number or None


def trim(value, depth=0):
    """Podsumowanie bez długich serii pomiarów (te potrafią mieć setki KB)."""
    if isinstance(value, dict):
        return {key: trim(item, depth + 1) for key, item in value.items()
                if not (isinstance(item, list) and len(item) > MAX_LIST)}
    if isinstance(value, list):
        return [trim(item, depth + 1) for item in value[:MAX_LIST]]
    return value


def parse_summary(summary):
    summary = summary or {}
    moderate = _int(summary.get('moderateIntensityMinutes')) or 0
    vigorous = _int(summary.get('vigorousIntensityMinutes')) or 0
    return {
        'steps': _int(summary.get('totalSteps')),
        'step_goal': _positive(summary.get('dailyStepGoal')),
        'resting_hr': _positive(summary.get('restingHeartRate')),
        'stress_avg': _positive(summary.get('averageStressLevel')),
        'body_battery_high': _int(summary.get('bodyBatteryHighestValue')),
        'body_battery_low': _int(summary.get('bodyBatteryLowestValue')),
        # Garmin liczy minuty intensywne: umiarkowane + 2 × intensywne.
        'intensity_minutes': (moderate + 2 * vigorous) if (moderate or vigorous) else None,
    }


def parse_sleep(sleep):
    daily = (sleep or {}).get('dailySleepDTO') or {}
    score = ((daily.get('sleepScores') or {}).get('overall') or {}).get('value')
    return {
        'sleep_s': _positive(daily.get('sleepTimeSeconds')),
        'sleep_deep_s': _int(daily.get('deepSleepSeconds')),
        'sleep_rem_s': _int(daily.get('remSleepSeconds')),
        'sleep_score': _positive(score),
    }


def parse_hrv(hrv):
    summary = (hrv or {}).get('hrvSummary') or {}
    return {
        'hrv_avg': _positive(summary.get('lastNightAvg')),
        'hrv_status': str(summary.get('status') or '')[:20],
    }


def parse_readiness(readiness):
    items = readiness if isinstance(readiness, list) else [readiness] if readiness else []
    for item in items:
        if isinstance(item, dict) and item.get('score') is not None:
            return {'readiness': _int(item.get('score')), 'readiness_level': str(item.get('level') or '')[:20]}
    return {}


def _vo2(value):
    try:
        number = Decimal(str(value)).quantize(Decimal('0.1'))
    except Exception:
        return None
    return number if number > 0 else None


def parse_training_status(status):
    status = status or {}
    result = {}
    vo2 = ((status.get('mostRecentVO2Max') or {}).get('generic') or {})
    value = _vo2(vo2.get('vo2MaxPreciseValue') or vo2.get('vo2MaxValue'))
    if value:
        result['vo2max'] = value
    devices = ((status.get('mostRecentTrainingStatus') or {}).get('latestTrainingStatusData') or {})
    for data in devices.values() if isinstance(devices, dict) else []:
        phrase = (data or {}).get('trainingStatusFeedbackPhrase')
        if phrase:
            # "PRODUCTIVE_6" -> "PRODUCTIVE"; numer to tylko wariant komunikatu.
            result['training_status'] = re.sub(r'_\d+$', '', str(phrase))[:40]
            break
    return result


def parse_max_metrics(metrics):
    items = metrics if isinstance(metrics, list) else [metrics] if metrics else []
    for item in items:
        generic = (item or {}).get('generic') or {}
        value = _vo2(generic.get('vo2MaxPreciseValue') or generic.get('vo2MaxValue'))
        if value:
            return {'vo2max': value}
    return {}


def parse_race_predictions(predictions):
    if isinstance(predictions, list):
        predictions = predictions[-1] if predictions else {}
    predictions = predictions or {}
    return {
        'race_5k_s': _positive(predictions.get('time5K')),
        'race_10k_s': _positive(predictions.get('time10K')),
        'race_half_s': _positive(predictions.get('timeHalfMarathon')),
        'race_marathon_s': _positive(predictions.get('timeMarathon')),
    }


PARSERS = {
    'summary': parse_summary,
    'sleep': parse_sleep,
    'hrv': parse_hrv,
    'readiness': parse_readiness,
    'status': parse_training_status,
    'maxMetrics': parse_max_metrics,
    'race': parse_race_predictions,
}


def fields_from_raw(raw):
    """Pola DailyHealth z zapisanych odpowiedzi. VO2max z max metrics ma
    pierwszeństwo przed statusem treningowym (jest datowany na ten dzień)."""
    fields = {}
    for key in ('summary', 'sleep', 'hrv', 'readiness', 'status', 'maxMetrics', 'race'):
        if key in raw:
            parsed = PARSERS[key](raw[key])
            fields.update({name: value for name, value in parsed.items() if value not in (None, '')})
    return fields


def _fetch(client, day, with_fitness, paced=False):
    """Odpowiedzi Garmina dla jednego dnia: (surowe dane, klucze z błędem).
    Brak danych u Garmina = brak klucza; błąd zapytania trafia do listy."""
    iso = day.isoformat()
    calls = {
        'summary': lambda: client.get_user_summary(iso),
        'sleep': lambda: client.get_sleep_data(iso),
        'hrv': lambda: client.get_hrv_data(iso),
        'readiness': lambda: client.get_training_readiness(iso),
    }
    if with_fitness:
        calls.update({
            'status': lambda: client.get_training_status(iso),
            'maxMetrics': lambda: client.get_max_metrics(iso),
            'race': lambda: client.get_race_predictions(),
        })
    raw = {}
    errors = []
    for key, call in calls.items():
        if paced:
            pause()
        try:
            response = call()
        except Exception as exc:
            if exc.__class__.__name__ == 'GarminConnectTooManyRequestsError':
                raise
            logger.warning('Garmin: %s za %s nie pobrane: %s: %s', key, iso, exc.__class__.__name__, str(exc)[:150])
            errors.append(key)
            continue
        if key == 'sleep':
            response = {'dailySleepDTO': (response or {}).get('dailySleepDTO') or {}}
        elif key == 'hrv':
            response = {'hrvSummary': (response or {}).get('hrvSummary') or {}}
        elif key == 'status':
            response = {name: (response or {}).get(name) for name in ('mostRecentVO2Max', 'mostRecentTrainingStatus')}
        if response:
            raw[key] = trim(response)
    return raw, errors


def is_complete(raw):
    raw = raw or {}
    return all(raw.get(key) for key in ESSENTIAL) and not raw.get('_errors')


def save_day(user, day, raw, errors=(), today=None):
    """Zapisuje dzień (także pusty - z licznikiem prób, żeby wiedzieć, że był
    sprawdzany). Klucze z podkreśleniem to stan pobierania, nie dane Garmina."""
    row, _ = DailyHealth.objects.get_or_create(user=user, date=day)
    merged = dict(row.raw or {})
    merged.update(raw)
    merged['_errors'] = list(errors)
    merged['_checked'] = (today or timezone.localdate()).isoformat()
    merged['_attempts'] = 0 if is_complete(merged) else (row.raw or {}).get('_attempts', 0) + 1
    row.raw = merged
    for name, value in fields_from_raw(merged).items():
        setattr(row, name, value)
    row.save()
    return row


def gap_days(user, today, limit=GAP_DAYS_PER_RUN):
    """Dni z ostatnich BACKFILL_DAYS (bez dziś i wczoraj) do uzupełnienia, od najnowszych."""
    rows = {
        row.date: row.raw or {}
        for row in DailyHealth.objects.filter(user=user, date__gte=today - timedelta(days=BACKFILL_DAYS - 1))
    }
    gaps = []
    for offset in range(DEFAULT_DAYS, BACKFILL_DAYS):
        day = today - timedelta(days=offset)
        raw = rows.get(day)
        if raw is None:
            gaps.append(day)
        elif not is_complete(raw) and raw.get('_checked') != today.isoformat() \
                and raw.get('_attempts', 0) < MAX_ATTEMPTS:
            gaps.append(day)
        if len(gaps) >= limit:
            break
    return gaps


def sync_health(user, client, days=None, force=False, fill_gaps=True):
    """Pobiera ostatnie `days` dni (domyślnie dziś i wczoraj). Wskaźniki formy
    (status, VO2max, prognozy) tylko dla dzisiaj - zmieniają się powoli,
    a historia VO2max jest też w treningach biegowych."""
    settings_row = ActivitySettings.for_user(user)
    now = timezone.now()
    if not force and not days and settings_row.health_synced_at and now - settings_row.health_synced_at < INTERVAL:
        return 0
    today = timezone.localdate()
    if days:
        targets = [today - timedelta(days=offset) for offset in range(days)]
    else:
        targets = [today - timedelta(days=offset) for offset in range(DEFAULT_DAYS)]
        if fill_gaps:
            targets += gap_days(user, today)
    paced = len(targets) > DEFAULT_DAYS
    count = 0
    for day in targets:
        raw, errors = _fetch(client, day, with_fitness=day == today, paced=paced)
        save_day(user, day, raw, errors, today=today)
        count += bool(raw)
    settings_row.health_synced_at = now
    settings_row.save(update_fields=['health_synced_at'])
    return count


def _ranges(days):
    """Kolejne dni zwinięte w zakres: '18–20 paź, 24 paź', '29 wrz–2 paź'."""
    months = ['sty', 'lut', 'mar', 'kwi', 'maj', 'cze', 'lip', 'sie', 'wrz', 'paź', 'lis', 'gru']
    label = lambda day: f'{day.day} {months[day.month - 1]}'
    groups = []
    for day in sorted(days):
        if groups and day - groups[-1][1] == timedelta(days=1):
            groups[-1][1] = day
        else:
            groups.append([day, day])
    def span(a, b):
        if a == b:
            return label(a)
        return f'{a.day}–{label(b)}' if a.month == b.month else f'{label(a)}–{label(b)}'

    return ', '.join(span(a, b) for a, b in groups)


def coverage(user, days=BACKFILL_DAYS):
    """Jedno zdanie o danych zdrowotnych z ostatnich `days` dni (wynik komendy)."""
    today = timezone.localdate()
    rows = {row.date: row for row in DailyHealth.objects.filter(user=user, date__gte=today - timedelta(days=days - 1))}
    with_data, failed, empty = 0, 0, []
    for offset in range(days):
        day = today - timedelta(days=offset)
        row = rows.get(day)
        if row is not None and (row.steps is not None or row.sleep_s):
            with_data += 1
            failed += bool((row.raw or {}).get('_errors'))
        else:
            empty.append(day)
    text = f'zdrowie z {days} dni: z danymi {with_data}'
    if failed:
        text += f', z błędami {failed}'
    if empty:
        text += f', bez danych: {_ranges(empty)}'
    return text


def report(user, days=BACKFILL_DAYS):
    """Stan zapisanych dni do `sync_garmin --health-report` (bez sieci)."""
    today = timezone.localdate()
    rows = {row.date: row for row in DailyHealth.objects.filter(user=user, date__gte=today - timedelta(days=days - 1))}
    lines = ['data        kroki  sen     tętno HRV  źródła                       błędy / próby']
    for offset in range(days):
        day = today - timedelta(days=offset)
        row = rows.get(day)
        if row is None:
            lines.append(f'{day}  -- brak wiersza (jeszcze nie pobierany)')
            continue
        raw = row.raw or {}
        sources = ','.join(key for key in PARSERS if raw.get(key)) or '-'
        sleep = f'{row.sleep_s // 3600}:{row.sleep_s % 3600 // 60:02d}' if row.sleep_s else '-'
        lines.append(
            f'{day}  {row.steps if row.steps is not None else "-":>5}  {sleep:>6}  {row.resting_hr or "-":>5} {row.hrv_avg or "-":>4}  '
            f'{sources:<28} {",".join(raw.get("_errors") or []) or "-"} / {raw.get("_attempts", "-")} (sprawdzony {raw.get("_checked", "?")})'
        )
    return lines


def reparse(user=None):
    rows = DailyHealth.objects.all()
    if user is not None:
        rows = rows.filter(user=user)
    for row in rows.iterator():
        for name, value in fields_from_raw(row.raw or {}).items():
            setattr(row, name, value)
        row.save()


def serialize(row):
    def num(value):
        return float(value) if isinstance(value, Decimal) else value

    data = {
        'date': row.date.isoformat(),
        'steps': row.steps, 'stepGoal': row.step_goal, 'restingHr': row.resting_hr,
        'stress': row.stress_avg, 'bbHigh': row.body_battery_high, 'bbLow': row.body_battery_low,
        'intensity': row.intensity_minutes, 'sleep': row.sleep_s, 'sleepDeep': row.sleep_deep_s,
        'sleepRem': row.sleep_rem_s, 'sleepScore': row.sleep_score, 'hrv': row.hrv_avg,
        'hrvStatus': row.hrv_status, 'readiness': row.readiness, 'readinessLevel': row.readiness_level,
        'vo2max': num(row.vo2max), 'status': row.training_status,
        'race5k': row.race_5k_s, 'race10k': row.race_10k_s, 'raceHalf': row.race_half_s,
        'raceMarathon': row.race_marathon_s,
    }
    return {key: value for key, value in data.items() if value not in (None, '')}
