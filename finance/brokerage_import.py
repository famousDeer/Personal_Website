from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

from django.db import transaction
from django.utils import timezone

from utils.tools import parse_decimal

from .models import BrokerageAccount, BrokerageInstrument, BrokerageTransaction


XLSX_MAIN_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
XLSX_REL_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PACKAGE_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
NS = {
    'a': XLSX_MAIN_NS,
    'r': XLSX_REL_NS,
    'rel': PACKAGE_REL_NS,
}


class BrokerageImportError(Exception):
    pass


@dataclass
class BrokerageImportResult:
    created: int = 0
    duplicates: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


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

    clean_value = str(value).strip()
    try:
        serial = float(clean_value)
    except ValueError:
        serial = None

    if serial is not None and 20000 < serial < 70000:
        whole_days = int(serial)
        seconds = int(round((serial - whole_days) * 86400))
        return datetime(1899, 12, 30) + timedelta(days=whole_days, seconds=seconds)

    for date_format in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M'):
        try:
            return datetime.strptime(clean_value, date_format)
        except ValueError:
            continue
    return None


def _normalize_symbol(symbol):
    clean_symbol = (symbol or '').strip().upper()
    if clean_symbol.endswith('.PL') or clean_symbol.endswith('.WA'):
        return clean_symbol[:-3]
    return clean_symbol


def _exchange_for_symbol(symbol):
    clean_symbol = (symbol or '').strip().upper()
    if clean_symbol.endswith('.PL') or clean_symbol.endswith('.WA'):
        return 'XWAR'
    return ''


def _cell_value(cell, shared_strings):
    cell_type = cell.attrib.get('t')
    if cell_type == 'inlineStr':
        return ''.join(text.text or '' for text in cell.findall('.//a:t', NS)).strip()

    value = cell.find('a:v', NS)
    if value is None:
        return ''

    raw_value = (value.text or '').strip()
    if cell_type == 's' and raw_value:
        return shared_strings[int(raw_value)].strip()
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
        target = PurePosixPath('xl') / rel_map[rel_id].lstrip('/')
        sheets[sheet.attrib['name'].strip()] = str(target)
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
    normalized_required = {header.lower() for header in required_headers}
    for index, row in enumerate(rows):
        normalized_row = {str(value).strip().lower() for value in row if str(value).strip()}
        if normalized_required.issubset(normalized_row):
            header_index = index
            break

    if header_index is None:
        return []

    headers = [str(value).strip() for value in rows[header_index]]
    records = []
    for row in rows[header_index + 1:]:
        record = {
            header: row[index] if index < len(row) else ''
            for index, header in enumerate(headers)
            if header
        }
        first_value = next((str(value).strip() for value in record.values() if str(value).strip()), '')
        if not first_value or first_value.lower() == 'total':
            continue
        records.append(record)
    return records


def _read_xtb_workbook(uploaded_file):
    try:
        data = uploaded_file.read()
        archive = ZipFile(BytesIO(data))
    except (BadZipFile, OSError) as exc:
        raise BrokerageImportError('Nie udało się odczytać pliku XLSX z XTB.') from exc

    with archive:
        shared_strings = _load_shared_strings(archive)
        sheets = _sheet_targets(archive)
        workbook_rows = {
            sheet_name: _worksheet_rows(archive, target, shared_strings)
            for sheet_name, target in sheets.items()
        }
    return workbook_rows


def _read_account_currency(workbook_rows):
    for rows in workbook_rows.values():
        for row_index, row in enumerate(rows[:12]):
            for index, value in enumerate(row):
                if str(value).strip().lower() == 'currency' and index + 1 < len(row):
                    currency = str(row[index + 1]).strip().upper()
                    if not currency and row_index + 1 < len(rows) and index < len(rows[row_index + 1]):
                        currency = str(rows[row_index + 1][index]).strip().upper()
                    if currency:
                        return currency
    return ''


def _trade_from_open_position(record, account, account_number):
    position_id = str(record.get('Position', '')).strip()
    symbol = str(record.get('Symbol', '')).strip().upper()
    transaction_type = str(record.get('Type', '')).strip().upper()
    trade_time = _xlsx_datetime(record.get('Open time'))
    if not all((position_id, symbol, transaction_type, trade_time)):
        raise ValueError('brak identyfikatora, symbolu, typu albo daty otwarcia')

    mapped_type = BrokerageTransaction.BUY if transaction_type == 'BUY' else BrokerageTransaction.SELL
    return XtbTrade(
        position_id=position_id,
        symbol=symbol,
        transaction_type=mapped_type,
        trade_time=trade_time,
        quantity=parse_decimal(record.get('Volume')),
        price=parse_decimal(record.get('Open price')),
        fees=abs(parse_decimal(record.get('Commission') or '0')),
        external_id=f'xtb:{account.id}:position:{position_id}:open',
        source_sheet='OPEN POSITION HISTORY',
    )


