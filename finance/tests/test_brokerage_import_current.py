from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from finance.brokerage_import import import_xtb_files, import_xtb_transactions
from finance.models import (
    BrokerageAccount,
    BrokerageCashOperation,
    BrokerageInstrument,
    BrokeragePositionSnapshot,
    BrokeragePriceSnapshot,
    BrokerageTransaction,
)


User = get_user_model()


def _column_name(index):
    name = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _worksheet_xml(rows):
    xml_rows = []
    for row_number, row in enumerate(rows, start=1):
        cells = []
        for column_number, value in enumerate(row, start=1):
            reference = f'{_column_name(column_number)}{row_number}'
            cells.append(
                f'<c r="{reference}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
            )
        xml_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(xml_rows)}</sheetData>'
        '</worksheet>'
    )


def _xlsx_with_sheets(sheets):
    buffer = BytesIO()
    with ZipFile(buffer, 'w', ZIP_DEFLATED) as archive:
        overrides = ''.join(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for index in range(1, len(sheets) + 1)
        )
        archive.writestr(
            '[Content_Types].xml',
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            f'{overrides}'
            '</Types>',
        )
        archive.writestr(
            '_rels/.rels',
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
            '</Relationships>',
        )
        workbook_sheets = ''.join(
            f'<sheet name="{escape(sheet_name)}" sheetId="{index}" r:id="rId{index}"/>'
            for index, (sheet_name, _rows) in enumerate(sheets, start=1)
        )
        archive.writestr(
            'xl/workbook.xml',
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets>{workbook_sheets}</sheets>'
            '</workbook>',
        )
        relationships = ''.join(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, len(sheets) + 1)
        )
        archive.writestr(
            'xl/_rels/workbook.xml.rels',
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'{relationships}'
            '</Relationships>',
        )
        for index, (_sheet_name, rows) in enumerate(sheets, start=1):
            archive.writestr(f'xl/worksheets/sheet{index}.xml', _worksheet_xml(rows))
    return buffer.getvalue()


