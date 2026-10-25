"""Harmonogram dnia: stoper, oś dnia, podsumowania i wpisy z zegarka.

Wpisy mogą na siebie zachodzić (zegarek dopisał trening w środku „Pracy”).
Przy liczeniu czasu wygrywa wpis, który zaczął się później - `flatten` tnie
dzień na rozłączne odcinki, więc żadna minuta nie liczy się podwójnie.
"""
import datetime
import unicodedata
from collections import defaultdict

from django.db import transaction
from django.utils import timezone

from .models import Category, Entry, TimelogSettings

# Krótszy odcinek przy przełączaniu stopera to pomyłka w kliknięciu:
# zamiast osobnego wpisu zmieniamy kategorię biegnącego.
MISCLICK = datetime.timedelta(seconds=60)
# Dłuższy „sen” z zegarka to błąd danych, nie noc.
MAX_SLEEP = datetime.timedelta(hours=16)

DEFAULT_CATEGORIES = [
    # nazwa, ikona, kolor, ocena, automatycznie
    ('Praca', 'bi-briefcase', 1, Category.GOOD, ''),
    ('Nauka', 'bi-book', 6, Category.GOOD, ''),
    ('Sport', 'bi-activity', 3, Category.GOOD, Category.SPORT),
    ('Rodzina i znajomi', 'bi-people', 5, Category.GOOD, ''),
    ('Sen', 'bi-moon-stars', 7, Category.NEUTRAL, Category.SLEEP),
    ('Dom i obowiązki', 'bi-house', 4, Category.NEUTRAL, ''),
    ('Jedzenie', 'bi-cup-hot', 4, Category.NEUTRAL, ''),
    ('Dojazdy', 'bi-car-front', 2, Category.NEUTRAL, ''),
    ('Odpoczynek', 'bi-tree', 3, Category.NEUTRAL, ''),
    ('Telefon i social media', 'bi-phone', 8, Category.WASTE, ''),
    ('Seriale i gry', 'bi-tv', 8, Category.WASTE, ''),
]


def ensure_categories(user):
    """Pierwsze wejście: gotowy zestaw kategorii do przerobienia na swój."""
    if not Category.objects.filter(user=user).exists():
        Category.objects.bulk_create([
            Category(user=user, name=name, icon=icon, color=color, value=value, auto=auto, position=i)
            for i, (name, icon, color, value, auto) in enumerate(DEFAULT_CATEGORIES)
        ])


def now():
    return timezone.now().replace(microsecond=0)


def day_bounds(day):
    """Początek i koniec dnia w strefie lokalnej (doba przy zmianie czasu ma 23 albo 25 h)."""
    start = timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))
    end = timezone.make_aware(datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time.min))
    return start, end


def local_time(day, value):
    return timezone.make_aware(datetime.datetime.combine(day, value))


def normalize(text):
    """Nazwa bez wielkości liter i polskich znaków - Skrót może wysłać „telefon”."""
    text = (text or '').strip().lower().replace('ł', 'l')
    return ''.join(ch for ch in unicodedata.normalize('NFKD', text) if not unicodedata.combining(ch))


def find_category(user, name_or_id):
    categories = Category.objects.filter(user=user, is_archived=False)
    if str(name_or_id).isdigit():
        return categories.filter(pk=int(name_or_id)).first()
    wanted = normalize(name_or_id)
    return next((c for c in categories if normalize(c.name) == wanted), None)


# --- Stoper ---------------------------------------------------------------

def running_entry(user):
    return Entry.objects.filter(user=user, end__isnull=True).select_related('category').order_by('-start').first()


def _close(entry, at):
    if at <= entry.start:
        entry.delete()
    else:
        entry.end = at
        entry.save(update_fields=['end'])


@transaction.atomic
def start_timer(user, category, *, source=Entry.TIMER, at=None, note=''):
    """Kończy biegnący wpis i zaczyna nowy. Ta sama kategoria = nic się nie dzieje."""
    at = at or now()
    running = list(Entry.objects.select_for_update().filter(user=user, end__isnull=True).order_by('-start'))
    current = running[0] if running else None
    if current and current.category_id == category.pk:
        return current
    if current and at - current.start < MISCLICK:
        current.category = category
        current.note = note or current.note
        current.save(update_fields=['category', 'note'])
        return current
    for entry in running:
        _close(entry, at)
    return Entry.objects.create(user=user, category=category, start=at, source=source, note=note)


