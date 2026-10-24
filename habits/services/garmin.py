"""Pobieranie treningów z Garmin Connect (nieoficjalna biblioteka garminconnect).

Hasło do Garmina nie jest nigdzie zapisywane: jednorazowe logowanie
(`manage.py garmin_login`) zapisuje tokeny w `GARMIN_TOKEN_DIR/<id osoby>/`,
a cron (`manage.py sync_garmin`) i przycisk na stronie używają już tylko ich.
Biblioteka nie jest oficjalnym API - po zmianach u Garmina potrafi przestać
działać; wtedy błąd trafia do `ActivitySettings.garmin_error` i na stronę.
"""
import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import ActivitySettings, Workout, WorkoutSet

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 30
OVERLAP_DAYS = 3

# Rodzaje aktywności Garmina (activityType.typeKey), których nie pobieramy:
# codzienne spacery zawyżałyby cel tygodniowy.
SKIPPED_TYPES = {'walking', 'casual_walking', 'speed_walking'}

# Kategorie ćwiczeń siłowych z zegarka -> nazwy na stronie.
EXERCISE_NAMES = {
    'BENCH_PRESS': 'Wyciskanie na ławce',
    'SQUAT': 'Przysiad',
    'DEADLIFT': 'Martwy ciąg',
    'PULL_UP': 'Podciąganie',
    'PUSH_UP': 'Pompki',
    'ROW': 'Wiosłowanie',
    'SHOULDER_PRESS': 'Wyciskanie nad głowę',
    'CURL': 'Uginanie ramion',
    'TRICEPS_EXTENSION': 'Prostowanie ramion',
    'LATERAL_RAISE': 'Wznosy bokiem',
    'LUNGE': 'Wykroki',
    'LEG_CURL': 'Uginanie nóg',
    'CALF_RAISE': 'Wspięcia na palce',
    'HIP_RAISE': 'Unoszenie bioder',
    'HIP_STABILITY': 'Stabilizacja bioder',
    'CHEST_FLY': 'Rozpiętki',
    'CRUNCH': 'Brzuszki',
    'PLANK': 'Deska',
    'CORE': 'Mięśnie brzucha',
    'SHRUG': 'Szrugsy',
    'CARRY': 'Spacer farmera',
    'SIT_UP': 'Brzuszki',
}
EQUIPMENT_NAMES = {
    'BARBELL': 'sztanga',
    'DUMBBELL': 'hantle',
    'KETTLEBELL': 'kettlebell',
    'CABLE': 'wyciąg',
    'MACHINE': 'maszyna',
    'SMITH_MACHINE': 'suwnica Smitha',
    'WEIGHTED': 'z obciążeniem',
}


class GarminError(Exception):
    pass


def token_dir(user):
    return Path(settings.GARMIN_TOKEN_DIR) / str(user.pk)


def is_connected(user):
    path = token_dir(user)
    return path.is_dir() and any(path.iterdir())


def status(user, settings_row=None):
    settings_row = settings_row or ActivitySettings.for_user(user)
    return {
        'enabled': settings.GARMIN_ENABLED,
        'connected': settings.GARMIN_ENABLED and is_connected(user),
        'syncedAt': settings_row.garmin_synced_at.isoformat() if settings_row.garmin_synced_at else None,
        'error': settings_row.garmin_error,
    }


def _garmin_class():
    try:
        from garminconnect import Garmin
    except ImportError as exc:  # pragma: no cover - zależność tylko w obrazie
        raise GarminError('Brak biblioteki garminconnect (pip install garminconnect).') from exc
    return Garmin


def login(user, email, password, prompt_mfa):
    """Logowanie z hasłem (komenda garmin_login). Zapisuje tokeny na dysku."""
    path = token_dir(user)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    client = _garmin_class()(email, password, prompt_mfa=prompt_mfa)
    client.login(str(path))
    return client


def connect(user):
    """Klient z zapisanych tokenów - bez hasła."""
    if not settings.GARMIN_ENABLED:
        raise GarminError('Pobieranie z Garmina jest wyłączone (GARMIN_ENABLED).')
    if not is_connected(user):
        raise GarminError('Brak połączenia z Garminem - uruchom manage.py garmin_login.')
    client = _garmin_class()()
    try:
        client.login(str(token_dir(user)))
    except Exception as exc:
        raise GarminError(f'Garmin odrzucił zapisane logowanie - zaloguj się ponownie ({exc}).') from exc
    return client


def workout_kind(type_key):
    type_key = (type_key or '').lower()
    if type_key in SKIPPED_TYPES:
        return None
    if 'swim' in type_key:
        return Workout.SWIM
    if 'run' in type_key:
        return Workout.RUN
    if type_key == 'strength_training':
        return Workout.GYM
    return Workout.OTHER


