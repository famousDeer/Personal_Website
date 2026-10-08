import csv
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date as date_type, datetime
from decimal import Decimal
from io import StringIO

from django.db import transaction

from utils.tools import month_start, parse_date_input, parse_decimal

from .account_utils import get_or_create_monthly_record, recalculate_monthly_record
from .investment_funding import INVESTMENT_CATEGORY, sync_investment_funding
from .models import BrokerageAccount, Daily, Income, Monthly


MILLENNIUM_SOURCE = 'millennium'
ING_SOURCE = 'ing'
EXPENSE = 'expense'
INCOME = 'income'

MILLENNIUM_REQUIRED_HEADERS = {
    'Data transakcji',
    'Rodzaj transakcji',
    'Odbiorca/Zleceniodawca',
    'Opis',
    'Obciążenia',
    'Uznania',
    'Waluta',
}

ING_REQUIRED_HEADERS = {
    'Data transakcji',
    'Dane kontrahenta',
    'Tytuł',
    'Nr transakcji',
    'Kwota transakcji (waluta rachunku)',
    'Waluta',
    'Konto',
}

BANK_IMPORT_SOURCES = {MILLENNIUM_SOURCE, ING_SOURCE}


class BankImportError(Exception):
    pass


@dataclass
class BankTransactionCandidate:
    index: int
    row_number: int
    kind: str
    date: object
    amount: Decimal
    title: str
    category: str = ''
    source: str = ''
    store: str = ''
    raw_description: str = ''
    transaction_type: str = ''
    counterparty: str = ''
    external_id: str = ''
    suggestion_reason: str = ''
    duplicate: bool = False
    duplicate_reason: str = ''
    possible_duplicate: bool = False
    # Data zaksięgowania z wyciągu, gdy data transakcji pochodzi z opisu karty
    # (płatność w sobotę bank księguje w poniedziałek).
    booking_date: object = None

    @property
    def booked_later(self):
        return self.booking_date is not None and self.booking_date != self.date

    @property
    def booking_date_display(self):
        return self.booking_date.strftime('%d.%m') if self.booking_date else ''

    @property
    def is_expense(self):
        return self.kind == EXPENSE

    @property
    def is_income(self):
        return self.kind == INCOME

    @property
    def selected_by_default(self):
        return not self.duplicate and not self.possible_duplicate

    @property
    def amount_display(self):
        return f'{self.amount:.2f}'

    @property
    def date_display(self):
        return self.date.strftime('%Y-%m-%d')


@dataclass
class BankImportPreview:
    candidates: list[BankTransactionCandidate]
    warnings: list[str] = field(default_factory=list)

    @property
    def expenses_count(self):
        return sum(1 for candidate in self.candidates if candidate.is_expense)

    @property
    def incomes_count(self):
        return sum(1 for candidate in self.candidates if candidate.is_income)

    @property
    def duplicate_count(self):
        return sum(1 for candidate in self.candidates if candidate.duplicate or candidate.possible_duplicate)


@dataclass
class BankImportResult:
    created_expenses: int = 0
    created_incomes: int = 0
    duplicates: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def created_total(self):
        return self.created_expenses + self.created_incomes


POLISH_TRANSLATION = str.maketrans({
    'ł': 'l',
    'Ł': 'L',
})


def normalize_text(value):
    normalized = unicodedata.normalize('NFKD', str(value or '').translate(POLISH_TRANSLATION))
    ascii_text = ''.join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r'[^a-z0-9]+', ' ', ascii_text.casefold()).strip()


def _clean_text(value):
    return re.sub(r'\s+', ' ', str(value or '').replace('\xa0', ' ')).strip()


def _clean_header(value):
    return _clean_text(value).lstrip('\ufeff')


