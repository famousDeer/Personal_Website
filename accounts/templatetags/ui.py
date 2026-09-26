"""Wspólne elementy interfejsu dla wszystkich modułów.

    {% load ui %}
    {% page_head title="Wydatki" eyebrow="Finanse" icon="bi-cash-coin" sub="..." back=url %}
        <a class="btn btn-primary" href="...">Dodaj</a>
    {% endpage_head %}

Treść bloku to akcje po prawej stronie nagłówka (może być pusta).
"""
from django import template
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

register = template.Library()


@register.simple_block_tag
def page_head(content, title, eyebrow='', sub='', icon='', back='', back_label='Wróć'):
    actions = content.strip()
    return render_to_string('partials/page_head.html', {
        'title': title,
        'eyebrow': eyebrow,
        'sub': sub,
        'icon': icon,
        'back': back,
        'back_label': back_label,
        'actions': mark_safe(actions) if actions else '',
    })


def _plural_form(count, forms):
    one, few, many = (part.strip() for part in forms.split(','))
    if count == 1:
        return one
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return few
    return many


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@register.filter
def pl(value, forms):
    """Liczba z odmienionym rzeczownikiem: ``{{ n|pl:"rachunek,rachunki,rachunków" }}``.

    1 rachunek, 2 rachunki, 5 rachunków, 22 rachunki, 12 rachunków.
    Formy podaje się w kolejności: jeden, kilka (2-4), wiele.
    """
    count = _as_int(value)
    if count is None:
        return value
    return f'{count} {_plural_form(abs(count), forms)}'


@register.filter
def pl_word(value, forms):
    """Sam odmieniony rzeczownik, gdy liczba stoi w osobnym elemencie."""
    count = _as_int(value)
    if count is None:
        return forms.split(',')[-1].strip()
    return _plural_form(abs(count), forms)


@register.filter
def undo_token(message):
    """Token „Cofnij” z komunikatu utworzonego przez ``utils.undo.delete_with_undo``."""
    from utils.undo import undo_token as _undo_token

    return _undo_token(message)
