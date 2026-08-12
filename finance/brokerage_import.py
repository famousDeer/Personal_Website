from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
from pathlib import PurePosixPath
import re
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

from django.db import transaction
from django.utils import timezone

from utils.tools import parse_decimal

from .investment_funding import reconcile_cash_operation
from .models import (
    BrokerageAccount,
    BrokerageCashOperation,
    BrokerageInstrument,
    BrokeragePositionSnapshot,
    BrokeragePriceSnapshot,
    BrokerageTransaction,
)


XLSX_MAIN_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
XLSX_REL_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PACKAGE_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
NS = {
    'a': XLSX_MAIN_NS,
    'r': XLSX_REL_NS,
    'rel': PACKAGE_REL_NS,
}

XTB_IMPORT_SOURCE = 'xtb'
XTB_SNAPSHOT_SOURCE = 'XTB'
XTB_FILENAME_RE = re.compile(r'^(IKE|PLN|EUR|USD)_(\d+)_', re.IGNORECASE)
XTB_TRADE_COMMENT_RE = re.compile(
    r'^(OPEN|CLOSE)\s+(BUY|SELL)\s+([0-9.]+)(?:/([0-9.]+))?\s+@\s+([0-9.]+)$',
    re.IGNORECASE,
)


class BrokerageImportError(Exception):
    pass


@dataclass
class BrokerageImportResult:
    # The original public counters retain their legacy meaning: transactions.
    created: int = 0
    duplicates: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    cash_operations_created: int = 0
    cash_operations_duplicates: int = 0
    position_snapshots_created: int = 0
    position_snapshots_updated: int = 0
    position_snapshots_duplicates: int = 0
    price_snapshots_created: int = 0
    price_snapshots_updated: int = 0
    price_snapshots_duplicates: int = 0
    accounts_created: int = 0
    accounts_linked: int = 0
    files_processed: int = 0
    account: object = None
    accounts: list = field(default_factory=list)

    # Short aliases are convenient for callers while the explicit field names
    # remain unambiguous in logs and API responses.
    @property
    def cash_created(self):
        return self.cash_operations_created

    @property
    def cash_duplicates(self):
        return self.cash_operations_duplicates

    @property
    def snapshots_created(self):
        return self.position_snapshots_created

    @property
    def snapshots_updated(self):
        return self.position_snapshots_updated

    def absorb(self, other, warning_prefix=''):
        counter_fields = (
            'created',
            'duplicates',
            'skipped',
            'cash_operations_created',
            'cash_operations_duplicates',
            'position_snapshots_created',
            'position_snapshots_updated',
            'position_snapshots_duplicates',
            'price_snapshots_created',
            'price_snapshots_updated',
            'price_snapshots_duplicates',
            'accounts_created',
            'accounts_linked',
            'files_processed',
        )
        for name in counter_fields:
            setattr(self, name, getattr(self, name) + getattr(other, name))

        for warning in other.warnings:
            self.warnings.append(f'{warning_prefix}{warning}')

        for account in other.accounts:
            if all(existing.pk != account.pk for existing in self.accounts):
                self.accounts.append(account)
        self.account = self.accounts[0] if len(self.accounts) == 1 else None
        return self


@dataclass
class XtbTrade:
    position_id: str
    symbol: str
    transaction_type: str
    trade_time: datetime
    quantity: Decimal
    price: Decimal
    fees: Decimal
    external_id: str
    source_sheet: str
    instrument_name: str = ''
    category: str = ''
    notes: str = ''


@dataclass
class XtbMetadata:
    external_account_id: str = ''
    currency: str = ''
    account_type: str = BrokerageAccount.STANDARD
    filename_prefix: str = ''
    warnings: list[str] = field(default_factory=list)


def _clean_text(value):
    return str(value or '').replace('\xa0', ' ').strip()


def _normalize_label(value):
    return ' '.join(_clean_text(value).casefold().split())


def _record_get(record, *names):
    normalized_record = {_normalize_label(key): value for key, value in record.items()}
    for name in names:
        key = _normalize_label(name)
        if key in normalized_record:
            return normalized_record[key]
    return ''


def _column_index(cell_ref):
    letters = ''.join(char for char in cell_ref if char.isalpha())
    index = 0
    for char in letters:
        index = index * 26 + ord(char.upper()) - 64
    return index - 1


def _xlsx_datetime(value):
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value

    clean_value = _clean_text(value)
    try:
        serial = Decimal(clean_value)
    except (InvalidOperation, ValueError):
        serial = None

    if serial is not None and Decimal('20000') < serial < Decimal('70000'):
        whole_days = int(serial)
        seconds = int(
            ((serial - Decimal(whole_days)) * Decimal('86400')).quantize(
                Decimal('1'),
                rounding=ROUND_HALF_UP,
            )
        )
        if seconds >= 86400:
            whole_days += 1
            seconds -= 86400
        return datetime(1899, 12, 30) + timedelta(days=whole_days, seconds=seconds)

    try:
        return datetime.fromisoformat(clean_value.replace('Z', '+00:00'))
    except ValueError:
        pass

    for date_format in (
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M',
        '%d/%m/%Y %H:%M:%S',
        '%d/%m/%Y %H:%M',
    ):
        try:
            return datetime.strptime(clean_value, date_format)
        except ValueError:
            continue
    return None


def _aware_utc(value):
    if value is None:
        return None
    if timezone.is_naive(value):
        return value.replace(tzinfo=datetime_timezone.utc)
    return value.astimezone(datetime_timezone.utc)


def _optional_decimal(value):
    if value in (None, '') or not _clean_text(value):
        return None
    return parse_decimal(value)