def _decode_csv(data):
    for encoding in ('utf-8-sig', 'cp1250', 'iso-8859-2'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise BankImportError('Nie udało się rozpoznać kodowania pliku CSV.')


def _csv_dialect(text):
    sample = text[:4096]
    try:
        return csv.Sniffer().sniff(sample, delimiters=',;')
    except csv.Error:
        return csv.excel


def _optional_decimal(value):
    if value in (None, ''):
        return None
    clean_value = _clean_text(value)
    if not clean_value:
        return None
    return parse_decimal(clean_value)


def _parse_date(value):
    clean_value = _clean_text(value)
    for date_format in ('%Y-%m-%d', '%d.%m.%Y'):
        try:
            return datetime.strptime(clean_value, date_format).date()
        except ValueError:
            continue
    raise ValueError(f'nieprawidłowa data: {value}')


STORE_ALIASES = [
    # (słowo po normalize_text, nazwa) - dopasowanie od początku słowa
    ('mcdonald', "McDonald's"),
    ('store steampowered', 'Steam'),
    ('steampowered', 'Steam'),
    ('steam', 'Steam'),
    ('valve', 'Steam'),
    ('biedronka', 'Biedronka'),
    ('zabka', 'Żabka'),
    ('lidl', 'Lidl'),
    ('kaufland', 'Kaufland'),
    ('auchan', 'Auchan'),
    ('carrefour', 'Carrefour'),
    ('dino', 'Dino'),
    ('netto', 'Netto'),
    ('stokrotka', 'Stokrotka'),
    ('polomarket', 'POLOmarket'),
    ('rossmann', 'Rossmann'),
    ('hebe', 'Hebe'),
    ('super pharm', 'Super-Pharm'),
    ('douglas', 'Douglas'),
    ('rituals', 'Rituals'),
    ('apteka', 'Apteka'),
    ('orlen', 'Orlen'),
    ('circle k', 'Circle K'),
    ('mol', 'MOL'),
    ('amic', 'Amic'),
    ('bp', 'BP'),
    ('shell', 'Shell'),
    ('moya', 'Moya'),
    ('pkp', 'PKP Intercity'),
    ('intercity', 'PKP Intercity'),
    ('koleo', 'Koleo'),
    ('jakdojade', 'Jakdojade'),
    ('apcoa', 'APCOA'),
    ('uber', 'Uber'),
    ('bolt', 'Bolt'),
    ('allegro', 'Allegro'),
    ('amazon', 'Amazon'),
    ('aliexpress', 'AliExpress'),
    ('temu', 'Temu'),
    ('ikea', 'IKEA'),
    ('leroy merlin', 'Leroy Merlin'),
    ('castorama', 'Castorama'),
    ('obi', 'OBI'),
    ('jysk', 'JYSK'),
    ('pepco', 'Pepco'),
    ('media expert', 'Media Expert'),
    ('media markt', 'MediaMarkt'),
    ('mediamarkt', 'MediaMarkt'),
    ('x kom', 'x-kom'),
    ('decathlon', 'Decathlon'),
    ('empik', 'Empik'),
    ('helios', 'Helios'),
    ('multikino', 'Multikino'),
    ('cinema city', 'Cinema City'),
    ('netflix', 'Netflix'),
    ('spotify', 'Spotify'),
    ('eneba', 'Eneba'),
    ('k4g', 'K4G'),
    ('g2a', 'G2A'),
    ('orange', 'Orange'),
    ('t mobile', 'T-Mobile'),
    ('xtb', 'XTB'),
    ('revolut', 'Revolut'),
]
# Operatorzy płatności przed gwiazdką: „ING*mrcleaner.pl”, „PAYU*SKLEP”.
PAYMENT_PROCESSORS = {
    'ing', 'payu', 'paypro', 'paypal', 'sumup', 'sq', 'zen', 'dotpay', 'przelewy24', 'p24', 'tpay',
    'stripe', 'ppro', 'nayax', 'google', 'paddle', 'fs', 'payeezy', 'adyen', 'dlocal',
}
DOMAIN = re.compile(
    r'^(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9-]*)(?:\.[a-z0-9-]+)*'
    r'\.(?:pl|com|eu|net|org|de|uk|io|lt|cz|sk|fr|it|es|nl|co|app|shop|store)(?:/\S*)?(?=\s|$|\*)',
    re.IGNORECASE,
)
LEGAL_FORM = re.compile(
    r'(?:^|\s)(?:sp\.?\s*z\s*o\.?\s*o\.?|sp[oó]łka\s+z\s+ograniczon[aą]\s+odpowiedzialno[sś]ci[aą]'
    r'|sp[oó]łka\s+akcyjna|sp[oó]łka\s+jawna|sp[oó]łka\s+komandytowa|s\.\s*a\.?|sa|sp\.?\s*j\.?|sp\.?\s*k\.?'
    r'|s\.?\s*c\.?|sp\.?|ltd\.?|limited|gmbh|inc\.?|llc|s\.?\s*r\.?\s*o\.?|b\.?\s*v\.?|ab|as)\s*$',
    re.IGNORECASE,
)
# Długie formy prawne ucinają nazwę także w środku: za nimi stoi już adres
# („MOBILE-TRAFFIC-DATA SPÓŁKA Z O.O. DRUŻBICKIEGO 11”).
LEGAL_FORM_INSIDE = re.compile(
    r'\s(?:sp\.?\s*z\s*o\.?\s*o\.?|sp[oó]łka\s+(?:z\s+ograniczon[aą]\s+odpowiedzialno[sś]ci[aą]|akcyjna|jawna|komandytowa)'
    r'|s\.\s*a\.|ltd\.?|gmbh|inc\.)(?=\s|$)',
    re.IGNORECASE,
)
ADDRESS_START = re.compile(
    r'\s(?:ul\.?|al\.?|os\.?|pl\.|ulica|aleja|street|st\.)\s|\s\d{2}-\d{3}\b|\s\d+[a-z]?(?:/\d+)?\s+\d{2}-\d{3}|,',
    re.IGNORECASE,
)
STORE_CODE = re.compile(r'^(?:[A-Z]{1,3}\d{2,}|K\.?\d+|NR\.?\d*|\d{1,5})$', re.IGNORECASE)


def _alias_for(value):
    padded = f' {normalize_text(value)}'
    for keyword, label in STORE_ALIASES:
        if f' {keyword}' in padded and (padded + ' ')[padded.index(f' {keyword}') + len(keyword) + 1] in ' 0123456789':
            return label
    return ''


def _pretty_case(value):
    """WIELKIE albo małe litery -> „Pierogarnia Pierozek”; mieszane bez zmian."""
    if not (value.isupper() or value.islower()):
        return value
    words = []
    for word in value.split(' '):
        if any(char.isdigit() for char in word) and any(char.isalpha() for char in word):
            words.append(word.upper())
        else:
            words.append('-'.join(part[:1].upper() + part[1:].lower() for part in word.split('-')))
    return ' '.join(words)


def _domain_brand(value):
    match = DOMAIN.match(value.strip())
    if not match:
        return ''
    brand = match.group(1)
    return brand.upper() if len(brand) <= 3 or any(char.isdigit() for char in brand) else brand.capitalize()


def _strip_legal_forms(value):
    previous = None
    while previous != value:
        previous = value
        value = LEGAL_FORM.sub('', value).strip(' .,-')
    return value


def clean_store_name(value, city=''):
    """Czytelna nazwa sklepu z opisu karty: „LEROY MERLIN GDYNIA” -> „Leroy Merlin”.

    Usuwa operatora płatności („ING*”), adres strony („www.”, „.pl”),
    miasto, kody stacji i kas („SF320”, „K.1”), formy prawne („SP. Z O.O.”).
    Znane sieci dostają stałą nazwę (STORE_ALIASES).
    """
    name = _clean_text(value)
    if not name:
        return ''
    alias = _alias_for(name)
    if alias:
        return alias
    if '*' in name:
        left, right = (part.strip() for part in name.split('*', 1))
        name = right if normalize_text(left) in PAYMENT_PROCESSORS and right else left
    brand = _domain_brand(name)
    if brand:
        return _alias_for(brand) or brand

    words = name.split(' ')
    city_key = normalize_text(city)
    if city_key:
        kept = []
        for word in words:
            key = normalize_text(word)
            if key == city_key or (key.startswith(city_key) and key[len(city_key):].isdigit()):
                continue
            kept.append(word)
        words = kept or words
    while len(words) > 1 and normalize_text(words[-1]) in ('pol', 'polska', 'pl'):
        words = words[:-1]
    name = _strip_legal_forms(' '.join(words))
    words = name.split(' ')
    without_codes = [word for word in words if not STORE_CODE.match(word.strip('.,'))]
    if without_codes and any(char.isalpha() for char in ''.join(without_codes)):
        words = without_codes
    elif city and not any(char.isalpha() for char in ''.join(words)):
        words = words + [city]  # „254” + „GDYNIA” - sam numer nic nie mówi
    name = _pretty_case(' '.join(words).strip(' .,-'))
    return _alias_for(name) or name


def clean_counterparty_name(value):
    """Kontrahent bez adresu: „eneba.com Gyneju st. 4-333 Vilnius” -> „Eneba”."""
    name = _clean_text(value)
    if not name:
        return ''
    alias = _alias_for(name)
    if alias:
        return alias
    brand = _domain_brand(name)
    if brand:
        return _alias_for(brand) or brand
    name = name.strip(' "\'')
    if '"' in name:
        name = name.split('"', 1)[0]  # ING: „"NAZWA FIRMY" ADRES”
    inside = LEGAL_FORM_INSIDE.search(f' {name}')
    if inside and inside.start() > 0:
        name = f' {name}'[:inside.start()]
    name = ADDRESS_START.split(f' {name.strip()} ', 1)[0].strip()
    name = _strip_legal_forms(name.strip(' "\''))
    words = name.split(' ')
    if len(words) > 1 and words[0].casefold() == words[1].casefold():
        words = words[1:]  # „Orange Orange Polska”
    name = _pretty_case(' '.join(words))
    return _alias_for(name) or name


def _canonical_store(value):
    """Zgodność wstecz: nazwa sklepu bez miasta i kodów."""
    return clean_store_name(value)


@dataclass
class CardDescription:
    merchant: str
    city: str
    country: str
    date: object


CARD_DESCRIPTION = re.compile(
    r'^(?P<body>.*?)\s+(?:(?P<country>[A-Z]{3})\s+)?(?P<date>\d{4}-\d{2}-\d{2})\s*$'
)
# Ile dni bank może księgować płatność kartą (weekend, święta, rozliczenie walut).
MAX_BOOKING_DELAY_DAYS = 14


def parse_card_description(raw_value):
    """Opis płatności kartą Millennium: „SKLEP  MIASTO KRAJ RRRR-MM-DD”.

    Sklep od miasta oddziela podwójna spacja (w nazwie też może być,
    np. „FRED SP.  Z O.O.  GDYNIA”), więc miasto to ostatni fragment.
    """
    text = str(raw_value or '').replace('\xa0', '  ').strip()
    match = CARD_DESCRIPTION.match(text)
    if not match:
        return None
    try:
        when = date_type.fromisoformat(match['date'])
    except ValueError:
        return None
    parts = [part.strip() for part in re.split(r'\s{2,}', match['body'].strip()) if part.strip()]
    if not parts:
        return None
    if len(parts) > 1:
        return CardDescription(' '.join(parts[:-1]), parts[-1], match['country'] or '', when)
    return CardDescription(parts[0], '', match['country'] or '', when)


def _merchant_from_description(description):
    card = parse_card_description(description)
    if card:
        return clean_store_name(card.merchant, card.city)
    return clean_store_name(description)


def _row_value(row, *fields):
    for field in fields:
        value = row.get(field, '')
        if value:
            return value
    return ''


def _combined_text(row):
    return ' '.join([
        _row_value(row, 'Rodzaj transakcji', 'Szczegóły'),
        _row_value(row, 'Odbiorca/Zleceniodawca', 'Dane kontrahenta'),
        _row_value(row, 'Opis', 'Tytuł'),
    ])


def _expense_store(row, card=None):
    if card is not None:
        return clean_store_name(card.merchant, card.city)
    description = _row_value(row, 'Opis', 'Tytuł')
    counterparty = _row_value(row, 'Odbiorca/Zleceniodawca', 'Dane kontrahenta')
    if counterparty:
        return clean_counterparty_name(counterparty)
    return clean_store_name(description)


TRANSFER_TYPES = ('przelew', 'blik na telefon')


def _is_transfer(row):
    transaction_type = normalize_text(_row_value(row, 'Rodzaj transakcji', 'Szczegóły'))
    return any(keyword in transaction_type for keyword in TRANSFER_TYPES)


# Słowa ze spacją z przodu pasują tylko od początku wyrazu („ mol ” nie trafi
# w „ramol”), ze spacją z tyłu - tylko do końca wyrazu („bar ” nie trafi
# w „barber”). Kolejność ma znaczenie: wygrywa pierwsza pasująca kategoria.
EXPENSE_RULES = [
    ('Zakupy spozywcze', ['biedronka', 'lidl', 'zabka', 'auchan', 'carrefour', 'aldi', 'netto', 'kaufland',
                          'stokrotka', 'leclerc', ' dino ', 'polomarket', ' spar ', 'intermarche', 'freshmarket',
                          'delikatesy', 'piekarnia', 'warzywniak', 'market ']),
    ('Jedzenie na miescie', ['mcdonald', 'kfc', 'burger', 'pizza', 'kebab', 'kebap', 'restauracja', 'restaurant',
                             'bar ', 'bistro', 'grill', 'pierogarnia', 'sushi', 'ramen', 'cafe', 'coffee', 'kawy',
                             'kawiarnia', 'cukiernia', 'lody', 'starbucks', 'costa', 'popeyes', 'pyszne', 'glovo',
                             'wolt', 'uber eats', 'karczma', 'gospoda', 'tawerna', 'pub ']),
    ('Podroze - transport', ['pkp', 'intercity', 'ryanair', 'wizzair', 'lot polish', 'booking flight', 'flixbus']),
    ('Transport miejski', ['ztm', 'skm', 'jakdojade', 'koleo', 'uber', 'bolt', 'taxi', 'skycash', 'parking',
                           'apcoa', 'parkomat', 'mpay', 'pango', 'mevo', 'tfl']),
    ('Paliwo', ['orlen', 'circle k', 'shell', 'moya', ' bp ', 'lotos', ' mol ', ' amic ', ' avia ',
                'stacja paliw']),
    ('Zdrowie', ['apteka', ' doz ', 'medicover', 'lux med', 'enel med', 'lekarz', 'przychodnia', 'dentyst', 'stomatolog']),
    ('Drogeria', ['rossmann', 'hebe', 'drogeria natura', 'super pharm']),
    ('Uroda', ['douglas', 'rituals', 'sephora', 'fryzjer', 'barber', 'kosmetycz']),
    ('Ubrania', ['zalando', 'reserved', 'zara', ' hm ', ' h m ', ' ccc ', 'eobuwie', 'shein', 'vinted', 'van graaf',
                 'sinsay', ' house ', 'cropp', 'answear']),
    ('Subskrypcje', ['netflix', 'spotify', 'icloud', 'apple com bill', 'google storage', 'youtube premium', 'disney',
                     'hbo max', ' max com', ' canal ', 'chatgpt', 'openai']),
    ('Rozrywka', ['steam', 'valve', 'playstation', 'xbox', 'nintendo', 'eneba', ' k4g', ' g2a', 'instant gaming',
                  'humble', ' gog ', 'epic games', 'kino', 'cinema', 'helios', 'multikino', 'teatr', 'bilety',
                  'ebilet', 'geoguessr']),
    ('Rachunki', ['czynsz', 'energia', 'prad', ' gaz ', 'internet', 'orange', 'play ', 't mobile', 'plus gsm',
                  'vectra', ' upc ', 'rachunek', 'pgnig', 'tauron', 'enea', 'energa', 'wodociagi']),
    ('Inwestycje', ['xtb', 'maklerski', 'broker', 'revolut trading']),
    ('Sport', ['silownia', ' gym ', 'fitness', 'decathlon', 'basen', 'multisport']),
    ('Wyposazenie domu', ['ikea', 'leroy', 'castorama', ' obi ', 'jysk', 'home you', 'pepco', ' action ', 'agata',
                          'black red white', 'media expert', 'mediamarkt', 'media markt', 'x kom', 'rtv euro']),
    ('Edukacja', ['udemy', 'coursera', 'ebook', 'ksiazka', 'studia', 'empik']),
    ('Prezenty', ['prezent']),
    ('Rodzina', [' zona ', ' zonka', ' zony ', ' maz ', ' meza ', ' mama', ' mamy ', ' tata', ' taty ', ' rodzic',
                 ' syn ', ' corka', ' brat ', ' siostra']),
]


def _category_by_rules(candidate_text):
    text = f' {normalize_text(candidate_text)} '
    for category, keywords in EXPENSE_RULES:
        if any(keyword in text for keyword in keywords):
            return category, f'reguła: {category}'
    return 'Inne', 'domyślnie'


def _source_by_rules(candidate_text):
    text = normalize_text(candidate_text)
    rules = [
        ('Pensja', ['pensja', 'wynagrodzenie', 'salary', 'payroll']),
        ('Premia', ['premia', 'bonus', 'kieszonkowe']),
        ('Dieta', ['dieta', 'delegac']),
        ('Inwestycje', ['odsetki', 'dywidenda', 'oprocentowanie']),
        ('Zwrot podatku', ['zwrot podatku', 'urzad skarbowy']),
        ('Sprzedaż', ['sprzedaz', 'vinted', 'olx', 'allegro lokalnie']),
        ('Rodzina', ['zona', 'zonka', 'maz', 'mama', 'tata', 'rodzic', 'netflix', 'spotify']),
    ]
    for source, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return source, f'reguła: {source}'
    return 'Inne', 'domyślnie'


HISTORY_LOOKUP_LIMIT = 500
MIN_EXPENSE_STORE_KEY_LENGTH = 3
MIN_INCOME_TITLE_KEY_LENGTH = 4


class BankHistoryIndex:
    """Znormalizowana historia konta, zbudowana raz na cały import.

    Poprzednio każdy wiersz wyciągu odpytywał bazę o 500 ostatnich rekordów
    i normalizował je od nowa - dla wyciągu z 300 pozycjami to 600 zapytań
    i 300 000 wywołań ``normalize_text``. Tutaj zapytania idą raz, a klucze
    są znormalizowane raz.

    Semantyka dopasowania jest identyczna: rekordy zachowują kolejność malejąco
    po dacie i wygrywa pierwszy pasujący. Klucze powtórzone są pomijane, bo
    przy dopasowaniu "pierwszy wygrywa" późniejszy duplikat i tak nie mógłby
    zostać zwrócony.
    """

    __slots__ = ('expenses', 'incomes')

    def __init__(self, account):
        self.expenses = []
        self.incomes = []
        if account is None:
            return

        seen_stores = set()
        expense_entries = (
            Daily.objects
            .filter(account=account)
            .exclude(store='')
            .order_by('-date')
            .values('store', 'category', 'title')[:HISTORY_LOOKUP_LIMIT]
        )
        for entry in expense_entries:
            store_key = normalize_text(entry['store'])
            if len(store_key) < MIN_EXPENSE_STORE_KEY_LENGTH or store_key in seen_stores:
                continue
            seen_stores.add(store_key)
            self.expenses.append((store_key, entry))

        seen_titles = set()
        income_entries = (
            Income.objects
            .filter(account=account)
            .order_by('-date')
            .values('title', 'source')[:HISTORY_LOOKUP_LIMIT]
        )
        for entry in income_entries:
            title_key = normalize_text(entry['title'])
            if len(title_key) < MIN_INCOME_TITLE_KEY_LENGTH or title_key in seen_titles:
                continue
            seen_titles.add(title_key)
            self.incomes.append((title_key, entry))

    def match_expense(self, store, row):
        text = normalize_text(' '.join([store, _combined_text(row)]))
        if not text:
            return None
        padded = f' {text} '
        store_text = f' {normalize_text(store)} '
        for store_key, entry in self.expenses:
            # Od początku wyrazu: sklep „Ola” z historii nie trafi w „kolacja”.
            if f' {store_key}' in padded or (store_text.strip() and store_text in f' {store_key} '):
                return entry
        return None

    def match_income(self, row):
        text = normalize_text(_combined_text(row))
        if not text:
            return None
        for title_key, entry in self.incomes:
            if title_key in text or text in title_key:
                return entry
        return None


def _expense_title(category, store, row):
    if category == 'Zakupy spozywcze':
        return 'Zakupy spożywcze'
    if category == 'Jedzenie na miescie':
        return store or 'Jedzenie na mieście'
    if category == 'Paliwo':
        return 'Paliwo'
    if category == 'Subskrypcje':
        return store or 'Subskrypcja'
    return store or _clean_text(_row_value(
        row,
        'Opis',
        'Tytuł',
        'Odbiorca/Zleceniodawca',
        'Dane kontrahenta',
        'Rodzaj transakcji',
        'Szczegóły',
    ))[:120]


def _income_title(row):
    description = _clean_text(_row_value(row, 'Opis', 'Tytuł'))
    counterparty = _clean_text(_row_value(row, 'Odbiorca/Zleceniodawca', 'Dane kontrahenta'))
    if description and len(description) <= 120:
        return description
    if counterparty:
        return counterparty[:120]
    return _clean_text(_row_value(row, 'Rodzaj transakcji', 'Szczegóły'))[:120] or 'Wpływ'


def _external_id(row, import_source):
    if import_source == ING_SOURCE:
        identity = '\u241f'.join(
            f'{field}={_clean_text(value)}'
            for field, value in sorted(row.items())
        )
        digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]
        return f'{ING_SOURCE}:{digest}'

    fields = [
        'Numer rachunku/karty',
        'Data transakcji',
        'Data rozliczenia',
        'Rodzaj transakcji',
        'Na konto/Z konta',
        'Odbiorca/Zleceniodawca',
        'Opis',
        'Obciążenia',
        'Uznania',
        'Saldo',
        'Waluta',
    ]
    identity = '\u241f'.join(_clean_text(row.get(field, '')) for field in fields)
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]
    return f'{import_source}:{digest}'


