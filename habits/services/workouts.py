"""Treningi dla aplikacji w telefonie: snapshot danych i zapis kolejki zmian.

Statystyki (seria tygodni, rekordy, kalendarz) liczy przeglądarka
(`static/habits/js/activity-stats.js`), bo muszą działać także dla treningów
dopisanych bez połączenia. Serwer tylko przechowuje i waliduje dane.
"""
from datetime import date, time
from decimal import Decimal, InvalidOperation

from django.db import transaction

from ..models import ActivitySettings, Workout, WorkoutSet
from . import garmin

KINDS = {value for value, _ in Workout.KIND_CHOICES}
MAX_SETS = 200
MAX_OPS = 500


def serialize_workout(workout):
    return {
        'uid': workout.uid,
        'kind': workout.kind,
        'date': workout.date.isoformat(),
        'time': workout.start_time.strftime('%H:%M') if workout.start_time else '',
        'title': workout.title,
        'duration': workout.duration_s,
        'distance': workout.distance_m,
        'avgHr': workout.avg_hr,
        'calories': workout.calories,
        'notes': workout.notes,
        'source': workout.source,
        'sets': [
            [item.exercise, item.reps, float(item.weight_kg) if item.weight_kg is not None else None]
            for item in workout.sets.all()
        ],
    }


def activity_snapshot(user):
    settings_row = ActivitySettings.for_user(user)
    workouts = (
        Workout.objects.filter(user=user, is_deleted=False)
        .prefetch_related('sets')
        .order_by('-date', '-start_time', '-id')
    )
    return {
        'workouts': [serialize_workout(workout) for workout in workouts],
        'settings': {
            'weeklyGoal': settings_row.weekly_goal,
            'garmin': garmin.status(user, settings_row),
        },
    }


def _optional_int(value, field, maximum):
    if value in (None, ''):
        return None
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        raise ValueError(f'Nieprawidłowa wartość pola „{field}”.') from None
    if number < 0 or number > maximum:
        raise ValueError(f'Wartość pola „{field}” jest poza zakresem.')
    return number or None


def _optional_weight(value):
    if value in (None, ''):
        return None
    try:
        weight = Decimal(str(value).replace(',', '.')).quantize(Decimal('0.01'))
    except InvalidOperation:
        raise ValueError('Nieprawidłowy ciężar.') from None
    if weight < 0 or weight > 1000:
        raise ValueError('Ciężar jest poza zakresem.')
    return weight


def clean_workout(data):
    """Sprawdza trening z telefonu. Zwraca (pola, serie) albo rzuca ValueError."""
    if not isinstance(data, dict):
        raise ValueError('Nieprawidłowe dane treningu.')
    uid = str(data.get('uid') or '').strip()
    if not uid or len(uid) > 36:
        raise ValueError('Brak identyfikatora treningu.')
    kind = data.get('kind')
    if kind not in KINDS:
        raise ValueError('Nieznany rodzaj treningu.')
    try:
        day = date.fromisoformat(str(data.get('date') or ''))
    except ValueError:
        raise ValueError('Nieprawidłowa data treningu.') from None
    start = None
    if data.get('time'):
        try:
            start = time.fromisoformat(str(data['time']))
        except ValueError:
            raise ValueError('Nieprawidłowa godzina treningu.') from None

    fields = {
        'kind': kind,
        'date': day,
        'start_time': start,
        'title': str(data.get('title') or '').strip()[:120],
        'duration_s': _optional_int(data.get('duration'), 'czas', 48 * 3600),
        'distance_m': _optional_int(data.get('distance'), 'dystans', 1_000_000),
        'avg_hr': _optional_int(data.get('avgHr'), 'tętno', 250),
        'calories': _optional_int(data.get('calories'), 'kalorie', 20_000),
        'notes': str(data.get('notes') or '').strip()[:2000],
    }

    sets = []
    raw_sets = data.get('sets') or []
    if not isinstance(raw_sets, list) or len(raw_sets) > MAX_SETS:
        raise ValueError('Nieprawidłowa lista serii.')
    for row in raw_sets:
        if not isinstance(row, (list, tuple)) or not row:
            raise ValueError('Nieprawidłowa seria.')
        exercise = str(row[0] or '').strip()[:80]
        if not exercise:
            continue
        reps = _optional_int(row[1] if len(row) > 1 else None, 'powtórzenia', 1000)
        weight = _optional_weight(row[2] if len(row) > 2 else None)
        sets.append((exercise, reps, weight))
    return uid, fields, sets


def save_workout(user, data):
    uid, fields, sets = clean_workout(data)
    with transaction.atomic():
        workout = Workout.objects.select_for_update().filter(uid=uid).first()
        if workout is not None and workout.user_id != user.pk:
            raise ValueError('Ten trening należy do innej osoby.')
        if workout is None:
            workout = Workout(user=user, uid=uid, source=Workout.MANUAL)
        for name, value in fields.items():
            setattr(workout, name, value)
        # Zapis po usunięciu to „Cofnij” - trening wraca.
        workout.is_deleted = False
        workout.save()
        workout.sets.all().delete()
        WorkoutSet.objects.bulk_create([
            WorkoutSet(workout=workout, position=index, exercise=exercise, reps=reps, weight_kg=weight)
            for index, (exercise, reps, weight) in enumerate(sets)
        ])
    return workout


def delete_workout(user, uid):
    workout = Workout.objects.filter(user=user, uid=str(uid or '')).first()
    if workout is None:
        return
    if workout.source == Workout.GARMIN:
        workout.is_deleted = True
        workout.save(update_fields=['is_deleted', 'updated_at'])
    else:
        workout.delete()


def set_weekly_goal(user, value):
    goal = _optional_int(value, 'cel tygodniowy', 7)
    if not goal:
        raise ValueError('Cel tygodniowy to od 1 do 7 dni.')
    ActivitySettings.objects.update_or_create(user=user, defaults={'weekly_goal': goal})


def apply_operations(ops, user):
    """Zapisuje kolejkę zmian z telefonu. Każda operacja osobno: błąd jednej
    (np. zła data) nie blokuje pozostałych, a telefon dostaje komunikat."""
    if not isinstance(ops, list) or len(ops) > MAX_OPS:
        raise ValueError('Nieprawidłowa lista zmian.')
    results = []
    for op in ops:
        op = op if isinstance(op, dict) else {}
        result = {'id': op.get('id'), 'ok': True}
        try:
            kind = op.get('type')
            if kind == 'save':
                save_workout(user, op.get('workout'))
            elif kind == 'delete':
                delete_workout(user, op.get('uid'))
            elif kind == 'goal':
                set_weekly_goal(user, op.get('value'))
            else:
                raise ValueError('Nieznana operacja.')
        except ValueError as exc:
            result = {'id': op.get('id'), 'ok': False, 'message': str(exc)}
        results.append(result)
    return results
