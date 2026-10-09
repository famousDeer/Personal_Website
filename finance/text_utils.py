"""Normalizacja tekstu z wyciągów bankowych.

Banki często gubią polskie znaki („ZABKA”, „Zona”), więc porównujemy tekst
bez nich: małe litery, bez ogonków, znaki inne niż litery i cyfry zamienione
na spacje. Wspólne dla finance.bank_import i finance.import_rules.
"""
import re
import unicodedata

POLISH_TRANSLATION = str.maketrans({
    'ł': 'l',
    'Ł': 'L',
})


def normalize_text(value):
    normalized = unicodedata.normalize('NFKD', str(value or '').translate(POLISH_TRANSLATION))
    ascii_text = ''.join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r'[^a-z0-9]+', ' ', ascii_text.casefold()).strip()
