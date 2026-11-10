"""Podsumowania pasków wypłaty: karta miesiąca i rok z drugim progiem podatkowym."""
from datetime import date
from decimal import Decimal

from django.db.models import Prefetch

from .models import Payslip, PayslipItem

TAX_THRESHOLD = Decimal('120000')
BASE_PAY_PREFIX = 'wynagrodzenie zasadnicze'
# Nadgodziny bywają na pasku jako „Rozliczenie odchyłek” (płatne godziny ponad normę).
OVERTIME_NAMES = ('wynagrodzenie za nadgodziny', 'rozliczenie odchyłek')


def payslip_hours(payslip):
    """(godziny w normie, nadgodziny) z pozycji paska."""
    base = overtime = Decimal('0')
    for item in payslip.items.all():
        name = item.name.lower()
        if name.startswith(BASE_PAY_PREFIX):
            base += item.hours_decimal
        elif name.startswith(OVERTIME_NAMES):
            overtime += item.hours_decimal
    return base, overtime


def percent(part, whole):
    return (part / whole * 100) if whole else Decimal('0')


def build_detail(payslip):
    gross = payslip.gross
    steps = [
        ('Potrącenia', payslip.total_deductions, 'bi-scissors'),
        ('ZUS (emerytalne, rentowe, chorobowe)', payslip.zus_employee, 'bi-shield-check'),
        ('Składka zdrowotna', payslip.health, 'bi-heart-pulse'),
        ('Zaliczka na PIT', payslip.tax_advance, 'bi-bank'),
        ('PPK (Twoja wpłata)', payslip.ppk_employee, 'bi-piggy-bank'),
    ]
    waterfall = [
        {'label': label, 'amount': amount, 'icon': icon, 'percent': percent(amount, gross)}
        for label, amount, icon in steps if amount
    ]
    base_hours, overtime_hours = payslip_hours(payslip)
    worked = base_hours + overtime_hours
    items = list(payslip.items.all())
    previous = (
        Payslip.objects.filter(user=payslip.user, period__lt=payslip.period)
        .prefetch_related('items').order_by('-period').first()
    )
    comparison = None
    if previous:
        _, previous_overtime = payslip_hours(previous)
        comparison = {
            'period': previous.period,
            'gross_delta': {'amount': gross - previous.gross, 'percent': None},
            'net_delta': {'amount': payslip.net - previous.net, 'percent': None},
            'overtime': overtime_hours - previous_overtime,
        }
    following = (
        Payslip.objects.filter(user=payslip.user, period__gt=payslip.period).order_by('period').first()
    )
    period = payslip.period
    return {
        'payslip': payslip,
        'paid_on': date(period.year + period.month // 12, period.month % 12 + 1, 10),
        'net_share': percent(payslip.net, gross),
        'waterfall': waterfall,
        'base_hours': base_hours,
        'overtime_hours': overtime_hours,
        'worked_hours': worked,
        'net_per_hour': (payslip.net / worked) if worked else None,
        'gross_per_hour': (gross / worked) if worked else None,
        'additions': [item for item in items if item.addition],
        'deductions': [item for item in items if item.deduction],
        'non_cash': [item for item in items if item.non_cash],
        'comparison': comparison,
        'previous': previous,
        'following': following,
    }


def last_month_of(year, payslips, today=None):
    """Do którego miesiąca pokazywać wiersze: pasek za miesiąc przychodzi w następnym."""
    today = today or date.today()
    if year < today.year:
        return 12
    latest = max((p.period.month for p in payslips), default=0)
    return max(latest, today.month - 1 if year == today.year else 0)


def build_year(user, year, today=None):
    payslips = list(
        Payslip.objects.filter(user=user, period__year=year)
        .prefetch_related(Prefetch('items', queryset=PayslipItem.objects.order_by('position')))
        .order_by('period')
    )
    by_month = {p.period.month: p for p in payslips}
    rows = []
    totals = {key: Decimal('0') for key in (
        'gross', 'net', 'tax', 'zus', 'health', 'ppk_employee', 'ppk_employer', 'hours', 'overtime', 'tax_base',
    )}
    crossed_in = None
    for month in range(1, last_month_of(year, payslips, today) + 1):
        payslip = by_month.get(month)
        row = {'month': date(year, month, 1), 'payslip': payslip}
        if payslip:
            base_hours, overtime = payslip_hours(payslip)
            totals['gross'] += payslip.gross
            totals['net'] += payslip.net
            totals['tax'] += payslip.tax_advance
            totals['zus'] += payslip.zus_employee
            totals['health'] += payslip.health
            totals['ppk_employee'] += payslip.ppk_employee
            totals['ppk_employer'] += payslip.ppk_employer
            totals['hours'] += base_hours + overtime
            totals['overtime'] += overtime
            totals['tax_base'] += payslip.tax_base_estimate
            row.update(hours=base_hours + overtime, overtime=overtime, cumulative_tax_base=totals['tax_base'])
            if crossed_in is None and payslip.income_32 > 0:
                crossed_in = payslip
        rows.append(row)

    missing = [row['month'] for row in rows if not row['payslip']]
    income_32 = sum((p.income_32 for p in payslips), Decimal('0'))
    if crossed_in:
        remaining = Decimal('0')
        progress = Decimal('100')
    else:
        remaining = max(Decimal('0'), TAX_THRESHOLD - totals['tax_base'])
        progress = min(Decimal('100'), percent(totals['tax_base'], TAX_THRESHOLD))
    years = sorted(
        {d.year for d in Payslip.objects.filter(user=user).dates('period', 'year')} | {year},
        reverse=True,
    )
    return {
        'year': year,
        'years': years,
        'rows': rows,
        'count': len(payslips),
        'missing': missing,
        'totals': totals,
        'ppk_total': totals['ppk_employee'] + totals['ppk_employer'],
        # Średnia z wgranych pasków – brakujące miesiące jej nie zaniżają.
        'avg_gross': totals['gross'] / len(payslips) if payslips else None,
        'avg_net': totals['net'] / len(payslips) if payslips else None,
        'net_share': percent(totals['net'], totals['gross']),
        'threshold': TAX_THRESHOLD,
        'threshold_remaining': remaining,
        'threshold_progress': progress,
        'threshold_crossed_in': crossed_in,
        'income_32': income_32,
    }