def _transaction_dates(row, card):
    """(data transakcji, data zaksięgowania albo None).

    Millennium w kolumnie „Data transakcji” podaje dzień zaksięgowania,
    a prawdziwy dzień płatności kartą stoi na końcu opisu. Bierzemy go,
    jeśli jest wcześniejszy i nie starszy niż MAX_BOOKING_DELAY_DAYS.
    """
    booked = _parse_date(row.get('Data transakcji'))
    if card is None or card.date > booked or (booked - card.date).days > MAX_BOOKING_DELAY_DAYS:
        return booked, None
    return card.date, booked


def _candidate_from_row(index, row_number, row, account, import_source=MILLENNIUM_SOURCE, history=None,
                        raw_description=None):
    if history is None:
        history = BankHistoryIndex(account)
    debit = _optional_decimal(row.get('Obciążenia'))
    credit = _optional_decimal(row.get('Uznania'))
    if debit is None and credit is None:
        return None
    if debit is not None and credit is not None:
        raise ValueError('wiersz ma jednocześnie obciążenie i uznanie')

    # Opis przed czyszczeniem - podwójna spacja oddziela sklep od miasta.
    card = parse_card_description(raw_description if raw_description is not None else row.get('Opis'))
    transaction_date, booking_date = _transaction_dates(row, card)
    if debit is not None:
        amount = abs(debit)
        store = _expense_store(row, card)
        rule_category, rule_reason = _category_by_rules(' '.join([store, _combined_text(row)]))
        historical_match = history.match_expense(store, row) if account else None
        if rule_category == INVESTMENT_CATEGORY:
            category = rule_category
            title = _expense_title(category, store, row)
            reason = rule_reason
        elif historical_match:
            category = historical_match['category']
            title = historical_match['title'] or _expense_title(category, store, row)
            reason = f"historia: {historical_match['store']}"
        else:
            category, reason = rule_category, rule_reason
            title = _expense_title(category, store, row)
            if card is None and store and _is_transfer(row):
                title = f'Przelew: {store}'
        return BankTransactionCandidate(
            index=index,
            row_number=row_number,
            kind=EXPENSE,
            date=transaction_date,
            amount=amount,
            title=title,
            category=category,
            store=store,
            raw_description=_clean_text(_row_value(row, 'Opis', 'Tytuł')),
            transaction_type=_clean_text(_row_value(row, 'Rodzaj transakcji', 'Szczegóły')),
            counterparty=_clean_text(_row_value(row, 'Odbiorca/Zleceniodawca', 'Dane kontrahenta')),
            external_id=_external_id(row, import_source),
            suggestion_reason=reason,
            booking_date=booking_date,
        )

    amount = abs(credit)
    historical_match = history.match_income(row) if account else None
    if historical_match:
        source = historical_match['source']
        title = historical_match['title'] or _income_title(row)
        reason = f"historia: {historical_match['title']}"
    else:
        source, reason = _source_by_rules(_combined_text(row))
        title = _income_title(row)
    return BankTransactionCandidate(
        index=index,
        row_number=row_number,
        kind=INCOME,
        date=transaction_date,
        amount=amount,
        title=title,
        source=source,
        raw_description=_clean_text(_row_value(row, 'Opis', 'Tytuł')),
        transaction_type=_clean_text(_row_value(row, 'Rodzaj transakcji', 'Szczegóły')),
        counterparty=_clean_text(_row_value(row, 'Odbiorca/Zleceniodawca', 'Dane kontrahenta')),
        external_id=_external_id(row, import_source),
        suggestion_reason=reason,
        booking_date=booking_date,
    )


