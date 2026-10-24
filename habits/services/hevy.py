"""Treningi siłowe z Hevy: publiczne API (Hevy Pro) albo plik CSV z eksportu.

API: https://api.hevyapp.com/docs/ - nagłówek `api-key`, zmiany pobieramy
przez /v1/workouts/events?since=... (dodane, zmienione i usunięte treningi),
po 10 na stronę. Klucz każda osoba wkleja na stronie Połączenia.

Sesja z Hevy nakładająca się w czasie z siłownią z zegarka Garmin staje się
jednym treningiem (services/merge.py): serie z Hevy, czas i tętno z zegarka.
"""
import csv
import hashlib
import re
import io
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from ..models import ActivitySettings, Workout, WorkoutSet
from .merge import find_match

logger = logging.getLogger(__name__)

API_URL = 'https://api.hevyapp.com/v1'
PAGE_SIZE = 10
MAX_PAGES = 200
SET_TYPES = {'warmup', 'failure', 'dropset'}
LBS_TO_KG = Decimal('0.45359237')
MAX_CSV_BYTES = 10 * 1024 * 1024


class HevyError(Exception):
    pass


def status(user, settings_row=None):
    settings_row = settings_row or ActivitySettings.for_user(user)
    return {
        'enabled': settings.HEVY_ENABLED,
        'connected': bool(settings_row.hevy_api_key),
        'syncedAt': settings_row.hevy_synced_at.isoformat() if settings_row.hevy_synced_at else None,
        'error': settings_row.hevy_error,
    }


# --- API -------------------------------------------------------------------
def request(api_key, path, params=None):
    if not settings.HEVY_ENABLED:
        raise HevyError('Pobieranie z Hevy jest wyłączone (HEVY_ENABLED).')
    url = f'{API_URL}{path}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        'api-key': api_key,
        'Accept': 'application/json',
        'User-Agent': 'Website-Finance (domowa strona aktywności)',
    })
    try:
        with urllib.request.urlopen(req, timeout=settings.HEVY_TIMEOUT) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise HevyError('Hevy nie przyjął klucza API. Klucz działa tylko z Hevy Pro – sprawdź go na hevy.com/settings?developer.') from exc
        if exc.code == 429:
            raise HevyError('Hevy chwilowo ogranicza liczbę zapytań – spróbuj za kilka minut.') from exc
        raise HevyError(f'Hevy odpowiedział błędem {exc.code}.') from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HevyError(f'Brak połączenia z Hevy ({exc}).') from exc
    except ValueError as exc:
        raise HevyError('Hevy zwrócił nieczytelną odpowiedź.') from exc


def check_key(api_key):
    """Sprawdza klucz zapytaniem o konto. Błąd -> HevyError."""
    request(api_key, '/user/info')


def _local(value):
    """'2026-10-20T16:02:11Z' -> naiwna data i godzina w strefie strony."""
    try:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    if timezone.is_aware(moment):
        moment = timezone.localtime(moment).replace(tzinfo=None)
    return moment


def _decimal(value, factor=None):
    if value in (None, ''):
        return None
    try:
        number = Decimal(str(value).replace(',', '.'))
    except InvalidOperation:
        return None
    if factor is not None:
        number *= factor
    number = number.quantize(Decimal('0.01'))
    return number if number > 0 else None


def _int(value):
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_workout(data):
    """Trening z API Hevy -> pola Workout + serie albo None."""
    start = _local(data.get('start_time'))
    end = _local(data.get('end_time'))
    if not data.get('id') or start is None:
        return None
    sets = []
    for exercise in sorted(data.get('exercises') or [], key=lambda e: e.get('index') or 0):
        title = str(exercise.get('title') or '').strip()[:80] or 'Ćwiczenie'
        for item in sorted(exercise.get('sets') or [], key=lambda s: s.get('index') or 0):
            set_type = 'warmup' if title.lower() in WARMUP_EXERCISES else str(item.get('type') or '').lower()
            sets.append({
                'exercise': title,
                'reps': _int(item.get('reps')),
                'weight_kg': _decimal(item.get('weight_kg')),
                'duration_s': _int(item.get('duration_seconds')),
                'set_type': set_type if set_type in SET_TYPES else '',
            })
    return {
        'hevy_id': str(data['id'])[:64],
        'start': start,
        'end': end if end and end > start else None,
        'title': str(data.get('title') or '').strip()[:120],
        'notes': str(data.get('description') or '').strip()[:2000],
        'sets': sets,
    }


def _replace_sets(workout, sets):
    workout.sets.all().delete()
    WorkoutSet.objects.bulk_create([
        WorkoutSet(workout=workout, position=index, **row) for index, row in enumerate(sets)
    ])