@transaction.atomic
def stop_timer(user, at=None):
    at = at or now()
    stopped = None
    for entry in Entry.objects.select_for_update().filter(user=user, end__isnull=True).order_by('-start'):
        _close(entry, at)
        stopped = stopped or entry
    return stopped


# --- Liczenie czasu -------------------------------------------------------

def flatten(entries, lo, hi, *, until=None):
    """Rozłączne odcinki [(początek, koniec, wpis)] w przedziale [lo, hi).

    Biegnący wpis kończy się „teraz” (`until`). Gdzie wpisy się nakładają,
    wygrywa ten, który zaczął się później.
    """
    until = until or now()
    items = []
    for entry in entries:
        start = max(entry.start, lo)
        end = min(entry.end or until, hi)
        if start < end:
            items.append((start, end, entry))
    points = sorted({p for start, end, _ in items for p in (start, end)})
    segments = []
    for a, b in zip(points, points[1:]):
        covering = [entry for start, end, entry in items if start <= a and end >= b]
        if not covering:
            continue
        top = max(covering, key=lambda e: (e.start, e.pk or 0))
        if segments and segments[-1][2] is top and segments[-1][1] == a:
            segments[-1] = (segments[-1][0], b, top)
        else:
            segments.append((a, b, top))
    return segments


def gaps(segments, lo, hi):
    """Odcinki bez żadnego wpisu - do uzupełnienia jednym kliknięciem."""
    result = []
    cursor = lo
    for start, end, _ in segments:
        if start > cursor:
            result.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < hi:
        result.append((cursor, hi))
    return [(a, b) for a, b in result if b - a >= datetime.timedelta(minutes=1)]


def entries_between(user, lo, hi):
    return list(
        Entry.objects.filter(user=user, start__lt=hi)
        .exclude(end__lte=lo)
        .select_related('category')
        .order_by('start', 'id')
    )


def day_data(user, day):
    """Oś jednego dnia: wpisy, luki, odcinki do paska i sumy kategorii."""
    lo, hi = day_bounds(day)
    current = now()
    entries = entries_between(user, lo, hi)
    # Liczy się to, co już było - wpis zaplanowany na wieczór nie zakrywa luk.
    visible_end = min(hi, max(lo, current))
    segments = flatten(entries, lo, visible_end, until=current)
    span = (hi - lo).total_seconds()
    totals = defaultdict(int)
    for start, end, entry in segments:
        totals[entry.category] += int((end - start).total_seconds())
    tracked = sum(totals.values())
    return {
        'entries': entries,
        'gaps': gaps(segments, lo, visible_end),
        'bars': [
            {
                'left': round((start - lo).total_seconds() / span * 100, 2),
                'width': round((end - start).total_seconds() / span * 100, 2),
                'entry': entry,
                'start': start,
                'end': end,
            }
            for start, end, entry in segments
        ],
        'totals': sorted(totals.items(), key=lambda item: -item[1]),
        'tracked': tracked,
        'untracked': max(0, int((visible_end - lo).total_seconds()) - tracked),
        'lo': lo,
        'hi': hi,
        'now_pct': round((current - lo).total_seconds() / span * 100, 2) if lo <= current < hi else None,
        # Podziałka co 3 h według zegara - przy zmianie czasu doba ma 23 albo 25 h.
        'scale': [
            (hour, round(((local_time(day, datetime.time(hour)) if hour < 24 else hi) - lo).total_seconds() / span * 100, 2))
            for hour in range(0, 25, 3)
        ],
    }


def period_summary(user, first, last):
    """Sumy kategorii i dni w okresie [first, last] (dni w przyszłości pomija)."""
    current = now()
    lo, _ = day_bounds(first)
    _, hi = day_bounds(last)
    entries = entries_between(user, lo, hi)
    totals = defaultdict(int)
    days = []
    elapsed = 0
    day = first
    while day <= last:
        day_lo, day_hi = day_bounds(day)
        if day_lo >= current:
            break
        day_entries = [e for e in entries if e.start < day_hi and (e.end is None or e.end > day_lo)]
        segments = flatten(day_entries, day_lo, min(day_hi, current), until=current)
        span = (day_hi - day_lo).total_seconds()
        day_seconds = int((min(day_hi, current) - day_lo).total_seconds())
        elapsed += day_seconds
        tracked = 0
        for start, end, entry in segments:
            seconds = int((end - start).total_seconds())
            totals[entry.category] += seconds
            tracked += seconds
        days.append({
            'day': day,
            'tracked': tracked,
            'untracked': max(0, day_seconds - tracked),
            'bars': [
                {'left': round((s - day_lo).total_seconds() / span * 100, 2),
                 'width': round((e - s).total_seconds() / span * 100, 2),
                 'entry': entry, 'start': s, 'end': e}
                for s, e, entry in segments
            ],
        })
        day += datetime.timedelta(days=1)
    tracked = sum(totals.values())
    by_value = defaultdict(int)
    for category, seconds in totals.items():
        by_value[category.value] += seconds
    sleep = sum(seconds for category, seconds in totals.items() if category.auto == Category.SLEEP)
    return {
        'totals': dict(totals),
        'days': days,
        'tracked': tracked,
        'untracked': max(0, elapsed - tracked),
        'elapsed_days': len(days),
        'by_value': dict(by_value),
        'awake': tracked - sleep,
    }