def _mark_duplicates(candidates, account):
    external_ids = [candidate.external_id for candidate in candidates if candidate.external_id]
    existing_expenses = set(
        Daily.objects.filter(account=account, external_id__in=external_ids).values_list('external_id', flat=True)
    )
    existing_incomes = set(
        Income.objects.filter(account=account, external_id__in=external_ids).values_list('external_id', flat=True)
    )

    # Wykrywanie "podobnych" pozycji robiło jedno zapytanie na kandydata, czyli
    # 300 zapytań dla wyciągu z 300 wierszami. Tutaj obie tabele są pobierane
    # raz, ograniczone do dat występujących w pliku, a porównanie idzie po
    # zbiorach kluczy.
    candidate_dates = {candidate.date for candidate in candidates if candidate.date}
    expense_keys_with_store = set()
    expense_keys_any_store = set()
    income_keys = set()
    if candidate_dates:
        for entry in Daily.objects.filter(
            account=account,
            date__in=candidate_dates,
        ).values('date', 'cost', 'title', 'store'):
            title_key = (entry['title'] or '').lower()
            expense_keys_any_store.add((entry['date'], entry['cost'], title_key))
            expense_keys_with_store.add(
                (entry['date'], entry['cost'], title_key, (entry['store'] or '').lower())
            )
        for entry in Income.objects.filter(
            account=account,
            date__in=candidate_dates,
        ).values('date', 'amount', 'title'):
            income_keys.add((entry['date'], entry['amount'], (entry['title'] or '').lower()))

    for candidate in candidates:
        if candidate.external_id in existing_expenses or candidate.external_id in existing_incomes:
            candidate.duplicate = True
            candidate.duplicate_reason = 'już zaimportowano'
            continue

        title_key = (candidate.title or '').lower()
        if candidate.is_expense:
            if candidate.store:
                found = (
                    candidate.date, candidate.amount, title_key, candidate.store.lower()
                ) in expense_keys_with_store
            else:
                found = (candidate.date, candidate.amount, title_key) in expense_keys_any_store
            if found:
                candidate.possible_duplicate = True
                candidate.duplicate_reason = 'podobny wydatek już istnieje'
        else:
            if (candidate.date, candidate.amount, title_key) in income_keys:
                candidate.possible_duplicate = True
                candidate.duplicate_reason = 'podobny przychód już istnieje'


