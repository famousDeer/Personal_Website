from decimal import Decimal

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


@register.filter
def plain_decimal(value):
    """Liczba do pola <input type="number">: 750.00 -> "750", 1.50 -> "1.5".

    Bez polskiego przecinka (pole liczbowe go odrzuca) i bez zbędnych zer.
    Wartości, które nie są liczbą (np. tekst odesłany w formularzu z błędem),
    wracają bez zmian, żeby użytkownik zobaczył to, co wpisał.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            return ''
        text = f'{value.normalize():f}'
        return text
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return plain_decimal(Decimal(str(value)))
    return value


@register.filter
def qty(value):
    """Ilość w stylu interfejsu: maks. 2 miejsca po przecinku, bez zbędnych zer.

    Ten sam format co ``formatQuantity`` w skryptach (``toLocaleString('pl-PL',
    {maximumFractionDigits: 2})``), więc liczba wyrenderowana przez serwer nie
    zmienia wyglądu po odświeżeniu przez JS: 900 ml zamiast 900,00 ml,
    1,5 kg zamiast 1,50 kg. Tysiące grupowane są spacją od 10 000 w górę,
    tak jak robi to polska lokalizacja przeglądarki.
    """
    if value in (None, ''):
        return ''
    try:
        number = Decimal(str(value))
    except Exception:
        return value
    number = number.quantize(Decimal('0.01'))
    negative = number < 0
    integer, _, fraction = f'{abs(number):f}'.partition('.')
    fraction = fraction.rstrip('0')
    if len(integer) > 4:
        groups = []
        while integer:
            groups.insert(0, integer[-3:])
            integer = integer[:-3]
        integer = ' '.join(groups)
    text = integer + (f',{fraction}' if fraction else '')
    return f'-{text}' if negative and text.strip('0, ') else text
