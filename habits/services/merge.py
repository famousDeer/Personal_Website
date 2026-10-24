"""Łączenie tej samej sesji na siłowni z Garmina (zegarek) i z Hevy (aplikacja).

Zegarek zna czas, tętno i kalorie, Hevy - ćwiczenia, serie i ciężary wpisane
ręcznie. Sesje nakładające się w czasie (z zapasem na włączenie zegarka przed
pierwszą serią) stają się jednym treningiem z oboma identyfikatorami.
"""
from datetime import datetime, timedelta

TOLERANCE = timedelta(minutes=20)
DEFAULT_LENGTH = timedelta(minutes=60)


def interval(workout):
    """(początek, koniec) treningu w czasie lokalnym albo None bez godziny."""
    if workout.start_time is None:
        return None
    start = datetime.combine(workout.date, workout.start_time)
    length = timedelta(seconds=workout.duration_s) if workout.duration_s else DEFAULT_LENGTH
    return start, start + length


def overlaps(first, second):
    return first[0] - TOLERANCE <= second[1] and second[0] - TOLERANCE <= first[1]


def find_match(candidates, start, end):
    """Kandydat (ten sam dzień, drugie źródło) nakładający się z [start, end].

    Bez godziny po którejś stronie łączymy tylko wtedy, gdy tego dnia jest
    dokładnie jeden kandydat - lepiej zostawić dwa treningi niż skleić złe.
    """
    candidates = list(candidates)
    if start is None:
        return candidates[0] if len(candidates) == 1 else None
    best = None
    best_gap = None
    for workout in candidates:
        span = interval(workout)
        if span is None:
            if len(candidates) == 1:
                return workout
            continue
        if overlaps(span, (start, end)):
            gap = abs((span[0] - start).total_seconds())
            if best is None or gap < best_gap:
                best, best_gap = workout, gap
    return best
