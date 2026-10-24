"""Pobieranie treningów z Garmin Connect (nieoficjalna biblioteka garminconnect).

Każdy domownik łączy własne konto na stronie /habits/garmin/ (albo komendą
`manage.py garmin_login`). Hasło do Garmina nie jest nigdzie zapisywane:
logowanie zapisuje tylko tokeny w `GARMIN_TOKEN_DIR/<id osoby>/`, a cron
(`manage.py sync_garmin`) i przycisk na stronie używają już tylko ich.
Biblioteka nie jest oficjalnym API - po zmianach u Garmina potrafi przestać
działać; wtedy błąd trafia do `ActivitySettings.garmin_error` i na stronę.
"""
import logging
import os
import shutil
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone

from ..models import ActivitySettings, Workout, WorkoutRoute, WorkoutSet
from .merge import find_match

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 30
# Historia pobierana zaraz po połączeniu konta.
IMPORT_DAYS = 365
HEALTH_IMPORT_DAYS = 30
# Okrążenia i trasa to osobne zapytania - dobieramy je po kilka na
# synchronizację (od najnowszych), żeby rok historii nie zablokował konta.
EXTRAS_PER_RUN = 8
ROUTE_MAX_POINTS = 400
MAX_LAPS = 100
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


def _prepare_token_dir(user):
    path = token_dir(user)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _connected(user, client):
    """Zapis tokenów po udanym logowaniu. W trybie return_on_mfa biblioteka
    sama ich nie zapisuje; później odświeża je już w tym samym pliku."""
    client.client.dump(str(_prepare_token_dir(user)))
    ActivitySettings.objects.update_or_create(user=user, defaults={'garmin_error': ''})


def friendly_error(exc):
    """Komunikat dla strony: przyczyna po polsku + oryginalny opis z biblioteki
    (bez danych logowania - biblioteka sama go czyści). Sam ogólny komunikat
    nie pozwalał odróżnić złego hasła od zablokowanego konta."""
    if isinstance(exc, GarminError):
        return str(exc)
    name = exc.__class__.__name__
    detail = str(exc).strip()[:200]
    lower = detail.lower()
    if 'mfa' in lower:
        reason = 'Garmin nie przyjął kodu weryfikacyjnego (mógł wygasnąć).'
    elif name == 'GarminConnectAuthenticationError' and 'locked' in lower:
        reason = ('Konto Garmin jest chwilowo zablokowane po kilku nieudanych logowaniach. '
                  'Odczekaj ok. 30 minut albo odblokuj je, logując się na connect.garmin.com.')
    elif name == 'GarminConnectAuthenticationError':
        reason = ('Garmin odrzucił e-mail lub hasło. Sprawdź je, logując się na connect.garmin.com. '
                  'Jeśli logujesz się tam przez Google, Apple albo Facebook, konto nie ma hasła Garmin – '
                  'ustaw je przez „Nie pamiętasz hasła?” i wpisz tutaj.')
    elif name == 'GarminConnectTooManyRequestsError':
        reason = 'Garmin chwilowo blokuje logowanie z tego serwera (zbyt wiele prób). Spróbuj za kilkanaście minut.'
    else:
        reason = 'Nie udało się połączyć z Garminem.'
    return f'{reason} (Garmin: {detail})' if detail else reason


def login(user, email, password, prompt_mfa):
    """Logowanie z hasłem w terminalu (komenda garmin_login)."""
    path = _prepare_token_dir(user)
    client = _garmin_class()(email, password, prompt_mfa=prompt_mfa)
    client.login(str(path))
    return client


def start_login(user, email, password):
    """Pierwszy krok logowania ze strony.

    Zwraca None, gdy konto jest już połączone, albo stan logowania czekającego
    na kod 2FA (słownik do JSON - zawiera ciasteczka sesji Garmina, więc trzymamy
    go w sesji Django tylko do wpisania kodu). Błąd -> GarminError.
    """
    try:
        client = _garmin_class()(email, password, return_on_mfa=True)
        result, state = client.login()
        if result == 'needs_mfa':
            return state
        _connected(user, client)
    except Exception as exc:
        logger.warning('Garmin: logowanie %s nie powiodło się: %s: %s', user, exc.__class__.__name__, str(exc)[:200])
        raise GarminError(friendly_error(exc)) from exc
    return None