def _canonical_identifier(value):
    clean_value = _clean_text(value)
    if not clean_value:
        return ''

    try:
        number = Decimal(clean_value)
    except (InvalidOperation, ValueError):
        return clean_value

    if not number.is_finite():
        return clean_value
    if number == number.to_integral_value():
        return format(number.quantize(Decimal('1')), 'f')
    return format(number.normalize(), 'f')


def _normalize_symbol(symbol):
    clean_symbol = _clean_text(symbol).upper()
    if clean_symbol.endswith('.PL') or clean_symbol.endswith('.WA'):
        return clean_symbol[:-3]
    return clean_symbol


def _exchange_for_symbol(symbol):
    clean_symbol = _clean_text(symbol).upper()
    return {
        'PL': 'XWAR',
        'WA': 'XWAR',
        'US': 'US',
        'NL': 'XAMS',
        'FR': 'XPAR',
        'UK': 'XLON',
        'DE': 'XETR',
    }.get(clean_symbol.rsplit('.', 1)[-1] if '.' in clean_symbol else '', '')


def _inferred_quote_currency(symbol):
    """Infer only unambiguous quote currencies from XTB's market suffix.

    London listings may be quoted in GBP, GBX or USD, so they deliberately
    return no guess until the market provider confirms it.
    """
    clean_symbol = _clean_text(symbol).upper()
    suffix = clean_symbol.rsplit('.', 1)[-1] if '.' in clean_symbol else ''
    return {
        'PL': 'PLN',
        'WA': 'PLN',
        'US': 'USD',
        'NL': 'EUR',
        'FR': 'EUR',
        'DE': 'EUR',
        'ES': 'EUR',
        'IT': 'EUR',
        'BE': 'EUR',
        'PT': 'EUR',
        'FI': 'EUR',
        'AT': 'EUR',
    }.get(suffix)


def _quote_currency_for_symbol(symbol, account_currency):
    return _inferred_quote_currency(symbol) or account_currency or 'PLN'


def _asset_type_for_category(category):
    normalized_category = _normalize_label(category)
    if not normalized_category:
        # Legacy XTB sheets did not expose a category and the previous importer
        # treated their instruments as stocks.
        return BrokerageInstrument.STOCK
    return {
        'stock': BrokerageInstrument.STOCK,
        'etf': BrokerageInstrument.ETF,
        'fund': BrokerageInstrument.FUND,
        'bond': BrokerageInstrument.BOND,
    }.get(normalized_category, BrokerageInstrument.OTHER)


def _cell_value(cell, shared_strings):
    cell_type = cell.attrib.get('t')
    if cell_type == 'inlineStr':
        return ''.join(text.text or '' for text in cell.findall('.//a:t', NS)).strip()

    value = cell.find('a:v', NS)
    if value is None:
        return ''

    raw_value = (value.text or '').strip()
    if cell_type == 's' and raw_value:
        try:
            return shared_strings[int(raw_value)].strip()
        except (IndexError, ValueError) as exc:
            raise BrokerageImportError('Plik XLSX zawiera nieprawidłowe odwołanie do tekstu.') from exc
    return raw_value


def _load_shared_strings(archive):
    if 'xl/sharedStrings.xml' not in archive.namelist():
        return []

    root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
    shared_strings = []
    for item in root.findall('a:si', NS):
        shared_strings.append(''.join(text.text or '' for text in item.findall('.//a:t', NS)))
    return shared_strings


def _sheet_targets(archive):
    workbook = ET.fromstring(archive.read('xl/workbook.xml'))
    rels = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
    rel_map = {rel.attrib['Id']: rel.attrib['Target'] for rel in rels}
    sheets = {}
    for sheet in workbook.findall('.//a:sheet', NS):
        rel_id = sheet.attrib[f'{{{XLSX_REL_NS}}}id']
        raw_target = rel_map[rel_id].lstrip('/')
        target = PurePosixPath(raw_target)
        if not str(target).startswith('xl/'):
            target = PurePosixPath('xl') / target
        sheets[_clean_text(sheet.attrib['name'])] = str(target)
    return sheets


def _worksheet_rows(archive, target, shared_strings):
    root = ET.fromstring(archive.read(target))
    rows = []
    for row in root.findall('.//a:sheetData/a:row', NS):
        values = {}
        max_index = -1
        for cell in row.findall('a:c', NS):
            index = _column_index(cell.attrib.get('r', 'A1'))
            values[index] = _cell_value(cell, shared_strings)
            max_index = max(max_index, index)
        rows.append([values.get(index, '') for index in range(max_index + 1)])
    return rows


def _records_from_sheet(rows, required_headers):
    header_index = None
    normalized_required = {_normalize_label(header) for header in required_headers}
    for index, row in enumerate(rows):
        normalized_row = {_normalize_label(value) for value in row if _clean_text(value)}
        if normalized_required.issubset(normalized_row):
            header_index = index
            break

    if header_index is None:
        return []

    headers = [_clean_text(value) for value in rows[header_index]]
    records = []
    for row in rows[header_index + 1:]:
        record = {
            header: row[index] if index < len(row) else ''
            for index, header in enumerate(headers)
            if header
        }
        nonempty_values = [_clean_text(value) for value in record.values() if _clean_text(value)]
        if not nonempty_values:
            continue
        first_value = _normalize_label(nonempty_values[0])
        if first_value in {'total', 'profit/loss'}:
            continue
        normalized_values = {_normalize_label(value) for value in nonempty_values}
        if normalized_required.issubset(normalized_values):
            continue
        records.append(record)
    return records