def _parse_millennium_csv_text(text, account):
    reader = csv.DictReader(StringIO(text), dialect=_csv_dialect(text))
    headers = {_clean_header(header) for header in (reader.fieldnames or [])}
    missing_headers = sorted(MILLENNIUM_REQUIRED_HEADERS - headers)
    if missing_headers:
        raise BankImportError(f'Brakuje kolumn w CSV Millennium: {", ".join(missing_headers)}.')

    warnings = []
    candidates = []
    history = BankHistoryIndex(account)
    for row_number, raw_row in enumerate(reader, start=2):
        row = {_clean_header(key): _clean_text(value) for key, value in raw_row.items() if key is not None}
        if not any(row.values()):
            continue
        raw_description = next(
            (value for key, value in raw_row.items() if key is not None and _clean_header(key) == 'Opis'), '',
        )

        currency = row.get('Waluta', '').upper()
        if currency and currency != 'PLN':
            warnings.append(f'Pominięto wiersz {row_number}: waluta {currency} nie jest obsługiwana.')
            continue

        try:
            candidate = _candidate_from_row(
                len(candidates), row_number, row, account, history=history, raw_description=raw_description,
            )
        except (ValueError, ArithmeticError) as exc:
            warnings.append(f'Pominięto wiersz {row_number}: {exc}.')
            continue
        if candidate is not None and candidate.amount > 0:
            candidates.append(candidate)

    if not candidates:
        raise BankImportError('Nie znaleziono wydatków ani przychodów do importu.')

    _mark_duplicates(candidates, account)
    return BankImportPreview(candidates=candidates, warnings=warnings)


