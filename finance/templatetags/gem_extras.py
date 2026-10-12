"""Formatowanie liczb panelu GEM (finance/partials/gem_panel.html)."""
from decimal import ROUND_HALF_UP, Decimal

from django import template
from django.utils.formats import number_format

register = template.Library()


@register.filter
def signed_pct(value, decimals=1):
    """+18,4% / −3,2% / 0,0% (minus typograficzny, przecinek jak w całej aplikacji)."""
    if value is None or value == '':
        return '–'
    decimals = int(decimals)
    # number_format obcina miejsca po przecinku, a wykres (JS) zaokrągla -
    # zaokrąglamy tak samo, żeby panel i wykres pokazywały te same liczby.
    value = _rounded(value, decimals)
    if value == 0:
        return f'{number_format(Decimal(0), decimal_pos=decimals)}%'
    return f'{"+" if value > 0 else "−"}{number_format(abs(value), decimal_pos=decimals)}%'


@register.filter
def signed_pp(value, decimals=1):
    """Różnica w punktach procentowych: „3,1 pp”."""
    if value is None or value == '':
        return '–'
    return f'{number_format(abs(_rounded(value, int(decimals))), decimal_pos=int(decimals))} pp'


def _rounded(value, decimals):
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