def _read_xtb_workbook(uploaded_file):
    try:
        if hasattr(uploaded_file, 'seek'):
            uploaded_file.seek(0)
        data = uploaded_file.read()
        archive = ZipFile(BytesIO(data))
    except (BadZipFile, OSError, TypeError, AttributeError) as exc:
        raise BrokerageImportError('Nie udało się odczytać pliku XLSX z XTB.') from exc

    try:
        with archive:
            shared_strings = _load_shared_strings(archive)
            sheets = _sheet_targets(archive)
            workbook_rows = {
                sheet_name: _worksheet_rows(archive, target, shared_strings)
                for sheet_name, target in sheets.items()
            }
    except (BadZipFile, ET.ParseError, KeyError, OSError) as exc:
        raise BrokerageImportError('Plik XLSX z XTB ma nieprawidłową strukturę.') from exc
    return workbook_rows


def _metadata_value(rows, label):
    wanted = _normalize_label(label)
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            if _normalize_label(value) != wanted:
                continue
            if column_index + 1 < len(row) and _clean_text(row[column_index + 1]):
                return row[column_index + 1]
            if row_index + 1 < len(rows) and column_index < len(rows[row_index + 1]):
                below = rows[row_index + 1][column_index]
                if _clean_text(below):
                    return below
    return ''


def _read_account_currency(workbook_rows):
    supported = {choice[0] for choice in BrokerageAccount.CURRENCY_CHOICES}
    for rows in workbook_rows.values():
        currency = _clean_text(_metadata_value(rows, 'Currency')).upper()
        if currency in supported:
            return currency
    return ''


def _sheet_account_ids(workbook_rows, sheet_prefix):
    account_ids = []
    normalized_prefix = _normalize_label(sheet_prefix)
    for sheet_name, rows in workbook_rows.items():
        if not _normalize_label(sheet_name).startswith(normalized_prefix):
            continue
        account_id = _canonical_identifier(_metadata_value(rows, 'Account number'))
        if account_id and account_id not in account_ids:
            account_ids.append(account_id)
    return account_ids


def _filename_hints(uploaded_file):
    filename = PurePosixPath(_clean_text(getattr(uploaded_file, 'name', '')).replace('\\', '/')).name
    match = XTB_FILENAME_RE.match(filename)
    if not match:
        return '', ''
    return match.group(1).upper(), _canonical_identifier(match.group(2))


def _workbook_mentions_ike_product(workbook_rows):
    for rows in workbook_rows.values():
        records = _records_from_sheet(rows, ['Product'])
        for record in records:
            if _normalize_label(_record_get(record, 'Product')) == 'ike':
                return True
    return False


def _read_xtb_metadata(workbook_rows, uploaded_file, selected_account=None):
    prefix, filename_account_id = _filename_hints(uploaded_file)
    cash_ids = _sheet_account_ids(workbook_rows, 'Cash Operations')
    closed_ids = _sheet_account_ids(workbook_rows, 'Closed Positions')
    open_ids = _sheet_account_ids(workbook_rows, 'Open Positions')

    if len(cash_ids) > 1 or len(closed_ids) > 1:
        raise BrokerageImportError('Arkusze XTB zawierają więcej niż jeden numer konta.')

    reliable_sources = []
    if filename_account_id:
        reliable_sources.append(('nazwa pliku', filename_account_id))
    if cash_ids:
        reliable_sources.append(('Cash Operations', cash_ids[0]))
    if closed_ids:
        reliable_sources.append(('Closed Positions', closed_ids[0]))

    warnings = []
    external_account_id = ''
    if reliable_sources:
        counts = Counter(value for _, value in reliable_sources)
        highest_count = max(counts.values())
        winners = [value for value, count in counts.items() if count == highest_count]
        selected_external_id = _canonical_identifier(
            getattr(selected_account, 'external_account_id', '') if selected_account else ''
        )
        if len(winners) == 1:
            external_account_id = winners[0]
        elif selected_external_id and selected_external_id in winners:
            external_account_id = selected_external_id
        else:
            details = ', '.join(f'{source}: {value}' for source, value in reliable_sources)
            raise BrokerageImportError(f'Nie można jednoznacznie ustalić numeru konta XTB ({details}).')

        mismatches = [
            f'{source}: {value}'
            for source, value in reliable_sources
            if value != external_account_id
        ]
        if mismatches:
            warnings.append(
                f'Niespójny numer konta w eksporcie XTB ({", ".join(mismatches)}); '
                f'użyto numeru {external_account_id} potwierdzonego przez pozostałe metadane.'
            )

    for open_account_id in open_ids:
        if external_account_id and open_account_id != external_account_id:
            warning_prefix = 'Eksport IKE XTB' if prefix == 'IKE' else 'Arkusz Open Positions'
            warnings.append(
                f'{warning_prefix} podaje numer konta {open_account_id}, podczas gdy nazwa pliku oraz '
                f'arkusze historii wskazują {external_account_id}. Open Positions użyto tylko do wyceny.'
            )

    workbook_currency = _read_account_currency(workbook_rows)
    if prefix == 'IKE':
        currency = 'PLN'
        account_type = BrokerageAccount.IKE
    elif prefix in {'PLN', 'EUR', 'USD'}:
        currency = prefix
        account_type = BrokerageAccount.STANDARD
    else:
        currency = workbook_currency
        account_type = (
            BrokerageAccount.IKE
            if _workbook_mentions_ike_product(workbook_rows)
            else BrokerageAccount.STANDARD
        )
        if account_type == BrokerageAccount.IKE and not currency:
            currency = 'PLN'

    if workbook_currency and currency and workbook_currency != currency:
        warnings.append(
            f'Nazwa pliku wskazuje walutę {currency}, a podsumowanie Open Positions {workbook_currency}; '
            f'użyto waluty {currency}.'
        )

    return XtbMetadata(
        external_account_id=external_account_id,
        currency=currency,
        account_type=account_type,
        filename_prefix=prefix,
        warnings=warnings,
    )


