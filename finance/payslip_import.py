"""Import pasków wypłaty z PDF (enova365 / Soneta).

Tekst z PDF-a czyta ``pdftotext -layout`` (poppler-utils, jak e-paragony
w kuchni). ``parse_payslip_text`` to czysta funkcja na tym tekście – testy
podają jej próbki bez prawdziwych PDF-ów. PESEL i dane pracownika nie są
zapisywane, sam plik też nie.
"""
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.db import transaction

AMOUNT_RE = re.compile(r'-?(?:\d{1,3}(?: \d{3})+|\d+),\d{2}')
PERIOD_RE = re.compile(r'Za okres:\s*\d{1,2}\.\.\.\d{1,2}\.(\d{1,2})\.(\d{4})')
ITEM_RE = re.compile(r'^\s*(\d{1,3})(\D.*?)(?=\s{2,}|$)')
HOURS_RE = re.compile(r'(?<![\d:])(\d{1,3}:\d{2})(?![\d:])')
DAYS_RE = re.compile(r'(?<![\d:,])(\d{1,3})(?![\d:,])')


class PayslipImportError(Exception):
    pass


@dataclass
class ParsedItem:
    position: int
    name: str
    hours: str = ''
    days: int | None = None
    addition: Decimal | None = None
    deduction: Decimal | None = None
    non_cash: Decimal | None = None


@dataclass
class ParsedPayslip:
    period: date
    employer: str = ''
    document_number: str = ''
    zus_base: Decimal = Decimal('0')
    zus_pension: Decimal = Decimal('0')
    zus_disability: Decimal = Decimal('0')
    zus_sickness: Decimal = Decimal('0')
    employer_zus: Decimal = Decimal('0')
    employer_fp: Decimal = Decimal('0')
    employer_fgsp: Decimal = Decimal('0')
    health_base: Decimal = Decimal('0')
    health: Decimal = Decimal('0')
    tax_advance: Decimal = Decimal('0')
    tax_costs: Decimal = Decimal('0')
    tax_relief: Decimal = Decimal('0')
    income_32: Decimal = Decimal('0')
    ppk_employee: Decimal = Decimal('0')
    ppk_employer: Decimal = Decimal('0')
    total_additions: Decimal = Decimal('0')
    total_deductions: Decimal = Decimal('0')
    total_non_cash: Decimal = Decimal('0')
    cash: Decimal = Decimal('0')
    transfer: Decimal = Decimal('0')
    items: list[ParsedItem] = field(default_factory=list)

    @property
    def zus_employee(self):
        return self.zus_pension + self.zus_disability + self.zus_sickness

    @property
    def payout_difference(self):
        """Różnica między wypłatą z paska a wyliczoną ze składników (powinna być 0)."""
        expected = (
            self.total_additions - self.total_deductions - self.zus_employee
            - self.health - self.tax_advance - self.ppk_employee
        )
        return (self.cash + self.transfer) - expected


def to_decimal(text):
    return Decimal(text.replace(' ', '').replace(',', '.'))


def amounts(line):
    return [to_decimal(match.group()) for match in AMOUNT_RE.finditer(line)]


def labeled(text, label):
    match = re.search(re.escape(label) + r'\s*(' + AMOUNT_RE.pattern + ')', text)
    return to_decimal(match.group(1)) if match else Decimal('0')


def row(lines, label, start=0):
    """Kwoty z pierwszego wiersza zaczynającego się od ``label`` (od indeksu ``start``)."""
    for index in range(start, len(lines)):
        if lines[index].strip().startswith(label):
            return index, amounts(lines[index].split(label, 1)[1])
    return None, []


def pick(values, index):
    return values[index] if len(values) > index else Decimal('0')


def parse_items(lines):
    header_index = next(
        (i for i, line in enumerate(lines) if line.strip().startswith('Lp.') and 'Dodatek' in line),
        None,
    )
    if header_index is None:
        return []
    header = lines[header_index]
    # Kwoty są wyrównane do prawej pod nagłówkami kolumn – kolumnę wybiera
    # najbliższy koniec nagłówka.
    columns = {
        'addition': header.index('Dodatek') + len('Dodatek'),
        'deduction': header.index('Potrącenie') + len('Potrącenie'),
        'non_cash': header.index('niepieniężne') + len('niepieniężne') if 'niepieniężne' in header else 10 ** 6,
    }
    items = []
    for line in lines[header_index + 1:]:
        if 'Suma:' in line:
            break
        match = ITEM_RE.match(line)
        if not match:
            continue
        item = ParsedItem(position=int(match.group(1)), name=match.group(2).strip())
        rest_start = match.end()
        rest = line[rest_start:]
        for amount in AMOUNT_RE.finditer(rest):
            end = rest_start + amount.end()
            column = min(columns, key=lambda key: abs(columns[key] - end))
            setattr(item, column, to_decimal(amount.group()))
        without_amounts = AMOUNT_RE.sub(lambda m: ' ' * len(m.group()), rest)
        hours = HOURS_RE.search(without_amounts)
        if hours:
            item.hours = hours.group(1)
            without_amounts = without_amounts.replace(hours.group(1), ' ' * len(hours.group(1)), 1)
        days = DAYS_RE.search(without_amounts)
        if days:
            item.days = int(days.group(1))
        items.append(item)
    return items