def _positive_int(value):
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def map_activity(activity):
    """Aktywność z listy Garmina -> pola Workout. None = pomijamy."""
    type_key = (activity.get('activityType') or {}).get('typeKey', '')
    kind = workout_kind(type_key)
    activity_id = activity.get('activityId')
    started = str(activity.get('startTimeLocal') or '')
    if kind is None or not activity_id or not started:
        return None
    try:
        start = datetime.fromisoformat(started.replace('T', ' ')[:19])
    except ValueError:
        return None
    return {
        'garmin_id': int(activity_id),
        'garmin_type': type_key[:60],
        'kind': kind,
        'date': start.date(),
        'start_time': start.time().replace(second=0, microsecond=0),
        'title': str(activity.get('activityName') or '')[:120],
        'duration_s': _positive_int(activity.get('duration')),
        # Siłownia z zegarka ma dystans 0 albo krokomierz - pomijamy.
        'distance_m': _positive_int(activity.get('distance')) if kind != Workout.GYM else None,
        'avg_hr': _positive_int(activity.get('averageHR')),
        'calories': _positive_int(activity.get('calories')),
    }


def exercise_name(exercise):
    category = str(exercise.get('category') or '').upper()
    name = str(exercise.get('name') or '').upper()
    base = EXERCISE_NAMES.get(category)
    if base is None:
        source = name or category
        if not source or source == 'UNKNOWN':
            return 'Ćwiczenie'
        return source.replace('_', ' ').capitalize()
    if name and name != category:
        variant = name.replace(category, '').strip('_')
        if variant in EQUIPMENT_NAMES:
            return f'{base} – {EQUIPMENT_NAMES[variant]}'
        if variant:
            return f'{base} – {variant.replace("_", " ").lower()}'
    return base


def map_sets(payload):
    """Serie z get_activity_exercise_sets -> [(ćwiczenie, powtórzenia, kg)].

    Garmin podaje ciężar w gramach; przerwy (setType REST) pomijamy."""
    rows = []
    for item in (payload or {}).get('exerciseSets') or []:
        if str(item.get('setType') or '').upper() != 'ACTIVE':
            continue
        exercises = item.get('exercises') or []
        best = max(exercises, key=lambda e: e.get('probability') or 0) if exercises else {}
        weight = item.get('weight')
        try:
            kg = (Decimal(str(weight)) / 1000).quantize(Decimal('0.01')) if weight and float(weight) > 0 else None
        except (ValueError, ArithmeticError):
            kg = None
        rows.append((exercise_name(best), _positive_int(item.get('repetitionCount')), kg))
    return rows


def sync_user(user, days=None, client=None):
    """Pobiera nowe aktywności. Zwraca liczbę dodanych treningów.

    Bez `days` zaczyna kilka dni przed ostatnim treningiem z Garmina (zegarek
    potrafi wysłać aktywność z opóźnieniem), a przy pierwszym razie - od 30 dni.
    Trening już pobrany (także usunięty na stronie) nie jest nadpisywany,
    więc poprawki wpisane ręcznie zostają.
    """
    settings_row = ActivitySettings.for_user(user)
    try:
        client = client or connect(user)
        today = timezone.localdate()
        if days:
            start = today - timedelta(days=days)
        else:
            last = (
                Workout.objects.filter(user=user, source=Workout.GARMIN)
                .order_by('-date').values_list('date', flat=True).first()
            )
            start = (last - timedelta(days=OVERLAP_DAYS)) if last else today - timedelta(days=DEFAULT_DAYS)
        activities = client.get_activities_by_date(start.isoformat(), today.isoformat()) or []
        known = set(
            Workout.objects.filter(garmin_id__isnull=False).values_list('garmin_id', flat=True)
        )
        added = 0
        for activity in activities:
            fields = map_activity(activity)
            if fields is None or fields['garmin_id'] in known:
                continue
            sets = []
            if fields['kind'] == Workout.GYM:
                try:
                    sets = map_sets(client.get_activity_exercise_sets(fields['garmin_id']))
                except Exception:  # serie są dodatkiem - trening zapisujemy bez nich
                    logger.warning('Garmin: brak serii dla %s', fields['garmin_id'], exc_info=True)
            with transaction.atomic():
                workout = Workout.objects.create(user=user, source=Workout.GARMIN, **fields)
                WorkoutSet.objects.bulk_create([
                    WorkoutSet(workout=workout, position=index, exercise=name, reps=reps, weight_kg=kg)
                    for index, (name, reps, kg) in enumerate(sets)
                ])
            known.add(fields['garmin_id'])
            added += 1
    except Exception as exc:
        logger.warning('Garmin: synchronizacja %s nie powiodła się', user, exc_info=True)
        settings_row.garmin_error = str(exc)[:255] or exc.__class__.__name__
        settings_row.save(update_fields=['garmin_error'])
        raise GarminError(settings_row.garmin_error) from exc
    settings_row.garmin_synced_at = timezone.now()
    settings_row.garmin_error = ''
    settings_row.save(update_fields=['garmin_synced_at', 'garmin_error'])
    return added