def _unique_account_name(user, base_name, external_account_id):
    if not BrokerageAccount.objects.filter(user=user, name=base_name).exists():
        return base_name

    suffix = external_account_id or 'import'
    candidate = f'{base_name} ({suffix})'[:120]
    if not BrokerageAccount.objects.filter(user=user, name=candidate).exists():
        return candidate

    counter = 2
    while True:
        counter_suffix = f' {counter}'
        candidate = f'{base_name} ({suffix})'[:120 - len(counter_suffix)] + counter_suffix
        if not BrokerageAccount.objects.filter(user=user, name=candidate).exists():
            return candidate
        counter += 1


def _add_result_account(result, account):
    result.account = account
    if all(existing.pk != account.pk for existing in result.accounts):
        result.accounts.append(account)


def _resolve_account(user, selected_account, metadata, result):
    if selected_account is not None:
        if selected_account.user_id != user.id:
            raise BrokerageImportError('Wybrane konto maklerskie nie należy do zalogowanego użytkownika.')
        if selected_account.broker != BrokerageAccount.BROKER_XTB:
            raise BrokerageImportError('Import XLSX jest przygotowany dla eksportu z XTB.')
        account = selected_account

        if metadata.external_account_id:
            current_external_id = _canonical_identifier(account.external_account_id)
            if current_external_id and current_external_id != metadata.external_account_id:
                raise BrokerageImportError(
                    f'Plik dotyczy konta XTB {metadata.external_account_id}, a wybrane konto ma numer '
                    f'{current_external_id}.'
                )
            if not current_external_id:
                conflicting_account = (
                    BrokerageAccount.objects
                    .filter(
                        user=user,
                        broker=BrokerageAccount.BROKER_XTB,
                        external_account_id=metadata.external_account_id,
                    )
                    .exclude(pk=account.pk)
                    .first()
                )
                if conflicting_account is not None:
                    raise BrokerageImportError(
                        f'Numer {metadata.external_account_id} jest już przypisany do konta '
                        f'„{conflicting_account.name}”.'
                    )
                account.external_account_id = metadata.external_account_id
                account.save(update_fields=['external_account_id'])
                result.accounts_linked += 1
    else:
        account = None
        if metadata.external_account_id:
            account = (
                BrokerageAccount.objects
                .filter(
                    user=user,
                    broker=BrokerageAccount.BROKER_XTB,
                    external_account_id=metadata.external_account_id,
                )
                .first()
            )

        matching_accounts = BrokerageAccount.objects.none()
        if account is None and metadata.currency:
            matching_accounts = (
                BrokerageAccount.objects
                .filter(
                    user=user,
                    broker=BrokerageAccount.BROKER_XTB,
                    currency=metadata.currency,
                    account_type=metadata.account_type,
                    external_account_id='',
                )
                .order_by('id')
            )
            candidates = list(matching_accounts[:2])
            if len(candidates) == 1:
                account = candidates[0]
                if metadata.external_account_id:
                    account.external_account_id = metadata.external_account_id
                    account.save(update_fields=['external_account_id'])
                    result.accounts_linked += 1

        if account is None:
            if not metadata.external_account_id:
                raise BrokerageImportError(
                    'Nie udało się ustalić numeru konta XTB. Wybierz konto ręcznie albo użyj '
                    'oryginalnego pliku eksportu XTB.'
                )
            if not metadata.currency:
                raise BrokerageImportError('Nie udało się ustalić waluty konta z eksportu XTB.')

            if metadata.account_type == BrokerageAccount.IKE:
                base_name = 'XTB IKE'
            else:
                base_name = f'XTB {metadata.currency}'
            account = BrokerageAccount.objects.create(
                user=user,
                name=_unique_account_name(user, base_name, metadata.external_account_id),
                broker=BrokerageAccount.BROKER_XTB,
                account_type=metadata.account_type,
                currency=metadata.currency,
                external_account_id=metadata.external_account_id,
            )
            result.accounts_created += 1

    if metadata.currency and metadata.currency != account.currency:
        result.warnings.append(
            f'Waluta w pliku XTB to {metadata.currency}, a wybrane konto ma walutę {account.currency}. '
            'Operacje zapisano w walucie wybranego konta.'
        )
    if metadata.account_type != account.account_type:
        expected = 'IKE' if metadata.account_type == BrokerageAccount.IKE else 'zwykłe'
        actual = 'IKE' if account.account_type == BrokerageAccount.IKE else 'zwykłe'
        result.warnings.append(
            f'Plik wskazuje konto {expected}, a wybrane konto jest oznaczone jako {actual}. '
            'Nie zmieniono typu istniejącego konta.'
        )

    _add_result_account(result, account)
    return account


def _find_or_create_instrument_data(user, symbol, account_currency, instrument_name='', category=''):
    original_symbol = _clean_text(symbol).upper()
    ticker = _normalize_symbol(original_symbol)
    if not ticker:
        return None

    instrument = (
        BrokerageInstrument.objects
        .filter(user=user, price_symbol__iexact=original_symbol)
        .first()
    )
    if instrument is None:
        instrument = (
            BrokerageInstrument.objects
            .filter(user=user, ticker__iexact=ticker)
            .first()
        )
    if instrument is None:
        instrument = BrokerageInstrument.objects.create(
            user=user,
            ticker=ticker,
            name=_clean_text(instrument_name) or ticker,
            price_symbol=original_symbol,
            exchange=_exchange_for_symbol(original_symbol),
            asset_type=_asset_type_for_category(category),
            currency=_quote_currency_for_symbol(original_symbol, account_currency),
        )

    changed_fields = []
    if not instrument.price_symbol:
        instrument.price_symbol = original_symbol
        changed_fields.append('price_symbol')
    if not instrument.exchange:
        exchange = _exchange_for_symbol(original_symbol)
        if exchange:
            instrument.exchange = exchange
            changed_fields.append('exchange')

    quote_currency = _inferred_quote_currency(original_symbol)
    if quote_currency and instrument.currency != quote_currency:
        instrument.currency = quote_currency
        changed_fields.append('currency')

    imported_name = _clean_text(instrument_name)
    if imported_name and (not instrument.name or instrument.name in {instrument.ticker, instrument.price_symbol}):
        instrument.name = imported_name[:160]
        changed_fields.append('name')

    imported_asset_type = _asset_type_for_category(category)
    if imported_asset_type != BrokerageInstrument.OTHER and instrument.asset_type == BrokerageInstrument.OTHER:
        instrument.asset_type = imported_asset_type
        changed_fields.append('asset_type')

    if changed_fields:
        instrument.save(update_fields=changed_fields)
    return instrument