def _current_xtb_upload(
    *,
    prefix='PLN',
    account_number='50966529',
    open_account_number=None,
    ticker='KRU.PL',
    cash_id_prefix='100',
):
    open_account_number = open_account_number or account_number
    cash_headers = [
        'Type',
        'Instrument',
        'Ticker',
        'Category',
        'Time',
        'Amount',
        'ID',
        'Comment',
        'Product',
        'Position ID',
    ]
    cash_rows = [
        ['Account number', account_number],
        cash_headers,
        ['Deposit', '', '', '', '2026-08-01 09:00:00', '1000.00', f'{cash_id_prefix}1', 'Bank deposit', '', ''],
        ['IKE deposit', '', '', '', '2026-08-02 09:00:00', '200.00', f'{cash_id_prefix}2', 'Internal IKE leg', 'IKE', ''],
        ['Transfer', '', '', '', '2026-08-03 09:00:00', '-50.00', f'{cash_id_prefix}3', 'Currency transfer', '', ''],
        ['Stock purchase', 'KRUK S.A.', ticker, 'STOCK', '2026-08-04 10:15:00', '-501.00', f'{cash_id_prefix}4', 'OPEN BUY 2 @ 250.50', 'Stocks', 'P-77'],
        ['Dividend', 'KRUK S.A.', ticker, 'STOCK', '2026-08-05 12:00:00', '10.00', f'{cash_id_prefix}5', 'Dividend payment', 'Stocks', 'P-77'],
        ['Withholding tax', 'KRUK S.A.', ticker, 'STOCK', '2026-08-05 12:00:01', '-1.90', f'{cash_id_prefix}6', 'Dividend tax', 'Stocks', 'P-77'],
    ]
    open_rows = [
        ['Account number', open_account_number],
        ['Data as of report generated', '2026-08-10 17:25:00'],
        ['Currency', '' if prefix == 'IKE' else prefix],
        [
            'Product',
            'Instrument/Position',
            'Ticker',
            'Category',
            'Type',
            'Volume',
            'Value',
            'Current price',
            'Net Profit',
            'Net Profit %',
        ],
        ['Stocks', 'KRUK S.A.', ticker, 'STOCK', '', '2', '540.00', '270.00', '39.00', '7.7844'],
        ['Stocks', 'KRUK S.A.', ticker, 'STOCK', 'BUY', '2', '540.00', '270.00', '39.00', '7.7844'],
    ]
    closed_rows = [['Account number', account_number]]
    content = _xlsx_with_sheets([
        ('Cash Operations', cash_rows),
        ('Open Positions', open_rows),
        ('Closed Positions', closed_rows),
    ])
    return SimpleUploadedFile(
        f'{prefix}_{account_number}_2006-01-01_2026-08-10.xlsx',
        content,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


class CurrentXtbImportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='xtb-importer', password='pass123')

    def test_current_export_creates_account_cash_ledger_trade_and_position_snapshot(self):
        result = import_xtb_transactions(self.user, None, _current_xtb_upload())

        account = BrokerageAccount.objects.get()
        self.assertEqual(account.external_account_id, '50966529')
        self.assertEqual(account.name, 'XTB PLN')
        self.assertEqual(account.currency, 'PLN')
        self.assertEqual(account.account_type, BrokerageAccount.STANDARD)
        self.assertIsNotNone(account.last_import_at)
        self.assertEqual(result.account, account)
        self.assertEqual(result.accounts_created, 1)
        self.assertEqual(result.files_processed, 1)

        self.assertEqual(result.cash_operations_created, 6)
        operations = {
            operation.external_id: operation
            for operation in BrokerageCashOperation.objects.order_by('external_id')
        }
        self.assertEqual(operations['1001'].operation_type, BrokerageCashOperation.DEPOSIT)
        self.assertEqual(operations['1001'].amount, 1000)
        self.assertEqual(operations['1002'].operation_type, BrokerageCashOperation.INTERNAL_TRANSFER)
        self.assertEqual(operations['1003'].operation_type, BrokerageCashOperation.INTERNAL_TRANSFER)
        self.assertEqual(operations['1004'].operation_type, BrokerageCashOperation.BUY)
        self.assertEqual(operations['1005'].operation_type, BrokerageCashOperation.DIVIDEND)
        self.assertEqual(operations['1006'].operation_type, BrokerageCashOperation.WITHHOLDING_TAX)

        instrument = BrokerageInstrument.objects.get()
        transaction = BrokerageTransaction.objects.get()
        self.assertEqual(instrument.ticker, 'KRU')
        self.assertEqual(instrument.price_symbol, 'KRU.PL')
        self.assertEqual(transaction.account, account)
        self.assertEqual(transaction.instrument, instrument)
        self.assertEqual(transaction.transaction_type, BrokerageTransaction.BUY)
        self.assertEqual(transaction.quantity, 2)
        self.assertEqual(transaction.price, 250.5)
        self.assertEqual(transaction.external_id, f'xtb:{account.id}:cash:1004')
        self.assertEqual(transaction.notes, 'OPEN BUY 2 @ 250.50')

        snapshot = BrokeragePositionSnapshot.objects.get()
        self.assertEqual(snapshot.quantity, 2)
        self.assertEqual(snapshot.market_value, 540)
        self.assertEqual(snapshot.current_price, 270)
        self.assertEqual(snapshot.profit, 39)
        self.assertEqual(snapshot.source, 'XTB')
        price_snapshot = BrokeragePriceSnapshot.objects.get()
        self.assertEqual(price_snapshot.instrument, instrument)
        self.assertEqual(price_snapshot.price, 270)
        instrument.refresh_from_db()
        self.assertEqual(instrument.last_price, 270)
        self.assertEqual(result.position_snapshots_created, 1)
        self.assertEqual(result.price_snapshots_created, 1)

    def test_reimport_is_idempotent_by_cash_id_and_snapshot_timestamp(self):
        first = import_xtb_transactions(self.user, None, _current_xtb_upload())
        second = import_xtb_transactions(self.user, None, _current_xtb_upload())

        self.assertEqual(first.created, 1)
        self.assertEqual(second.created, 0)
        self.assertEqual(second.duplicates, 1)
        self.assertEqual(second.cash_operations_created, 0)
        self.assertEqual(second.cash_operations_duplicates, 6)
        self.assertEqual(second.position_snapshots_duplicates, 1)
        self.assertEqual(second.price_snapshots_duplicates, 1)
        self.assertEqual(second.accounts_created, 0)
        self.assertEqual(BrokerageAccount.objects.count(), 1)
        self.assertEqual(BrokerageCashOperation.objects.count(), 6)
        self.assertEqual(BrokerageTransaction.objects.count(), 1)
        self.assertEqual(BrokeragePositionSnapshot.objects.count(), 1)
        self.assertEqual(BrokeragePriceSnapshot.objects.count(), 1)

    def test_multiple_files_create_separate_currency_accounts(self):
        result = import_xtb_files(
            self.user,
            [
                _current_xtb_upload(),
                _current_xtb_upload(
                    prefix='USD',
                    account_number='53145618',
                    ticker='AAPL.US',
                    cash_id_prefix='200',
                ),
            ],
        )

        self.assertEqual(result.files_processed, 2)
        self.assertEqual(result.accounts_created, 2)
        self.assertEqual(len(result.accounts), 2)
        self.assertIsNone(result.account)
        self.assertEqual(
            set(BrokerageAccount.objects.values_list('external_account_id', 'currency')),
            {('50966529', 'PLN'), ('53145618', 'USD')},
        )
        self.assertEqual(BrokerageCashOperation.objects.count(), 12)
        self.assertEqual(BrokerageTransaction.objects.count(), 2)
        self.assertEqual(BrokeragePositionSnapshot.objects.count(), 2)

    def test_ike_open_positions_account_conflict_is_warned_and_not_used_for_binding(self):
        result = import_xtb_transactions(
            self.user,
            None,
            _current_xtb_upload(
                prefix='IKE',
                account_number='51204763',
                open_account_number='50966529',
            ),
        )

        account = BrokerageAccount.objects.get()
        self.assertEqual(account.external_account_id, '51204763')
        self.assertEqual(account.currency, 'PLN')
        self.assertEqual(account.account_type, BrokerageAccount.IKE)
        self.assertTrue(
            any(
                'Eksport IKE XTB' in warning
                and '50966529' in warning
                and '51204763' in warning
                for warning in result.warnings
            )
        )
        self.assertEqual(
            BrokerageCashOperation.objects.get(external_id='1002').operation_type,
            BrokerageCashOperation.INTERNAL_TRANSFER,
        )