def parse_millennium_csv(uploaded_file, account):
    data = uploaded_file.read()
    if not data:
        raise BankImportError('Wgrany plik jest pusty.')
    return _parse_millennium_csv_text(_decode_csv(data), account)


def _ing_row(headers, values):
    row = {}
    for header, value in zip(headers, values):
        clean_header = _clean_header(header)
        if clean_header and clean_header not in row:
            row[clean_header] = _clean_text(value)
    return row


def _parse_ing_csv_text(text, account):
    reader = csv.reader(StringIO(text), dialect=_csv_dialect(text))
    headers = None

    for row_number, values in enumerate(reader, start=1):
        clean_headers = [_clean_header(value) for value in values]
        if ING_REQUIRED_HEADERS.issubset(set(clean_headers)):
            headers = clean_headers
            break

    if headers is None:
        raise BankImportError('Brakuje kolumn w CSV ING.')

    warnings = []
    candidates = []
    history = BankHistoryIndex(account)
    for row_number, values in enumerate(reader, start=row_number + 1):
        row = _ing_row(headers, values)
        if not any(row.values()):
            continue

        currency = row.get('Waluta', '').upper()
        if currency and currency != 'PLN':
            warnings.append(f'Pominięto wiersz {row_number}: waluta {currency} nie jest obsługiwana.')
            continue

        try:
            signed_amount = _optional_decimal(row.get('Kwota transakcji (waluta rachunku)'))
            if signed_amount is None:
                continue
            if signed_amount < 0:
                row['Obciążenia'] = str(signed_amount)
            elif signed_amount > 0:
                row['Uznania'] = str(signed_amount)
            else:
                continue
            candidate = _candidate_from_row(
                len(candidates),
                row_number,
                row,
                account,
                import_source=ING_SOURCE,
                history=history,
            )
        except (ValueError, ArithmeticError) as exc:
            warnings.append(f'Pominięto wiersz {row_number}: {exc}.')
            continue
        if candidate is not None and candidate.amount > 0:
            candidates.append(candidate)

    if not candidates:
        raise BankImportError('Nie znaleziono wydatków ani przychodów do importu.')

    _mark_duplicates(candidates, account)
    return BankImportPreview(candidates=candidates, warnings=warnings)