def _find_or_create_instrument(user, trade, account_currency):
    return _find_or_create_instrument_data(
        user,
        trade.symbol,
        account_currency,
        instrument_name=trade.instrument_name,
        category=trade.category,
    )


def _trade_from_open_position(record, account, account_number):
    position_id = _canonical_identifier(_record_get(record, 'Position', 'Position ID'))
    symbol = _clean_text(_record_get(record, 'Symbol', 'Ticker')).upper()
    transaction_type = _clean_text(_record_get(record, 'Type')).upper()
    trade_time = _xlsx_datetime(_record_get(record, 'Open time', 'Open Time (UTC)'))
    if not all((position_id, symbol, transaction_type, trade_time)):
        raise ValueError('brak identyfikatora, symbolu, typu albo daty otwarcia')
    if transaction_type not in {'BUY', 'SELL'}:
        raise ValueError(f'nieobsługiwany typ transakcji {transaction_type}')

    mapped_type = BrokerageTransaction.BUY if transaction_type == 'BUY' else BrokerageTransaction.SELL
    quantity = parse_decimal(_record_get(record, 'Volume'))
    price = parse_decimal(_record_get(record, 'Open price', 'Open Price'))
    if quantity <= 0 or price <= 0:
        raise ValueError('wolumen i cena muszą być dodatnie')
    return XtbTrade(
        position_id=position_id,
        symbol=symbol,
        transaction_type=mapped_type,
        trade_time=trade_time,
        quantity=quantity,
        price=price,
        fees=abs(parse_decimal(_record_get(record, 'Commission', 'Open Commission') or '0')),
        external_id=f'xtb:{account.id}:position:{position_id}:open',
        source_sheet='OPEN POSITION HISTORY',
        instrument_name=_clean_text(_record_get(record, 'Instrument')),
        category=_clean_text(_record_get(record, 'Category')),
    )


def _trades_from_closed_position(record, account, account_number):
    position_id = _canonical_identifier(_record_get(record, 'Position', 'Position ID'))
    symbol = _clean_text(_record_get(record, 'Symbol', 'Ticker')).upper()
    position_type = _clean_text(_record_get(record, 'Type')).upper()
    open_time = _xlsx_datetime(_record_get(record, 'Open time', 'Open Time (UTC)'))
    close_time = _xlsx_datetime(_record_get(record, 'Close time', 'Close Time (UTC)'))
    if not all((position_id, symbol, position_type, open_time, close_time)):
        raise ValueError('brak identyfikatora, symbolu, typu, daty otwarcia albo daty zamknięcia')
    if position_type not in {'BUY', 'SELL'}:
        raise ValueError(f'nieobsługiwany typ transakcji {position_type}')

    if position_type == 'BUY':
        open_type = BrokerageTransaction.BUY
        close_type = BrokerageTransaction.SELL
    else:
        open_type = BrokerageTransaction.SELL
        close_type = BrokerageTransaction.BUY

    commission = abs(parse_decimal(_record_get(record, 'Commission') or '0'))
    quantity = parse_decimal(_record_get(record, 'Volume'))
    open_price = parse_decimal(_record_get(record, 'Open price', 'Open Price'))
    close_price = parse_decimal(_record_get(record, 'Close price', 'Close Price'))
    if quantity <= 0 or open_price <= 0 or close_price <= 0:
        raise ValueError('wolumen i ceny muszą być dodatnie')
    common = {
        'position_id': position_id,
        'symbol': symbol,
        'quantity': quantity,
        'instrument_name': _clean_text(_record_get(record, 'Instrument')),
        'category': _clean_text(_record_get(record, 'Category')),
    }
    return [
        XtbTrade(
            **common,
            transaction_type=open_type,
            trade_time=open_time,
            price=open_price,
            fees=Decimal('0.00'),
            external_id=f'xtb:{account.id}:position:{position_id}:open',
            source_sheet='CLOSED POSITION HISTORY',
        ),
        XtbTrade(
            **common,
            transaction_type=close_type,
            trade_time=close_time,
            price=close_price,
            fees=commission,
            external_id=f'xtb:{account.id}:position:{position_id}:close',
            source_sheet='CLOSED POSITION HISTORY',
        ),
    ]