# --- Wpisy z zegarka (Nawyki) ---------------------------------------------

def _sleep_window(health):
    dto = ((health.raw or {}).get('sleep') or {}).get('dailySleepDTO') or {}
    start_ms, end_ms = dto.get('sleepStartTimestampGMT'), dto.get('sleepEndTimestampGMT')
    if not isinstance(start_ms, (int, float)) or not isinstance(end_ms, (int, float)):
        return None
    start = datetime.datetime.fromtimestamp(start_ms / 1000, tz=datetime.timezone.utc).replace(microsecond=0)
    end = datetime.datetime.fromtimestamp(end_ms / 1000, tz=datetime.timezone.utc).replace(microsecond=0)
    if not (start < end <= start + MAX_SLEEP):
        return None
    return start, end


def _trim_forgotten(user, sleep_start):
    """Stoper zostawiony na noc („Telefon” od 23:00 do rana) kończy się
    z chwilą zaśnięcia. Wpisów wpisanych ręcznie nie rusza."""
    forgotten = Entry.objects.filter(
        user=user, source__in=[Entry.TIMER, Entry.SHORTCUT], start__lt=sleep_start,
    ).exclude(end__lte=sleep_start)
    forgotten.update(end=sleep_start)


@transaction.atomic
def import_automatic(user, first, last):
    """Sen (DailyHealth) i treningi (Workout) z Nawyków jako wpisy w dniach
    [first, last]. Idempotentne - można wołać przy każdym otwarciu strony."""
    from habits.models import DailyHealth, Workout

    auto = {c.auto: c for c in Category.objects.filter(user=user, is_archived=False).exclude(auto='')}
    if not auto:
        return 0
    dismissed = set(TimelogSettings.for_user(user).dismissed)
    existing = {e.external_id: e for e in Entry.objects.filter(user=user, external_id__isnull=False)
                .filter(start__date__gte=first - datetime.timedelta(days=1), start__date__lte=last + datetime.timedelta(days=1))}
    changed = 0

    def upsert(external_id, category, start, end, note):
        nonlocal changed
        if external_id in dismissed:
            return
        entry = existing.get(external_id) or Entry.objects.filter(user=user, external_id=external_id).first()
        if entry is None:
            Entry.objects.create(user=user, category=category, start=start, end=end, note=note,
                                 source=Entry.GARMIN, external_id=external_id)
            changed += 1
        elif entry.source == Entry.GARMIN and (entry.start, entry.end, entry.category_id) != (start, end, category.pk):
            entry.start, entry.end, entry.category = start, end, category
            entry.save(update_fields=['start', 'end', 'category'])
            changed += 1

    if Category.SLEEP in auto:
        for health in DailyHealth.objects.filter(user=user, date__gte=first, date__lte=last):
            window = _sleep_window(health)
            if window:
                _trim_forgotten(user, window[0])
                upsert(f'sleep:{health.date.isoformat()}', auto[Category.SLEEP], *window, '')

    if Category.SPORT in auto:
        seen = set()
        workouts = Workout.objects.filter(user=user, date__gte=first, date__lte=last, start_time__isnull=False)
        for workout in workouts:
            external_id = f'workout:{workout.uid}'
            if workout.is_deleted or not workout.duration_s:
                continue
            seen.add(external_id)
            start = local_time(workout.date, workout.start_time.replace(microsecond=0))
            end = start + datetime.timedelta(seconds=workout.duration_s)
            upsert(external_id, auto[Category.SPORT], start, end, workout.title or workout.get_kind_display())
        # Trening usunięty w Nawykach znika też stąd (chyba że wpis był edytowany).
        stale = [e for key, e in existing.items()
                 if key.startswith('workout:') and key not in seen and e.source == Entry.GARMIN
                 and first <= timezone.localdate(e.start) <= last]
        for entry in stale:
            entry.delete()
            changed += 1
    return changed
