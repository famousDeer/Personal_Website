"""Podsumowania pasków wypłaty: karta miesiąca i rok z drugim progiem podatkowym."""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Prefetch

from .models import Payslip, PayslipItem

TAX_THRESHOLD = Decimal('120000')
TAX_REDUCING_AMOUNT = Decimal('3600')
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
        'gross', 'net', 'tax', 'zus', 'health', 'ppk_employee', 'ppk_employer', 'hours', 'overtime',
    )}
    tax_payslips = tax_year_payslips(user, year)
    cumulative_by_period = {}
    tax_base = Decimal('0')
    for payslip in tax_payslips:
        tax_base += payslip.tax_base_estimate
        cumulative_by_period[payslip.period] = tax_base
    totals['tax_base'] = tax_base
    crossed_in = next((p for p in tax_payslips if p.income_32 > 0), None)
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
            # Grudzień jest wypłacany w styczniu, więc liczy się do następnego roku podatkowego.
            row.update(
                hours=base_hours + overtime, overtime=overtime,
                cumulative_tax_base=cumulative_by_period.get(payslip.period),
            )
        rows.append(row)

    missing = [row['month'] for row in rows if not row['payslip']]
    income_32 = sum((p.income_32 for p in tax_payslips), Decimal('0'))
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
        'previous_december': next((p for p in tax_payslips if p.period.year == year - 1), None),
        'settlement': build_settlement(user, year, tax_payslips),
    }


def tax_year_payslips(user, year):
    """Paski wypłacone w roku podatkowym ``year``: za grudzień poprzedniego roku – listopad
    (wypłata 10. dnia następnego miesiąca, PIT liczy się według daty wypłaty)."""
    return list(
        Payslip.objects.filter(user=user, period__gte=date(year - 1, 12, 1), period__lte=date(year, 11, 1))
        .order_by('period')
    )


def pit_scale(income):
    """Roczny PIT ze skali: 12% minus kwota zmniejszająca 3600 zł, 32% ponad 120 000 zł."""
    income = max(Decimal('0'), Decimal(income).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if income <= TAX_THRESHOLD:
        tax = income * Decimal('0.12') - TAX_REDUCING_AMOUNT
    else:
        tax = TAX_THRESHOLD * Decimal('0.12') - TAX_REDUCING_AMOUNT + (income - TAX_THRESHOLD) * Decimal('0.32')
    return max(Decimal('0'), tax.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def next_month(day):
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)


def project_tax_year(year, tax_payslips):
    """Dochód i zaliczki za cały rok podatkowy.

    Miesiące bez paska (luki i miesiące do listopada) dostają średni dochód
    z wgranych pasków, a ich zaliczkę liczymy tak jak płatnik: 12% albo 32%
    po przekroczeniu progu, minus 300 zł ulgi. Miesiące przed pierwszym
    paskiem to brak pracy u tego pracodawcy.
    """
    if not tax_payslips:
        return None
    known = {p.period: p for p in tax_payslips}
    first, last = tax_payslips[0].period, date(year, 11, 1)
    months = []
    month = first
    while month <= last:
        months.append(month)
        month = next_month(month)
    fill = {m: None for m in months if m not in known}
    average = sum((p.tax_base_estimate for p in tax_payslips), Decimal('0')) / len(tax_payslips)

    # Pasek, w którym pierwszy raz pojawia się „Dochód 32%”, zdradza dochód sprzed niego:
    # 120 000 − (dochód tego miesiąca − część ponad próg). Luki przed nim dzielą tę resztę.
    crossing = next((p for p in tax_payslips if p.income_32 > 0), None)
    if crossing:
        before = [m for m in fill if m < crossing.period]
        known_before = sum((p.tax_base_estimate for p in tax_payslips if p.period < crossing.period), Decimal('0'))
        rest = TAX_THRESHOLD - (crossing.tax_base_estimate - crossing.income_32) - known_before
        if before and rest > 0:
            for m in before:
                fill[m] = rest / len(before)
            filled = [p.tax_base_estimate for p in tax_payslips if p.period <= crossing.period] + [fill[m] for m in before]
            average = sum(filled, Decimal('0')) / len(filled)

    relief = tax_payslips[-1].tax_relief
    income = advances = Decimal('0')
    projected = []
    for month in months:
        payslip = known.get(month)
        if payslip:
            base, advance = payslip.tax_base_estimate, payslip.tax_advance
        else:
            base = fill[month] if fill[month] is not None else average
            below = max(Decimal('0'), min(base, TAX_THRESHOLD - income))
            advance = max(Decimal('0'), (below * Decimal('0.12') + (base - below) * Decimal('0.32') - relief)
                          .quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            projected.append(month)
        income += base
        advances += advance
    return {
        'income': income.quantize(Decimal('0.01')), 'advances': advances, 'projected': projected,
        'average': average, 'calibrated': bool(crossing and any(m < crossing.period for m in projected)),
    }


def build_settlement(user, year, tax_payslips):
    projection = project_tax_year(year, tax_payslips)
    if projection is None:
        return None
    from .models import PayslipTaxYear

    partner = PayslipTaxYear.objects.filter(user=user, year=year).first()
    own_tax = pit_scale(projection['income'])
    settlement = {
        **projection,
        'own_tax': own_tax,
        'own_refund': projection['advances'] - own_tax,
        'partner': partner,
    }
    if partner and partner.partner_income is not None:
        partner_tax = pit_scale(partner.partner_income)
        partner_paid = partner.partner_tax_paid if partner.partner_tax_paid is not None else partner_tax
        joint_tax = 2 * pit_scale((projection['income'] + partner.partner_income) / 2)
        settlement.update(
            partner_tax=partner_tax,
            separate_tax=own_tax + partner_tax,
            partner_paid=partner_paid,
            joint_tax=joint_tax,
            joint_paid=projection['advances'] + partner_paid,
            joint_refund=projection['advances'] + partner_paid - joint_tax,
            joint_benefit=own_tax + partner_tax - joint_tax,
        )
    return settlement