def save_parsed(user, parsed):
    """Zapisuje trening z Hevy: aktualizacja po hevy_id, połączenie z siłownią
    z zegarka albo nowy trening. Zwraca (trening, czy_nowy)."""
    start, end = parsed['start'], parsed['end']
    duration = int((end - start).total_seconds()) if end else None
    with transaction.atomic():
        workout = Workout.objects.select_for_update().filter(hevy_id=parsed['hevy_id']).first()
        if workout is not None and workout.user_id != user.pk:
            return None, False
        if workout is None and not parsed['hevy_id'].startswith('csv:'):
            # Ten sam trening wcześniej wgrany z pliku CSV - przejmujemy go.
            workout = Workout.objects.filter(
                user=user, hevy_id__startswith='csv:', date=start.date(),
                start_time=start.time().replace(second=0, microsecond=0),
            ).first()
        created = False
        if workout is None:
            candidates = Workout.objects.filter(
                user=user, kind=Workout.GYM, date=start.date(),
                garmin_id__isnull=False, hevy_id__isnull=True,
            )
            workout = find_match(candidates, start, end or start + timedelta(hours=1))
        if workout is None:
            workout = Workout(
                user=user, kind=Workout.GYM, source=Workout.HEVY, date=start.date(),
                start_time=start.time().replace(second=0, microsecond=0), duration_s=duration,
            )
            created = True
        elif workout.garmin_id is None:
            # Bez zegarka czas pochodzi z Hevy.
            workout.date = start.date()
            workout.start_time = start.time().replace(second=0, microsecond=0)
            workout.duration_s = duration
        workout.hevy_id = parsed['hevy_id']
        workout.title = parsed['title'] or workout.title
        if parsed['notes'] or created:
            workout.notes = parsed['notes']
        workout.save()
        _replace_sets(workout, parsed['sets'])
    return workout, created


def remove(user, hevy_id):
    """Trening usunięty w Hevy. Połączony z zegarkiem zostaje (bez serii z Hevy)."""
    workout = Workout.objects.filter(user=user, hevy_id=hevy_id).first()
    if workout is None:
        return
    if workout.garmin_id:
        workout.hevy_id = None
        workout.save(update_fields=['hevy_id', 'updated_at'])
        workout.sets.all().delete()
    else:
        workout.delete()


def sync_user(user):
    """Pobiera zmiany od ostatniej synchronizacji. Zwraca liczbę nowych treningów."""
    settings_row = ActivitySettings.for_user(user)
    if not settings_row.hevy_api_key:
        raise HevyError('Hevy nie jest połączone – wklej klucz API na stronie Połączenia.')
    started = timezone.now()
    # Zapas 10 minut: zmiana zapisana w trakcie poprzedniego pobierania nie przepadnie.
    since = (settings_row.hevy_synced_at - timedelta(minutes=10)) if settings_row.hevy_synced_at else None
    params = {'pageSize': PAGE_SIZE, 'since': since.isoformat().replace('+00:00', 'Z') if since else '1970-01-01T00:00:00Z'}
    added = 0
    try:
        page = 1
        while page <= MAX_PAGES:
            data = request(settings_row.hevy_api_key, '/workouts/events', {**params, 'page': page})
            for event in data.get('events') or []:
                if event.get('type') == 'deleted' and event.get('id'):
                    remove(user, str(event['id']))
                elif event.get('type') == 'updated' and event.get('workout'):
                    parsed = parse_workout(event['workout'])
                    if parsed:
                        added += save_parsed(user, parsed)[1]
            if page >= (data.get('page_count') or 1):
                break
            page += 1
    except HevyError as exc:
        settings_row.hevy_error = str(exc)[:255]
        settings_row.save(update_fields=['hevy_error'])
        raise
    settings_row.hevy_synced_at = started
    settings_row.hevy_error = ''
    settings_row.save(update_fields=['hevy_synced_at', 'hevy_error'])
    return added


def connect(user, api_key):
    api_key = api_key.strip()
    check_key(api_key)
    ActivitySettings.objects.update_or_create(user=user, defaults={
        'hevy_api_key': api_key, 'hevy_synced_at': None, 'hevy_error': '',
    })


def disconnect(user):
    ActivitySettings.objects.update_or_create(user=user, defaults={
        'hevy_api_key': '', 'hevy_synced_at': None, 'hevy_error': '',
    })


def _sync_in_thread(user_pk):
    from django.contrib.auth import get_user_model

    close_old_connections()
    try:
        sync_user(get_user_model().objects.get(pk=user_pk))
    except Exception:
        logger.warning('Hevy: import historii nie powiódł się', exc_info=True)
    finally:
        close_old_connections()


def start_import(user):
    """Cała historia po wklejeniu klucza - w tle (10 treningów na zapytanie)."""
    if settings.GARMIN_IMPORT_ASYNC:
        threading.Thread(target=_sync_in_thread, args=(user.pk,), daemon=True).start()
    else:
        _sync_in_thread(user.pk)


