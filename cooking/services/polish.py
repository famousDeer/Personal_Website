"""Polskie formy liczebników i liczb w komunikatach."""


def polish_number(value):
    """Decimal bez zbędnych zer i z polskim przecinkiem: 1.50 -> '1,5'."""
    return f'{value.normalize():f}'.replace('.', ',')


def polish_count(count, one, few, many):
    """'1 ruch', '3 ruchy', '5 ruchów' - odmiana rzeczownika po liczebniku."""
    if count == 1:
        return f'{count} {one}'
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f'{count} {few}'
    return f'{count} {many}'


def completion_message(added, already):
    if added and already:
        return (
            f'Dodano do spiżarni {polish_count(added, "pozycję", "pozycje", "pozycji")}; '
            f'{polish_count(already, "trafiła", "trafiły", "trafiło")} tam już przy odhaczaniu.'
        )
    if already:
        return 'Lista zakończona. Kupione produkty trafiły do spiżarni już przy odhaczaniu.'
    return f'Dodano do spiżarni {polish_count(added, "kupioną pozycję", "kupione pozycje", "kupionych pozycji")}.'