def finish_login(user, state, code):
    """Drugi krok: kod 2FA z SMS/e-maila/aplikacji."""
    try:
        client = _garmin_class()()
        client.resume_login(state, code)
        _connected(user, client)
    except Exception as exc:
        logger.warning('Garmin: kod 2FA %s nie przeszedł: %s: %s', user, exc.__class__.__name__, str(exc)[:200])
        raise GarminError(friendly_error(exc)) from exc


def disconnect(user):
    """Usuwa tokeny z dysku. Pobrane treningi zostają."""
    shutil.rmtree(token_dir(user), ignore_errors=True)
    ActivitySettings.objects.update_or_create(user=user, defaults={'garmin_error': '', 'garmin_synced_at': None})


def _import_in_thread(user_pk, days):
    from django.contrib.auth import get_user_model

    close_old_connections()
    try:
        user = get_user_model().objects.get(pk=user_pk)
        sync_all(user, days=days, health_days=HEALTH_IMPORT_DAYS, extras_limit=30)
    except Exception:  # błąd jest już zapisany w ActivitySettings
        logger.warning('Garmin: import historii nie powiódł się', exc_info=True)
    finally:
        close_old_connections()


def start_import(user, days=IMPORT_DAYS):
    """Historia po połączeniu konta - w tle, bo rok treningów (z seriami
    z siłowni) to kilkadziesiąt zapytań, dłużej niż timeout gunicorna."""
    if settings.GARMIN_IMPORT_ASYNC:
        threading.Thread(target=_import_in_thread, args=(user.pk, days), daemon=True).start()
    else:
        _import_in_thread(user.pk, days)


def connect(user):
    """Klient z zapisanych tokenów - bez hasła."""
    if not settings.GARMIN_ENABLED:
        raise GarminError('Pobieranie z Garmina jest wyłączone (GARMIN_ENABLED).')
    if not is_connected(user):
        raise GarminError('Konto Garmin nie jest połączone - połącz je na stronie Aktywność → Garmin.')
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


def _number(value, digits=1):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:   # NaN, brak danych
        return None
    return int(round(number)) if digits == 0 else round(number, digits)


# Pola widoku treningu: klucz na stronie -> pola podsumowania Garmina (pierwsze
# niepuste) i liczba miejsc po przecinku. Nazwy z listy aktywności Garmin Connect;
# brakujące pole po prostu nie pojawia się na stronie.
DETAIL_FIELDS = {
    'maxHr': (['maxHR'], 0),
    'elevationGain': (['elevationGain'], 0),
    'elevationLoss': (['elevationLoss'], 0),
    'cadence': (['averageRunningCadenceInStepsPerMinute', 'averageSwimCadenceInStrokesPerMinute'], 0),
    'aerobicTE': (['aerobicTrainingEffect'], 1),
    'anaerobicTE': (['anaerobicTrainingEffect'], 1),
    'trainingLoad': (['activityTrainingLoad'], 0),
    'vo2max': (['vO2MaxValue'], 1),
    'avgPower': (['avgPower'], 0),
    'maxSpeed': (['maxSpeed'], 2),
    'movingDuration': (['movingDuration'], 0),
    'steps': (['steps'], 0),
    'lengths': (['activeLengths'], 0),
    'swolf': (['averageSwolf'], 0),
    'strokes': (['totalNumberOfStrokes', 'strokes'], 0),
    'avgStrokes': (['avgStrokes', 'averageStrokes'], 1),
}


def parse_details(raw):
    """Podsumowanie aktywności z Garmina -> słownik do widoku treningu."""
    raw = raw or {}
    details = {}
    for key, (names, digits) in DETAIL_FIELDS.items():
        for name in names:
            value = _number(raw.get(name), digits)
            if value is not None:
                details[key] = value
                break
    zones = [_number(raw.get(f'hrTimeInZone_{index}'), 0) or 0 for index in range(1, 6)]
    if any(zones):
        details['hrZones'] = zones
    label = raw.get('trainingEffectLabel')
    if label:
        details['teLabel'] = str(label)[:40]
    pool = _number(raw.get('poolLength'), 2)
    if pool:
        factor = _number((raw.get('unitOfPoolLength') or {}).get('factor'), 2)
        # Garmin podaje długość basenu w centymetrach (factor 100).
        pool = pool / factor if factor else (pool / 100 if pool > 100 else pool)
        details['poolLength'] = round(pool, 1)
    if raw.get('locationName'):
        details['location'] = str(raw['locationName'])[:80]
    # `hasRoute` dopisuje dopiero fetch_extras, gdy trasa naprawdę się pobrała.
    return details


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
        'details': parse_details(activity),
        'garmin_raw': activity,
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