# --- CSV ----------------------------------------------------------------------
CSV_DATE_FORMATS = ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S']
# Hevy zapisuje daty w języku aplikacji: "19 paź 2026, 16:02" albo "19 Oct 2026, 16:02".
# Własne skróty miesięcy zamiast %b - ten zależy od locale serwera.
CSV_MONTHS = {
    'sty': 1, 'lut': 2, 'mar': 3, 'kwi': 4, 'maj': 5, 'cze': 6,
    'lip': 7, 'sie': 8, 'wrz': 9, 'paź': 10, 'paz': 10, 'lis': 11, 'gru': 12,
    'jan': 1, 'feb': 2, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7, 'aug': 8,
    'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}
CSV_TEXT_DATE = re.compile(r'^(\d{1,2})\s+([^\W\d_]+)\.?\s+(\d{4}),?\s+(\d{1,2}):(\d{2})$')
# Ćwiczenie „rozgrzewka” (np. rower przez 2 minuty) to rozgrzewka, choć Hevy
# zapisuje je jako zwykłą serię.
WARMUP_EXERCISES = {'rozgrzewka', 'warm up', 'warm-up', 'warmup'}


def _csv_datetime(value):
    value = str(value or '').strip()
    match = CSV_TEXT_DATE.match(value)
    if match:
        day, month_name, year, hour, minute = match.groups()
        month = CSV_MONTHS.get(month_name.lower()[:3]) or CSV_MONTHS.get(month_name.lower())
        if month:
            try:
                return datetime(int(year), month, int(day), int(hour), int(minute))
            except ValueError:
                return None
    for fmt in CSV_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return _local(value) if value else None


def import_csv(user, uploaded):
    """Plik z Hevy (Ustawienia → Eksportuj dane). Wiersz = seria; treningi
    grupujemy po (tytuł, początek, koniec). Ciężar w kg albo funtach,
    zależnie od jednostek w aplikacji. Zwraca (nowe, zaktualizowane)."""
    if uploaded.size > MAX_CSV_BYTES:
        raise HevyError('Plik jest za duży (limit 10 MB).')
    try:
        text = uploaded.read().decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise HevyError('Plik nie jest tekstem CSV z Hevy.') from exc
    reader = csv.DictReader(io.StringIO(text))
    columns = set(reader.fieldnames or [])
    if not {'title', 'start_time', 'exercise_title'} <= columns:
        raise HevyError('To nie wygląda na eksport z Hevy (brak kolumn title, start_time, exercise_title).')
    weight_column, factor = ('weight_kg', None) if 'weight_kg' in columns else ('weight_lbs', LBS_TO_KG)

    workouts = {}
    unreadable = None
    for row in reader:
        key = (row.get('title', ''), row.get('start_time', ''), row.get('end_time', ''))
        start = _csv_datetime(row.get('start_time'))
        if start is None:
            unreadable = unreadable or row.get('start_time')
            continue
        workout = workouts.get(key)
        if workout is None:
            digest = hashlib.sha1('|'.join(key).encode('utf-8')).hexdigest()[:24]
            end = _csv_datetime(row.get('end_time'))
            workout = workouts[key] = {
                'hevy_id': f'csv:{digest}',
                'start': start,
                'end': end if end and end > start else None,
                'title': str(row.get('title') or '').strip()[:120],
                'notes': str(row.get('description') or '').strip()[:2000],
                'sets': [],
            }
        set_type = str(row.get('set_type') or '').lower()
        exercise = str(row.get('exercise_title') or '').strip()[:80] or 'Ćwiczenie'
        if exercise.lower() in WARMUP_EXERCISES:
            set_type = 'warmup'
        workout['sets'].append({
            'exercise': exercise,
            'reps': _int(row.get('reps')),
            'weight_kg': _decimal(row.get(weight_column), factor),
            'duration_s': _int(row.get('duration_seconds')),
            'set_type': set_type if set_type in SET_TYPES else '',
        })
    if not workouts:
        if unreadable:
            raise HevyError(f'Nie rozpoznaję daty w pliku („{unreadable[:40]}”). Wyślij ten plik – dopiszę ten format.')
        raise HevyError('W pliku nie ma żadnego treningu.')

    created = updated = 0
    for parsed in workouts.values():
        # Trening już pobrany przez API (ten sam początek) - plik go nie dubluje.
        if Workout.objects.filter(
            user=user, hevy_id__isnull=False, date=parsed['start'].date(),
            start_time=parsed['start'].time().replace(second=0, microsecond=0),
        ).exclude(hevy_id__startswith='csv:').exists():
            continue
        workout, is_new = save_parsed(user, parsed)
        if workout is not None:
            created += is_new
            updated += not is_new
    return created, updated