def parse_payslip_text(text):
    period = PERIOD_RE.search(text)
    if not period or 'Ubezpieczony:' not in text or 'Suma:' not in text:
        raise PayslipImportError('To nie wygląda na pasek wypłaty z enova365 (brak okresu, składek albo sumy).')
    lines = text.splitlines()
    parsed = ParsedPayslip(period=date(int(period.group(2)), int(period.group(1)), 1))

    employer = re.search(r'Nazwa firmy:\s*(.+?)\s*$', text, re.MULTILINE)
    parsed.employer = employer.group(1) if employer else ''
    document = re.search(r'\b([A-Z]{2,}/[A-Z]{2,}/\d{4}/\d+/\d+/\d+)\b', text)
    parsed.document_number = document.group(1) if document else ''

    base_index, base = row(lines, 'Podstawa:')
    parsed.zus_base = pick(base, 0)
    parsed.health_base = pick(base, 4)

    _, insured = row(lines, 'Ubezpieczony:')
    parsed.zus_pension, parsed.zus_disability, parsed.zus_sickness = pick(insured, 0), pick(insured, 1), pick(insured, 2)
    _, payer = row(lines, 'Płatnik:')
    parsed.employer_zus = pick(payer, 4)

    contribution_index, contribution = row(lines, 'Skladka:')
    if contribution_index is None:
        contribution_index, contribution = row(lines, 'Składka:')
    parsed.employer_fp, parsed.employer_fgsp = pick(contribution, 0), pick(contribution, 1)
    # PPK: wiersz „U” (pracownik) jest w wierszu składek, „F” (firma) pod nim.
    parsed.ppk_employee = pick(contribution, 3) + pick(contribution, 4)
    if contribution_index is not None:
        for line in lines[contribution_index + 1:contribution_index + 3]:
            if re.match(r'^\s*F\s', line):
                employer_ppk = amounts(line)
                parsed.ppk_employer = pick(employer_ppk, 0) + pick(employer_ppk, 1)
                break

    parsed.health = labeled(text, 'Zdrow. prac.:')
    parsed.tax_advance = labeled(text, 'Zal. podatku:')
    parsed.tax_costs = labeled(text, 'Koszty uz.:')
    parsed.tax_relief = labeled(text, 'Ulga podatkowa:')
    parsed.income_32 = labeled(text, 'Dochód 32%:')
    parsed.cash = labeled(text, 'Gotówka:')
    parsed.transfer = labeled(text, 'ROR:')

    sum_line = next(line for line in lines if 'Suma:' in line)
    totals = amounts(sum_line.split('Suma:', 1)[1])
    parsed.total_additions, parsed.total_deductions, parsed.total_non_cash = (
        pick(totals, 0), pick(totals, 1), pick(totals, 2),
    )
    parsed.items = parse_items(lines)
    if not parsed.total_additions or not (parsed.transfer or parsed.cash):
        raise PayslipImportError('Nie udało się odczytać sumy dodatków albo kwoty wypłaty.')
    return parsed


def extract_pdf_text(uploaded_file):
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / 'pasek.pdf'
        with source.open('wb') as handle:
            for chunk in uploaded_file.chunks():
                handle.write(chunk)
        try:
            completed = subprocess.run(
                ['pdftotext', '-layout', '-l', '2', str(source), '-'],
                capture_output=True, timeout=30, check=False,
            )
        except FileNotFoundError as exc:
            raise PayslipImportError('Brak programu pdftotext na serwerze (poppler-utils).') from exc
        except subprocess.TimeoutExpired as exc:
            raise PayslipImportError('Odczyt PDF-a trwał za długo.') from exc
    stderr = completed.stderr.decode('utf-8', 'replace')
    if completed.returncode != 0:
        if 'password' in stderr.lower():
            raise PayslipImportError('PDF jest zabezpieczony hasłem – otwórz go, wpisz hasło i wyeksportuj kopię bez hasła.')
        raise PayslipImportError('Nie udało się otworzyć PDF-a.')
    return completed.stdout.decode('utf-8', 'replace')


@transaction.atomic
def save_payslip(user, parsed):
    """Zapisuje pasek; ponowny import tego samego miesiąca nadpisuje dane. Zwraca (pasek, utworzony)."""
    from .models import Payslip, PayslipItem

    values = {
        name: getattr(parsed, name)
        for name in (
            'employer', 'document_number', 'zus_base', 'zus_pension', 'zus_disability', 'zus_sickness',
            'employer_zus', 'employer_fp', 'employer_fgsp', 'health_base', 'health', 'tax_advance',
            'tax_costs', 'tax_relief', 'income_32', 'ppk_employee', 'ppk_employer', 'total_additions',
            'total_deductions', 'total_non_cash', 'cash', 'transfer',
        )
    }
    payslip, created = Payslip.objects.update_or_create(user=user, period=parsed.period, defaults=values)
    payslip.items.all().delete()
    PayslipItem.objects.bulk_create([
        PayslipItem(
            payslip=payslip, position=item.position, name=item.name[:200], hours=item.hours,
            days=item.days, addition=item.addition, deduction=item.deduction, non_cash=item.non_cash,
        )
        for item in parsed.items
    ])
    return payslip, created
