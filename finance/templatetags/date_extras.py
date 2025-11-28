from django import template
from django.utils.safestring import mark_safe

register = template.Library()

@register.filter
def days_between(start_date, end_date):
    if not start_date or not end_date:
        return ""
    delta = (end_date - start_date).days + 1
    return f"{delta} dni"

@register.filter(name='get_shop_icon')
def get_shop_icon(shop_name):
    """
    Zwraca HTML z ikoną/logiem na podstawie nazwy sklepu.
    """
    if not shop_name:
        # Domyślna ikona dla braku sklepu
        return mark_safe('<i class="bi bi-bag text-muted"></i>')
    
    name = shop_name.lower().strip()

    # 1. MAPA LOGO (Pliki SVG w static/img/logos/)
    # Używamy plików SVG dla największych marek
    logos = {
        'biedronka': 'biedronka.svg',
        'lidl': 'lidl.svg',
        'żabka': 'zabka.svg',
        'zabka': 'zabka.svg',
        'orlen': 'orlen.svg',
        'mcdonalds': 'mcdonalds.svg',
        'mcdonald\'s': 'mcdonalds.svg',
        'auchan': 'auchan.svg',
        'rossmann': 'rossmann.svg',
        'allegro': 'allegro.svg',
        'netflix': 'netflix.svg',
        'pekao': 'pekao.svg',
        'spotify': 'spotify.svg',
        'ikea': 'ikea.svg',
        'empik': 'empik.svg',
    }

    # Sprawdź czy mamy logo dla tej nazwy (lub czy nazwa zawiera klucz)
    for key, filename in logos.items():
        if key in name:
            # Zwracamy obrazek SVG. Klasa 'shop-logo' do stylizacji w CSS.
            return mark_safe(f'<img src="/static/img/logos/{filename}" class="shop-logo" alt="{shop_name}">')

    # 2. MAPA IKON (Fallback do Bootstrap Icons)
    # Jeśli nie mamy loga, dobieramy ikonę po słowach kluczowych
    if any(x in name for x in ['paliwo', 'stacja', 'bp', 'lotos', 'circle']):
        return mark_safe('<i class="bi bi-fuel-pump text-danger"></i>')
    
    if any(x in name for x in ['restauracja', 'bar', 'pizza', 'burger', 'kebab']):
        return mark_safe('<i class="bi bi-cup-straw text-warning"></i>')
    
    if any(x in name for x in ['apteka', 'leki', 'doz']):
        return mark_safe('<i class="bi bi-capsule text-primary"></i>')

    # Domyślna ikona, jeśli nic nie pasuje
    return mark_safe('<i class="bi bi-shop text-secondary"></i>')