def _trade_from_cash_operation(record, account):
    cash_id = _canonical_identifier(_record_get(record, 'ID'))
    position_id = _canonical_identifier(_record_get(record, 'Position ID'))
    symbol = _clean_text(_record_get(record, 'Ticker')).upper()
    comment = _clean_text(_record_get(record, 'Comment'))
    trade_time = _xlsx_datetime(_record_get(record, 'Time'))
    match = XTB_TRADE_COMMENT_RE.fullmatch(comment)
    if not all((cash_id, symbol, trade_time, match)):
        raise ValueError('brak ID, tickera, czasu albo nierozpoznany komentarz transakcji')

    action, side, quantity_value, _total_quantity, price_value = match.groups()
    action = action.upper()
    side = side.upper()
    if action == 'OPEN':
        mapped_type = BrokerageTransaction.BUY if side == 'BUY' else BrokerageTransaction.SELL
    else:
        mapped_type = BrokerageTransaction.SELL if side == 'BUY' else BrokerageTransaction.BUY

    quantity = parse_decimal(quantity_value)
    price = parse_decimal(price_value)
    if quantity <= 0 or price <= 0:
        raise ValueError('wolumen i cena muszą być dodatnie')
    return XtbTrade(
        position_id=position_id or cash_id,
        symbol=symbol,
        transaction_type=mapped_type,
        trade_time=trade_time,
        quantity=quantity,
        price=price,
        fees=Decimal('0.00'),
        external_id=f'xtb:{account.id}:cash:{cash_id}',
        source_sheet='Cash Operations',
        instrument_name=_clean_text(_record_get(record, 'Instrument')),
        category=_clean_text(_record_get(record, 'Category')),
        notes=comment,
    )


def _trade_identity(trade):
    return (
        _normalize_symbol(trade.symbol),
        trade.transaction_type,
        trade.trade_time.replace(microsecond=0),
        trade.quantity,
        trade.price,
    )


def _xtb_trades(workbook_rows, account, account_number):
    cash_trades = []
    legacy_open_trades = []
    closed_trades = []
    warnings = []

    for sheet_name, rows in workbook_rows.items():
        normalized_sheet = _normalize_label(sheet_name)
        if normalized_sheet.startswith('cash operations'):
            records = _records_from_sheet(
                rows,
                ['Type', 'Instrument', 'Ticker', 'Time', 'Amount', 'ID', 'Comment', 'Product', 'Position ID'],
            )
            for record in records:
                raw_type = _normalize_label(_record_get(record, 'Type'))
                if raw_type not in {'stock purchase', 'stock sell'}:
                    continue
                try:
                    cash_trades.append(_trade_from_cash_operation(record, account))
                except (ValueError, ArithmeticError) as exc:
                    warnings.append(f'Pominięto transakcję z arkusza {sheet_name}: {exc}.')

        elif normalized_sheet.startswith('open position'):
            # The historical legacy format represented trades here. The current
            # format uses aggregate/lot rows and is handled as snapshots below.
            records = _records_from_sheet(
                rows,
                ['Position', 'Symbol', 'Type', 'Volume', 'Open time', 'Open price'],
            )
            for record in records:
                try:
                    legacy_open_trades.append(_trade_from_open_position(record, account, account_number))
                except (ValueError, ArithmeticError) as exc:
                    warnings.append(f'Pominięto wiersz z arkusza {sheet_name}: {exc}.')

        elif normalized_sheet.startswith('closed position'):
            records = _records_from_sheet(
                rows,
                ['Ticker', 'Type', 'Volume', 'Open Price', 'Open Time (UTC)', 'Close Price', 'Close Time (UTC)', 'Position ID'],
            )
            if not records:
                records = _records_from_sheet(
                    rows,
                    ['Position', 'Symbol', 'Type', 'Volume', 'Open time', 'Open price', 'Close time', 'Close price'],
                )
            for record in records:
                try:
                    closed_trades.extend(_trades_from_closed_position(record, account, account_number))
                except (ValueError, ArithmeticError) as exc:
                    warnings.append(f'Pominięto wiersz z arkusza {sheet_name}: {exc}.')

    # Cash-operation IDs are the stable transaction identity in current XTB
    # exports. A single cash purchase can later be split into several rows in
    # Closed Positions during partial closes, so mixing both sources would
    # double-count the original buy. Closed Positions is therefore a whole-file
    # fallback only when no parseable cash trades are present.
    trades = cash_trades + legacy_open_trades
    identities = {_trade_identity(trade) for trade in trades}
    if not cash_trades:
        for trade in closed_trades:
            identity = _trade_identity(trade)
            if identity in identities:
                continue
            trades.append(trade)
            identities.add(identity)
    return trades, warnings


def _find_duplicate(account, instrument, trade):
    existing = BrokerageTransaction.objects.filter(account=account, external_id=trade.external_id).first()
    if existing is not None:
        return existing

    # Current exports provide a globally stable cash-operation ID. Two fills
    # can otherwise have the same second, ticker, quantity and price, so fuzzy
    # matching would incorrectly collapse valid executions.
    if _normalize_label(trade.source_sheet) == 'cash operations':
        return None

    return (
        BrokerageTransaction.objects
        .filter(
            account=account,
            instrument=instrument,
            transaction_type=trade.transaction_type,
            trade_date=trade.trade_time.date(),
            trade_time=trade.trade_time.time().replace(microsecond=0),
            quantity=trade.quantity,
            price=trade.price,
        )
        .first()
    )


def _operation_type_for_xtb(raw_type):
    return {
        'stock purchase': BrokerageCashOperation.BUY,
        'stock sell': BrokerageCashOperation.SELL,
        'dividend': BrokerageCashOperation.DIVIDEND,
        'dividend from foreign company on pl market': BrokerageCashOperation.DIVIDEND,
        'ike deposit': BrokerageCashOperation.INTERNAL_TRANSFER,
        'withholding tax': BrokerageCashOperation.WITHHOLDING_TAX,
        'deposit': BrokerageCashOperation.DEPOSIT,
        'withdrawal': BrokerageCashOperation.WITHDRAWAL,
        'free funds interest': BrokerageCashOperation.INTEREST,
        'free funds interest tax': BrokerageCashOperation.INTEREST_TAX,
        'transfer': BrokerageCashOperation.INTERNAL_TRANSFER,
        'commission': BrokerageCashOperation.FEE,
        'fee': BrokerageCashOperation.FEE,
    }.get(_normalize_label(raw_type), BrokerageCashOperation.OTHER)


