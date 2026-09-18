import csv
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
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


def _canonical_store(value):
    normalized = normalize_text(value)
    aliases = [
        ('mcdonald', "McDonald's"),
        ('store steampowered', 'Steam'),
        ('steam', 'Steam'),
        ('valve', 'Steam'),
        ('biedronka', 'Biedronka'),
        ('zabka', 'Żabka'),
        ('lidl', 'Lidl'),
        ('rossmann', 'Rossmann'),
        ('douglas', 'Douglas'),
        ('rituals', 'Rituals'),
        ('apteka', 'Apteka'),
        ('orlen', 'Orlen'),
        ('circle', 'Circle K'),
        ('bp', 'BP'),
        ('shell', 'Shell'),
        ('pkp', 'PKP Intercity'),
        ('intercity', 'PKP Intercity'),
        ('koleo', 'Koleo'),
        ('jakdojade', 'Jakdojade'),
        ('uber', 'Uber'),
        ('bolt', 'Bolt'),
        ('allegro', 'Allegro'),
        ('amazon', 'Amazon'),
        ('ikea', 'IKEA'),
        ('empik', 'Empik'),
        ('netflix', 'Netflix'),
        ('spotify', 'Spotify'),
        ('xtb', 'XTB'),
        ('revolut', 'Revolut'),
    ]
    for keyword, label in aliases:
        if keyword in normalized:
            return label

    clean_value = _clean_text(value)
    clean_value = re.sub(r'\b\d{1,4}\b$', '', clean_value).strip()
    if clean_value.isupper() or clean_value.casefold() == clean_value:
        return clean_value.title()
    return clean_value


def _merchant_from_description(description):
    clean_description = _clean_text(description)
    if not clean_description:
        return ''

    clean_description = re.sub(r'\s+\d{4}-\d{2}-\d{2}$', '', clean_description).strip()
    clean_description = re.sub(r'\s+POL$', '', clean_description, flags=re.IGNORECASE).strip()
    if '  ' in str(description or ''):
        clean_description = _clean_text(str(description).split('  ', 1)[0])
    if '\xa0' in str(description or ''):
        clean_description = _clean_text(str(description).split('\xa0', 1)[0])
    return _canonical_store(clean_description)


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


def _expense_store(row):
    transaction_type = normalize_text(_row_value(row, 'Rodzaj transakcji', 'Szczegóły'))
    description = _row_value(row, 'Opis', 'Tytuł')
    counterparty = _row_value(row, 'Odbiorca/Zleceniodawca', 'Dane kontrahenta')
    if any(keyword in transaction_type for keyword in ('zakup', 'platnosc blik', 'karta', 'tr kart', 'tr blik')):
        return _merchant_from_description(counterparty or description)
    return _canonical_store(counterparty or description)


def _category_by_rules(candidate_text):
    text = normalize_text(candidate_text)
    rules = [
        ('Zakupy spozywcze', ['biedronka', 'lidl', 'zabka', 'auchan', 'carrefour', 'aldi', 'netto', 'kaufland', 'stokrotka', 'leclerc']),
        ('Jedzenie na miescie', ['mcdonald', 'kfc', 'burger', 'pizza', 'kebab', 'restauracja', 'restaurant', 'bar ', 'cafe', 'coffee', 'starbucks', 'costa', 'popeyes']),
        ('Podroze - transport', ['pkp', 'intercity', 'ryanair', 'wizzair', 'lot polish', 'booking flight']),
        ('Transport miejski', ['ztm', 'skm', 'jakdojade', 'koleo', 'uber', 'bolt', 'taxi', 'skycash', 'parking', 'tfl']),
        ('Paliwo', ['orlen', 'circle k', 'shell', 'moya', 'bp ', 'lotos', 'stacja paliw']),
        ('Zdrowie', ['apteka', 'doz', 'medicover', 'lux med', 'lekarz', 'przychodnia']),
        ('Drogeria', ['rossmann', 'hebe', 'drogeria natura']),
        ('Uroda', ['douglas', 'rituals', 'sephora', 'fryzjer', 'barber', 'kosmetycz']),
        ('Ubrania', ['zalando', 'reserved', 'zara', 'hm ', 'h m', 'ccc', 'eobuwie', 'shein', 'vinted', 'van graaf']),
        ('Subskrypcje', ['netflix', 'spotify', 'icloud', 'apple com bill', 'google storage', 'youtube premium']),
        ('Rozrywka', ['steam', 'valve', 'playstation', 'xbox', 'kino', 'cinema', 'geoguessr']),
        ('Rachunki', ['czynsz', 'energia', 'prad', 'gaz', 'internet', 'orange', 'play ', 't mobile', 'vectra', 'upc', 'rachunek']),
        ('Inwestycje', ['xtb', 'maklerski', 'broker', 'revolut trading']),
        ('Sport', ['silownia', 'gym', 'fitness', 'decathlon']),
        ('Wyposazenie domu', ['ikea', 'leroy', 'castorama', 'obi', 'jysk', 'home you']),
        ('Edukacja', ['udemy', 'coursera', 'ebook', 'ksiazka', 'studia']),
        ('Prezenty', ['prezent']),
    ]
    for category, keywords in rules:
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
        for store_key, entry in self.expenses:
            if store_key in text or text in store_key:
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


def _candidate_from_row(index, row_number, row, account, import_source=MILLENNIUM_SOURCE, history=None):
    if history is None:
        history = BankHistoryIndex(account)
    debit = _optional_decimal(row.get('Obciążenia'))
    credit = _optional_decimal(row.get('Uznania'))
    if debit is None and credit is None:
        return None
    if debit is not None and credit is not None:
        raise ValueError('wiersz ma jednocześnie obciążenie i uznanie')

    transaction_date = _parse_date(row.get('Data transakcji'))
    if debit is not None:
        amount = abs(debit)
        store = _expense_store(row)
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

        currency = row.get('Waluta', '').upper()
        if currency and currency != 'PLN':
            warnings.append(f'Pominięto wiersz {row_number}: waluta {currency} nie jest obsługiwana.')
            continue

        try:
            candidate = _candidate_from_row(len(candidates), row_number, row, account, history=history)
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