def parse_laps(payload):
    """Okrążenia (lapDTOs) -> [{'d': m, 't': s, 'hr', 'elev', 'cad'}]."""
    laps = []
    for lap in (payload or {}).get('lapDTOs') or []:
        row = {
            'd': _number(lap.get('distance'), 0),
            't': _number(lap.get('duration'), 0),
            'hr': _number(lap.get('averageHR'), 0),
            'elev': _number(lap.get('elevationGain'), 0),
            'cad': _number(lap.get('averageRunCadence'), 0),
        }
        row = {key: value for key, value in row.items() if value is not None}
        if row.get('t'):
            laps.append(row)
    return laps[:MAX_LAPS]


def decode_polyline(text):
    """Polilinia w kodowaniu Google (pole gPolyline) -> [[lat, lng], ...]."""
    points, index, lat, lng = [], 0, 0, 0
    while index < len(text):
        values = []
        for _ in range(2):
            shift = result = 0
            while True:
                byte = ord(text[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            values.append(~(result >> 1) if result & 1 else result >> 1)
        lat += values[0]
        lng += values[1]
        points.append([lat / 1e5, lng / 1e5])
    return points


def _coordinate_pair(lat, lng):
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)) and (lat or lng) \
            and -90 <= lat <= 90 and -180 <= lng <= 180:
        return [lat, lng]
    return None


def parse_route(payload):
    """Trasa z dowolnej z odpowiedzi Garmina, przerzedzona do ROUTE_MAX_POINTS:
    geoPolylineDTO.polyline (mapdetails, details), próbki directLatitude /
    directLongitude (details) albo zakodowana polilinia (polyline)."""
    if isinstance(payload, str):
        payload = {'gPolyline': payload}
    payload = payload if isinstance(payload, dict) else {}
    points = []
    for point in (payload.get('geoPolylineDTO') or {}).get('polyline') or []:
        if isinstance(point, dict):
            pair = _coordinate_pair(point.get('lat', point.get('latitude')), point.get('lon', point.get('lng', point.get('longitude'))))
            if pair:
                points.append(pair)
    if not points:
        indexes = {
            item.get('key'): item.get('metricsIndex')
            for item in payload.get('metricDescriptors') or [] if isinstance(item, dict)
        }
        lat_index, lng_index = indexes.get('directLatitude'), indexes.get('directLongitude')
        if isinstance(lat_index, int) and isinstance(lng_index, int):
            for sample in payload.get('activityDetailMetrics') or []:
                metrics = (sample or {}).get('metrics') or []
                if max(lat_index, lng_index) < len(metrics):
                    pair = _coordinate_pair(metrics[lat_index], metrics[lng_index])
                    if pair:
                        points.append(pair)
    if not points:
        encoded = next((payload.get(key) for key in ('gPolyline', 'encodedPolyline', 'polyline')
                        if isinstance(payload.get(key), str) and payload.get(key)), None)
        if encoded:
            try:
                points = [pair for pair in decode_polyline(encoded) if _coordinate_pair(*pair)]
            except (IndexError, ValueError):
                points = []
    if len(points) > ROUTE_MAX_POINTS:
        step = len(points) / ROUTE_MAX_POINTS
        points = [points[int(i * step)] for i in range(ROUTE_MAX_POINTS)] + [points[-1]]
    return [[round(lat, 5), round(lng, 5)] for lat, lng in points]


# Rodzaje bez GPS: nie pytamy Garmina o trasę.
INDOOR_HINTS = ('indoor', 'treadmill', 'virtual', 'lap_swimming', 'pool', 'elliptical', 'stair',
                'strength', 'yoga', 'pilates', 'cardio', 'fitness_equipment', 'hiit', 'breathwork')


