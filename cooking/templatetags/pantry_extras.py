from django import template
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from ..constants import PANTRY_CATEGORY_GROUPS, PANTRY_CATEGORY_OTHER

register = template.Library()


def _option(category, selected):
    return format_html(
        '<option value="{}"{}>{}</option>',
        category,
        mark_safe(' selected') if category == selected else '',
        category,
    )


@register.simple_tag
def pantry_category_options(selected=''):
    """Opcje <select> z kategoriami w grupach "Spożywcze" i "Dom".

    Przy 16 pozycjach płaska lista przestaje być czytelna, a grupa od razu
    mówi, gdzie szukać. Tag zastępuje pętlę powtarzaną w kilkunastu
    szablonach, więc kolejność i grupy są zdefiniowane w jednym miejscu
    (cooking/constants.py).
    """
    selected = str(selected or '')
    groups = format_html_join(
        '',
        '<optgroup label="{}">{}</optgroup>',
        (
            (label, mark_safe(''.join(_option(category, selected) for category in categories)))
            for label, categories in PANTRY_CATEGORY_GROUPS
        ),
    )
    return mark_safe(groups + _option(PANTRY_CATEGORY_OTHER, selected))
