"""Zdrowie i forma z Garmin Connect: sen, tętno spoczynkowe, HRV, Body Battery,
kroki, stres, gotowość do treningu, VO2max, status treningowy, prognozy biegu.

Jeden wiersz DailyHealth na dzień. Każde źródło pobierane osobno - brak jednego
(np. zegarek bez HRV) nie blokuje reszty. Surowe odpowiedzi (przycięte do
podsumowań) zostają w `raw`, więc po zmianie nazw pól u Garmina wystarczy
`sync_garmin --reparse`, bez ponownego pobierania.
"""
import logging
import re
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from ..models import ActivitySettings, DailyHealth

logger = logging.getLogger(__name__)

# Domyślnie odświeżamy dziś i wczoraj (sen „wczoraj w nocy” dochodzi rano),
# nie częściej niż co 2 godziny - cron chodzi co godzinę.
DEFAULT_DAYS = 2
INTERVAL = timedelta(hours=2)
MAX_LIST = 30


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


def _fetch(client, day, with_fitness):
    """Odpowiedzi Garmina dla jednego dnia. Brak źródła = brak klucza."""
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
    for key, call in calls.items():
        try:
            response = call()
        except Exception as exc:
            if exc.__class__.__name__ == 'GarminConnectTooManyRequestsError':
                raise
            logger.info('Garmin: brak %s za %s (%s)', key, iso, exc.__class__.__name__)
            continue
        if key == 'sleep':
            response = {'dailySleepDTO': (response or {}).get('dailySleepDTO') or {}}
        elif key == 'hrv':
            response = {'hrvSummary': (response or {}).get('hrvSummary') or {}}
        elif key == 'status':
            response = {name: (response or {}).get(name) for name in ('mostRecentVO2Max', 'mostRecentTrainingStatus')}
        if response:
            raw[key] = trim(response)
    return raw


def save_day(user, day, raw):
    row, _ = DailyHealth.objects.get_or_create(user=user, date=day)
    merged = dict(row.raw or {})
    merged.update(raw)
    row.raw = merged
    for name, value in fields_from_raw(merged).items():
        setattr(row, name, value)
    row.save()
    return row


def sync_health(user, client, days=None, force=False):
    """Pobiera ostatnie `days` dni (domyślnie dziś i wczoraj). Wskaźniki formy
    (status, VO2max, prognozy) tylko dla dzisiaj - zmieniają się powoli,
    a historia VO2max jest też w treningach biegowych."""
    settings_row = ActivitySettings.for_user(user)
    now = timezone.now()
    if not force and not days and settings_row.health_synced_at and now - settings_row.health_synced_at < INTERVAL:
        return 0
    today = timezone.localdate()
    count = 0
    for offset in range(days or DEFAULT_DAYS):
        day = today - timedelta(days=offset)
        raw = _fetch(client, day, with_fitness=offset == 0)
        if raw:
            save_day(user, day, raw)
            count += 1
    settings_row.health_synced_at = now
    settings_row.save(update_fields=['health_synced_at'])
    return count


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