def route_wanted(workout):
    """Czy warto pytać o trasę. Podsumowanie Garmina bywa niepełne (brak
    hasPolyline), więc przy braku informacji próbujemy dla treningów
    z dystansem na zewnątrz."""
    raw = workout.garmin_raw or {}
    if raw.get('hasPolyline') or raw.get('startLatitude') or raw.get('hasMap'):
        return True
    if raw.get('hasPolyline') is False:
        return False
    type_key = (workout.garmin_type or '').lower()
    return bool(workout.distance_m) and workout.kind != Workout.GYM and not any(hint in type_key for hint in INDOOR_HINTS)


def route_sources(client, activity_id):
    """Źródła trasy od najlżejszego; kolejne tylko, gdy poprzednie nic nie dało."""
    return [
        ('mapdetails', lambda: client.get_activity_map_details(activity_id, maxpoly=ROUTE_MAX_POINTS, max_heat_map=0)),
        ('details', lambda: client.get_activity_details(activity_id, maxchart=ROUTE_MAX_POINTS, maxpoly=ROUTE_MAX_POINTS)),
        ('polyline', lambda: client.get_activity_polyline(activity_id)),
    ]


def fetch_route(client, activity_id):
    for name, call in route_sources(client, activity_id):
        try:
            points = parse_route(call())
        except Exception as exc:
            if exc.__class__.__name__ == 'GarminConnectTooManyRequestsError':
                raise
            logger.info('Garmin: trasa %s z %s niedostępna (%s)', activity_id, name, exc.__class__.__name__)
            continue
        if len(points) > 1:
            return points
    return []


def fetch_extras(client, workout):
    """Okrążenia i trasa jednego treningu (od 1 do 4 zapytań)."""
    details = dict(workout.details or {})
    if workout.kind != Workout.GYM:
        laps = parse_laps(client.get_activity_splits(workout.garmin_id))
        if len(laps) > 1:
            details['laps'] = laps
    if route_wanted(workout):
        points = fetch_route(client, workout.garmin_id)
        if points:
            WorkoutRoute.objects.update_or_create(workout=workout, defaults={'points': points})
            details['hasRoute'] = True
        else:
            details.pop('hasRoute', None)
    workout.details = details
    workout.extras_synced = True
    workout.save(update_fields=['details', 'extras_synced', 'updated_at'])


def retry_routes(user=None):
    """Treningi na zewnątrz bez trasy wracają do kolejki dodatków (po poprawce
    pobierania tras). Zwraca ich liczbę."""
    workouts = Workout.objects.filter(garmin_id__isnull=False, is_deleted=False, extras_synced=True, route__isnull=True)
    if user is not None:
        workouts = workouts.filter(user=user)
    pks = [workout.pk for workout in workouts if route_wanted(workout)]
    Workout.objects.filter(pk__in=pks).update(extras_synced=False)
    return len(pks)


def inspect_route(client, workout):
    """Diagnoza dla `sync_garmin --inspect-route`: co zwraca każde źródło trasy
    (klucze i liczba punktów, bez współrzędnych)."""
    raw = workout.garmin_raw or {}
    lines = [
        f'Trening {workout.date} {workout.get_kind_display()} (Garmin {workout.garmin_id}, {workout.garmin_type})',
        f'  podsumowanie: hasPolyline={raw.get("hasPolyline")!r} startLatitude={"jest" if raw.get("startLatitude") else "brak"} '
        f'-> szukamy trasy: {"tak" if route_wanted(workout) else "nie"}',
    ]
    for name, call in route_sources(client, workout.garmin_id):
        try:
            payload = call()
        except Exception as exc:
            lines.append(f'  {name}: błąd {exc.__class__.__name__}: {str(exc)[:150]}')
            continue
        keys = sorted(payload.keys())[:15] if isinstance(payload, dict) else type(payload).__name__
        lines.append(f'  {name}: klucze {keys}, punktów trasy: {len(parse_route(payload))}')
    return lines