def _cash_operation_records(workbook_rows):
    records = []
    for sheet_name, rows in workbook_rows.items():
        if not _normalize_label(sheet_name).startswith('cash operations'):
            continue
        for record in _records_from_sheet(
            rows,
            ['Type', 'Instrument', 'Ticker', 'Category', 'Time', 'Amount', 'ID', 'Comment', 'Product', 'Position ID'],
        ):
            records.append((sheet_name, record))
    return records


def _import_cash_operations(user, account, workbook_rows, result):
    recognized = 0
    for sheet_name, record in _cash_operation_records(workbook_rows):
        raw_type = _clean_text(_record_get(record, 'Type'))
        external_id = _canonical_identifier(_record_get(record, 'ID'))
        if not raw_type:
            continue
        if not external_id:
            result.skipped += 1
            result.warnings.append(f'Pominięto operację z arkusza {sheet_name}: brak ID operacji.')
            continue

        try:
            occurred_at = _aware_utc(_xlsx_datetime(_record_get(record, 'Time')))
            amount = parse_decimal(_record_get(record, 'Amount'))
            if occurred_at is None:
                raise ValueError('brak lub nieprawidłowy czas operacji')
        except (ValueError, ArithmeticError) as exc:
            result.skipped += 1
            result.warnings.append(f'Pominięto operację {external_id} z arkusza {sheet_name}: {exc}.')
            continue

        ticker = _clean_text(_record_get(record, 'Ticker')).upper()
        instrument = None
        if ticker:
            instrument = _find_or_create_instrument_data(
                user,
                ticker,
                account.currency,
                instrument_name=_record_get(record, 'Instrument'),
                category=_record_get(record, 'Category'),
            )

        operation_type = _operation_type_for_xtb(raw_type)
        comment = _clean_text(_record_get(record, 'Comment'))
        description = f'{raw_type}: {comment}' if comment else raw_type
        defaults = {
            'instrument': instrument,
            'operation_type': operation_type,
            'occurred_at': occurred_at,
            'amount': amount,
            'currency': account.currency,
            'position_external_id': _canonical_identifier(_record_get(record, 'Position ID')),
            'description': description[:500],
            'product': _clean_text(_record_get(record, 'Product'))[:255],
        }
        cash_operation, created = BrokerageCashOperation.objects.get_or_create(
            account=account,
            import_source=XTB_IMPORT_SOURCE,
            external_id=external_id,
            defaults=defaults,
        )
        recognized += 1
        if created:
            result.cash_operations_created += 1
        else:
            result.cash_operations_duplicates += 1

        if operation_type == BrokerageCashOperation.OTHER:
            result.warnings.append(
                f'Operację XTB „{raw_type}” zapisano jako inną operację; oryginalny typ zachowano w opisie.'
            )
        if _normalize_label(raw_type) == 'deposit':
            reconcile_cash_operation(cash_operation)
    return recognized


def _import_transactions(user, account, workbook_rows, result):
    trades, warnings = _xtb_trades(workbook_rows, account, account.external_account_id or account.name)
    result.warnings.extend(warnings)

    for trade in trades:
        instrument = _find_or_create_instrument(user, trade, account.currency)
        duplicate = _find_duplicate(account, instrument, trade)
        if duplicate is not None:
            if not duplicate.external_id:
                duplicate.external_id = trade.external_id
                duplicate.import_source = XTB_IMPORT_SOURCE
                duplicate.save(update_fields=['external_id', 'import_source'])
            result.duplicates += 1
            continue

        aware_time = _aware_utc(trade.trade_time)
        BrokerageTransaction.objects.create(
            account=account,
            instrument=instrument,
            transaction_type=trade.transaction_type,
            trade_date=aware_time.date(),
            trade_time=aware_time.time().replace(microsecond=0),
            quantity=trade.quantity,
            price=trade.price,
            fees=trade.fees,
            import_source=XTB_IMPORT_SOURCE,
            external_id=trade.external_id,
            notes=(trade.notes or f'Import XTB, pozycja {trade.position_id}')[:255],
        )
        result.created += 1
    return len(trades)


def _update_model(instance, defaults):
    changed_fields = []
    for field_name, value in defaults.items():
        if getattr(instance, field_name) != value:
            setattr(instance, field_name, value)
            changed_fields.append(field_name)
    if changed_fields:
        instance.save(update_fields=changed_fields)
    return bool(changed_fields)