def _trades_from_closed_position(record, account, account_number):
    position_id = str(record.get('Position', '')).strip()
    symbol = str(record.get('Symbol', '')).strip().upper()
    position_type = str(record.get('Type', '')).strip().upper()
    open_time = _xlsx_datetime(record.get('Open time'))
    close_time = _xlsx_datetime(record.get('Close time'))
    if not all((position_id, symbol, position_type, open_time, close_time)):
        raise ValueError('brak identyfikatora, symbolu, typu, daty otwarcia albo daty zamknięcia')

    if position_type == 'BUY':
        open_type = BrokerageTransaction.BUY
        close_type = BrokerageTransaction.SELL
    else:
        open_type = BrokerageTransaction.SELL
        close_type = BrokerageTransaction.BUY

    commission = abs(parse_decimal(record.get('Commission') or '0'))
    quantity = parse_decimal(record.get('Volume'))
    return [
        XtbTrade(
            position_id=position_id,
            symbol=symbol,
            transaction_type=open_type,
            trade_time=open_time,
            quantity=quantity,
            price=parse_decimal(record.get('Open price')),
            fees=Decimal('0.00'),
            external_id=f'xtb:{account.id}:position:{position_id}:open',
            source_sheet='CLOSED POSITION HISTORY',
        ),
        XtbTrade(
            position_id=position_id,
            symbol=symbol,
            transaction_type=close_type,
            trade_time=close_time,
            quantity=quantity,
            price=parse_decimal(record.get('Close price')),
            fees=commission,
            external_id=f'xtb:{account.id}:position:{position_id}:close',
            source_sheet='CLOSED POSITION HISTORY',
        ),
    ]


def _xtb_trades(workbook_rows, account, account_number):
    trades = []
    warnings = []

    for sheet_name, rows in workbook_rows.items():
        normalized_sheet = sheet_name.upper()
        if normalized_sheet.startswith('OPEN POSITION'):
            records = _records_from_sheet(rows, ['Position', 'Symbol', 'Type', 'Volume', 'Open time', 'Open price'])
            for record in records:
                try:
                    trades.append(_trade_from_open_position(record, account, account_number))
                except (ValueError, ArithmeticError) as exc:
                    warnings.append(f'Pominięto wiersz z arkusza {sheet_name}: {exc}.')
        elif normalized_sheet.startswith('CLOSED POSITION'):
            records = _records_from_sheet(rows, ['Position', 'Symbol', 'Type', 'Volume', 'Open time', 'Open price', 'Close time', 'Close price'])
            for record in records:
                try:
                    trades.extend(_trades_from_closed_position(record, account, account_number))
                except (ValueError, ArithmeticError) as exc:
                    warnings.append(f'Pominięto wiersz z arkusza {sheet_name}: {exc}.')

    return trades, warnings


def _find_or_create_instrument(user, trade, account_currency):
    ticker = _normalize_symbol(trade.symbol)
    instrument = (
        BrokerageInstrument.objects
        .filter(user=user, price_symbol__iexact=trade.symbol)
        .first()
    )
    if instrument is None:
        instrument, _ = BrokerageInstrument.objects.get_or_create(
            user=user,
            ticker=ticker,
            defaults={
                'name': ticker,
                'price_symbol': trade.symbol,
                'exchange': _exchange_for_symbol(trade.symbol),
                'asset_type': BrokerageInstrument.STOCK,
                'currency': account_currency or 'PLN',
            },
        )

    changed_fields = []
    if not instrument.price_symbol:
        instrument.price_symbol = trade.symbol
        changed_fields.append('price_symbol')
    if not instrument.exchange:
        instrument.exchange = _exchange_for_symbol(trade.symbol)
        changed_fields.append('exchange')
    if changed_fields:
        instrument.save(update_fields=changed_fields)
    return instrument


def _find_duplicate(account, instrument, trade):
    existing = BrokerageTransaction.objects.filter(account=account, external_id=trade.external_id).first()
    if existing is not None:
        return existing

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


@transaction.atomic
def import_xtb_transactions(user, account, uploaded_file):
    if account.user_id != user.id:
        raise BrokerageImportError('Wybrane konto maklerskie nie należy do zalogowanego użytkownika.')
    if account.broker != BrokerageAccount.BROKER_XTB:
        raise BrokerageImportError('Import XLSX jest przygotowany dla eksportu z XTB.')

    workbook_rows = _read_xtb_workbook(uploaded_file)
    export_currency = _read_account_currency(workbook_rows)
    result = BrokerageImportResult()
    if export_currency and export_currency != account.currency:
        result.warnings.append(
            f'Waluta w pliku XTB to {export_currency}, a wybrane konto ma walutę {account.currency}. '
            'Transakcje zapisano w walucie wybranego konta.'
        )

    trades, warnings = _xtb_trades(workbook_rows, account, account.name)
    result.warnings.extend(warnings)
    if not trades:
        raise BrokerageImportError('Nie znaleziono transakcji kupna ani sprzedaży w arkuszach XTB.')

    for trade in trades:
        instrument = _find_or_create_instrument(user, trade, account.currency)
        duplicate = _find_duplicate(account, instrument, trade)
        if duplicate is not None:
            if not duplicate.external_id:
                duplicate.external_id = trade.external_id
                duplicate.import_source = 'xtb'
                duplicate.save(update_fields=['external_id', 'import_source'])
            result.duplicates += 1
            continue

        aware_time = timezone.make_aware(trade.trade_time) if timezone.is_naive(trade.trade_time) else trade.trade_time
        BrokerageTransaction.objects.create(
            account=account,
            instrument=instrument,
            transaction_type=trade.transaction_type,
            trade_date=aware_time.date(),
            trade_time=aware_time.time().replace(microsecond=0),
            quantity=trade.quantity,
            price=trade.price,
            fees=trade.fees,
            import_source='xtb',
            external_id=trade.external_id,
            notes=f'Import XTB, pozycja {trade.position_id}',
        )
        result.created += 1

    return result