def _contains_required_headers(text, required_headers):
    reader = csv.reader(StringIO(text), dialect=_csv_dialect(text))
    for values in reader:
        headers = {_clean_header(value) for value in values}
        if required_headers.issubset(headers):
            return True
    return False


def parse_bank_csv(uploaded_file, account):
    data = uploaded_file.read()
    if not data:
        raise BankImportError('Wgrany plik jest pusty.')

    text = _decode_csv(data)
    if _contains_required_headers(text, MILLENNIUM_REQUIRED_HEADERS):
        return _parse_millennium_csv_text(text, account)
    if _contains_required_headers(text, ING_REQUIRED_HEADERS):
        return _parse_ing_csv_text(text, account)
    raise BankImportError('Nie rozpoznano formatu CSV. Obsługiwane banki: Millennium i ING.')


def _payload_value(post_data, index, name, default=''):
    return post_data.get(f'row_{index}_{name}', default)


def import_candidates_from_post(user, account, post_data):
    try:
        row_count = int(post_data.get('row_count', '0'))
    except (TypeError, ValueError):
        raise BankImportError('Nieprawidłowe dane podglądu importu.')

    result = BankImportResult()
    touched_months = set()
    selected_any = False

    with transaction.atomic():
        for index in range(row_count):
            if post_data.get(f'row_{index}_selected') != 'on':
                result.skipped += 1
                continue

            selected_any = True
            kind = _payload_value(post_data, index, 'kind')
            external_id = _payload_value(post_data, index, 'external_id')
            if not external_id:
                result.warnings.append(f'Pominięto wiersz {index + 1}: brak identyfikatora importu.')
                result.skipped += 1
                continue

            import_source, separator, _ = external_id.partition(':')
            if not separator or import_source not in BANK_IMPORT_SOURCES:
                result.warnings.append(f'Pominięto wiersz {index + 1}: nieznane źródło importu.')
                result.skipped += 1
                continue

            if (
                Daily.objects.filter(account=account, external_id=external_id).exists()
                or Income.objects.filter(account=account, external_id=external_id).exists()
            ):
                result.duplicates += 1
                continue

            try:
                transaction_date = parse_date_input(_payload_value(post_data, index, 'date'))
                amount = parse_decimal(_payload_value(post_data, index, 'amount'))
            except ValueError as exc:
                result.warnings.append(f'Pominięto wiersz {index + 1}: {exc}.')
                result.skipped += 1
                continue

            if amount <= 0:
                result.warnings.append(f'Pominięto wiersz {index + 1}: kwota musi być większa od 0.')
                result.skipped += 1
                continue

            monthly_record, _ = get_or_create_monthly_record(
                user=user,
                account=account,
                month_date=month_start(transaction_date),
                for_update=True,
            )

            title = _clean_text(_payload_value(post_data, index, 'title')) or 'Import bankowy'
            if kind == EXPENSE:
                category = _clean_text(_payload_value(post_data, index, 'category')) or 'Inne'
                store = _clean_text(_payload_value(post_data, index, 'store'))
                raw_brokerage_account_id = _payload_value(post_data, index, 'brokerage_account')
                brokerage_account = None
                if raw_brokerage_account_id:
                    if category != INVESTMENT_CATEGORY:
                        raise BankImportError(
                            f'Wiersz {index + 1}: konto maklerskie można przypisać tylko do kategorii Inwestycje.'
                        )
                    brokerage_account = BrokerageAccount.objects.filter(
                        id=raw_brokerage_account_id,
                        user=user,
                    ).first()
                    if brokerage_account is None:
                        raise BankImportError(f'Wiersz {index + 1}: nieprawidłowe konto maklerskie.')

                expense = Daily.objects.create(
                    user=user,
                    account=account,
                    date=transaction_date,
                    title=title,
                    cost=amount,
                    category=category,
                    store=store,
                    month=monthly_record,
                    brokerage_account=brokerage_account,
                    import_source=import_source,
                    external_id=external_id,
                )
                sync_investment_funding(expense)
                result.created_expenses += 1
            elif kind == INCOME:
                source = _clean_text(_payload_value(post_data, index, 'source')) or 'Inne'
                counterparty = _clean_text(_payload_value(post_data, index, 'counterparty'))[:255]
                Income.objects.create(
                    user=user,
                    account=account,
                    date=transaction_date,
                    title=title,
                    amount=amount,
                    source=source,
                    counterparty=counterparty,
                    month=monthly_record,
                    import_source=import_source,
                    external_id=external_id,
                )
                result.created_incomes += 1
            else:
                result.warnings.append(f'Pominięto wiersz {index + 1}: nieznany typ transakcji.')
                result.skipped += 1
                continue

            touched_months.add(monthly_record.id)

    if not selected_any:
        raise BankImportError('Zaznacz przynajmniej jedną pozycję do importu.')

    for monthly_record in Monthly.objects.filter(id__in=touched_months):
        recalculate_monthly_record(monthly_record)

    return result