def _import_open_position_snapshots(user, account, workbook_rows, result):
    recognized = 0
    for sheet_name, rows in workbook_rows.items():
        normalized_sheet = _normalize_label(sheet_name)
        if not normalized_sheet.startswith('open position'):
            continue

        records = _records_from_sheet(
            rows,
            ['Product', 'Instrument/Position', 'Ticker', 'Category', 'Type', 'Volume', 'Value', 'Current price'],
        )
        if not records:
            continue

        as_of = _aware_utc(_xlsx_datetime(_metadata_value(rows, 'Data as of report generated')))
        if as_of is None:
            result.warnings.append(
                f'Pominięto wycenę z arkusza {sheet_name}: brak czasu wygenerowania raportu.'
            )
            result.skipped += 1
            continue

        prices_by_ticker = {}
        for record in records:
            ticker = _clean_text(_record_get(record, 'Ticker')).upper()
            if not ticker:
                continue
            try:
                current_price = _optional_decimal(_record_get(record, 'Current price'))
            except (ValueError, ArithmeticError):
                current_price = None
            if current_price is not None and current_price > 0:
                prices_by_ticker.setdefault(ticker, current_price)

        aggregates = {}
        for record in records:
            category = _normalize_label(_record_get(record, 'Category'))
            position_type = _clean_text(_record_get(record, 'Type'))
            ticker = _clean_text(_record_get(record, 'Ticker')).upper()
            if category not in {'stock', 'etf'} or position_type or not ticker:
                continue
            try:
                quantity = parse_decimal(_record_get(record, 'Volume'))
                market_value = parse_decimal(_record_get(record, 'Value'))
                profit = _optional_decimal(_record_get(record, 'Net Profit'))
                profit_percent = _optional_decimal(_record_get(record, 'Net Profit %'))
            except (ValueError, ArithmeticError) as exc:
                result.skipped += 1
                result.warnings.append(
                    f'Pominięto pozycję {ticker} z arkusza {sheet_name}: {exc}.'
                )
                continue
            if quantity <= 0:
                result.skipped += 1
                result.warnings.append(
                    f'Pominięto pozycję {ticker} z arkusza {sheet_name}: wolumen nie jest dodatni.'
                )
                continue

            data = aggregates.get(ticker)
            if data is None:
                aggregates[ticker] = {
                    'quantity': quantity,
                    'market_value': market_value,
                    'current_price': prices_by_ticker.get(ticker),
                    'profit': profit,
                    'profit_percent': profit_percent,
                    'instrument_name': _clean_text(_record_get(record, 'Instrument/Position')),
                    'category': category,
                }
            else:
                data['quantity'] += quantity
                data['market_value'] += market_value
                data['profit'] = (
                    data['profit'] + profit
                    if data['profit'] is not None and profit is not None
                    else None
                )
                data['profit_percent'] = None

        for ticker, data in aggregates.items():
            instrument = _find_or_create_instrument_data(
                user,
                ticker,
                account.currency,
                instrument_name=data['instrument_name'],
                category=data['category'],
            )
            snapshot_defaults = {
                'quantity': data['quantity'],
                'market_value': data['market_value'],
                'current_price': data['current_price'],
                'profit': data['profit'],
                'profit_percent': data['profit_percent'],
                'currency': account.currency,
                'source': XTB_SNAPSHOT_SOURCE,
            }
            snapshot, created = BrokeragePositionSnapshot.objects.get_or_create(
                account=account,
                instrument=instrument,
                as_of=as_of,
                defaults=snapshot_defaults,
            )
            recognized += 1
            if created:
                result.position_snapshots_created += 1
            elif _update_model(snapshot, snapshot_defaults):
                result.position_snapshots_updated += 1
            else:
                result.position_snapshots_duplicates += 1

            current_price = data['current_price']
            if current_price is None:
                continue
            price_defaults = {
                'price': current_price,
                'source': XTB_SNAPSHOT_SOURCE,
            }
            price_snapshot, price_created = BrokeragePriceSnapshot.objects.get_or_create(
                instrument=instrument,
                observed_at=as_of,
                defaults=price_defaults,
            )
            if price_created:
                result.price_snapshots_created += 1
            elif _update_model(price_snapshot, price_defaults):
                result.price_snapshots_updated += 1
            else:
                result.price_snapshots_duplicates += 1

            if instrument.last_price_at is None or as_of >= instrument.last_price_at:
                instrument.last_price = current_price
                instrument.last_price_at = as_of
                instrument.market_data_source = XTB_SNAPSHOT_SOURCE
                instrument.save(update_fields=['last_price', 'last_price_at', 'market_data_source'])
    return recognized


@transaction.atomic
def import_xtb_transactions(user, account, uploaded_file):
    """Import one legacy or current XTB XLSX export.

    ``account`` may be ``None``. In that case the original XTB filename and the
    Cash/Closed account metadata are used to bind an existing account or create
    a new one. Passing an account retains the legacy view/API behaviour.
    """
    # MultipleFileField deliberately returns a list even for one upload. Keep
    # the long-standing entry point usable while newer callers can invoke
    # import_xtb_files directly.
    if isinstance(uploaded_file, (list, tuple)):
        return import_xtb_files(user, uploaded_file, account=account)

    if account is not None:
        if account.user_id != user.id:
            raise BrokerageImportError('Wybrane konto maklerskie nie należy do zalogowanego użytkownika.')
        if account.broker != BrokerageAccount.BROKER_XTB:
            raise BrokerageImportError('Import XLSX jest przygotowany dla eksportu z XTB.')

    workbook_rows = _read_xtb_workbook(uploaded_file)
    result = BrokerageImportResult()
    metadata = _read_xtb_metadata(workbook_rows, uploaded_file, selected_account=account)
    result.warnings.extend(metadata.warnings)
    account = _resolve_account(user, account, metadata, result)

    cash_records = _import_cash_operations(user, account, workbook_rows, result)
    trade_records = _import_transactions(user, account, workbook_rows, result)
    snapshot_records = _import_open_position_snapshots(user, account, workbook_rows, result)
    if not any((cash_records, trade_records, snapshot_records)):
        raise BrokerageImportError(
            'Nie znaleziono operacji, transakcji ani pozycji w obsługiwanych arkuszach XTB.'
        )

    account.last_import_at = timezone.now()
    account.save(update_fields=['last_import_at'])
    result.files_processed = 1
    return result


@transaction.atomic
def import_xtb_files(user, uploaded_files, account=None):
    """Import several XTB exports and aggregate counters across their accounts."""
    files = list(uploaded_files or [])
    if not files:
        raise BrokerageImportError('Nie wybrano plików XLSX z XTB.')

    combined = BrokerageImportResult()
    for uploaded_file in files:
        file_result = import_xtb_transactions(user, account, uploaded_file)
        filename = PurePosixPath(
            _clean_text(getattr(uploaded_file, 'name', 'plik XTB')).replace('\\', '/')
        ).name
        prefix = f'{filename}: ' if len(files) > 1 else ''
        combined.absorb(file_result, warning_prefix=prefix)
    return combined
