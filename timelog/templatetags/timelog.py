from django import template

register = template.Library()


@register.filter
def hm(seconds):
    """Czas trwania: „45 min”, „3 h 05 min”, „12 h”."""
    try:
        minutes = round(int(seconds) / 60)
    except (TypeError, ValueError):
        return ''
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f'{minutes} min'
    return f'{hours} h {minutes:02d} min' if minutes else f'{hours} h'


@register.filter
def hours(seconds):
    """Godziny z jednym miejscem po przecinku: „12,5 h”."""
    try:
        return f'{int(seconds) / 3600:.1f}'.replace('.', ',') + ' h'
    except (TypeError, ValueError):
        return ''


@register.filter
def duration(entry):
    """Długość wpisu w sekundach (biegnący - do teraz)."""
    from ..services import now
    return int(((entry.end or now()) - entry.start).total_seconds())


@register.filter
def signed_hm(seconds):
    """Zmiana: „+1 h 05 min”, „−20 min”."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ''
    return ('+' if seconds >= 0 else '−') + hm(abs(seconds))