def sync_extras(client, user, limit=EXTRAS_PER_RUN):
    """Dobiera okrążenia i trasy dla najnowszych treningów, które ich nie mają."""
    pending = (
        Workout.objects.filter(user=user, garmin_id__isnull=False, extras_synced=False, is_deleted=False)
        .order_by('-date', '-start_time')[:limit]
    )
    done = 0
    for workout in pending:
        try:
            fetch_extras(client, workout)
            done += 1
        except Exception as exc:
            if exc.__class__.__name__ == 'GarminConnectTooManyRequestsError':
                break
            logger.warning('Garmin: brak okrążeń/trasy dla %s', workout.garmin_id, exc_info=True)
            workout.extras_synced = True   # nie próbujemy w nieskończoność
            workout.save(update_fields=['extras_synced'])
    return done


def _attach_to_hevy(user, fields):
    """Siłownia z zegarka w czasie sesji z Hevy: dopisujemy dane zegarka do
    istniejącego treningu (serie zostają z Hevy). True = połączono."""
    candidates = Workout.objects.filter(
        user=user, kind=Workout.GYM, date=fields['date'],
        hevy_id__isnull=False, garmin_id__isnull=True,
    )
    start = datetime.combine(fields['date'], fields['start_time']) if fields['start_time'] else None
    end = start + timedelta(seconds=fields['duration_s'] or 3600) if start else None
    match = find_match(candidates, start, end)
    if match is None:
        return False
    for name in ('garmin_id', 'garmin_type', 'start_time', 'duration_s', 'avg_hr', 'calories', 'details', 'garmin_raw'):
        setattr(match, name, fields[name])
    match.save()
    return True


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
                Workout.objects.filter(user=user, garmin_id__isnull=False)
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
            known.add(fields['garmin_id'])
            if fields['kind'] == Workout.GYM and _attach_to_hevy(user, fields):
                added += 1
                continue
            sets = []
            if fields['kind'] == Workout.GYM:
                try:
                    sets = map_sets(client.get_activity_exercise_sets(fields['garmin_id']))
                except Exception:  # serie są dodatkiem - trening zapisujemy bez nich
                    logger.warning('Garmin: brak serii dla %s', fields['garmin_id'], exc_info=True)
            try:
                with transaction.atomic():
                    workout = Workout.objects.create(user=user, source=Workout.GARMIN, **fields)
                    WorkoutSet.objects.bulk_create([
                        WorkoutSet(workout=workout, position=index, exercise=name, reps=reps, weight_kg=kg)
                        for index, (name, reps, kg) in enumerate(sets)
                    ])
            except IntegrityError:
                # Równoległa synchronizacja (import w tle + przycisk) już go zapisała.
                continue
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


def sync_all(user, days=None, health_days=None, extras_limit=EXTRAS_PER_RUN, force_health=False, client=None):
    """Treningi, potem dodatki (okrążenia, trasy) i zdrowie. Błąd dodatków
    albo zdrowia nie psuje synchronizacji treningów. Zwraca liczbę nowych treningów."""
    from . import garmin_health

    if client is None:
        try:
            client = connect(user)
        except GarminError as exc:
            ActivitySettings.objects.update_or_create(user=user, defaults={'garmin_error': str(exc)[:255]})
            raise
    added = sync_user(user, days=days, client=client)
    try:
        sync_extras(client, user, limit=extras_limit)
    except Exception:
        logger.warning('Garmin: dodatki %s', user, exc_info=True)
    try:
        garmin_health.sync_health(user, client, days=health_days, force=force_health)
    except Exception:
        logger.warning('Garmin: zdrowie %s', user, exc_info=True)
    return added


def reparse(user=None):
    """Przelicza `details` z zapisanych podsumowań (po zmianie DETAIL_FIELDS),
    zachowując okrążenia. Bez zapytań do Garmina."""
    workouts = Workout.objects.filter(garmin_raw__isnull=False)
    if user is not None:
        workouts = workouts.filter(user=user)
    count = 0
    for workout in workouts.iterator():
        details = parse_details(workout.garmin_raw)
        if 'laps' in (workout.details or {}):
            details['laps'] = workout.details['laps']
        if WorkoutRoute.objects.filter(workout=workout).exists():
            details['hasRoute'] = True
        if details != workout.details:
            workout.details = details
            workout.save(update_fields=['details'])
            count += 1
    return count
