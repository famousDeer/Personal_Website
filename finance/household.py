"""Analiza domowa: wspólne wydatki i przychody kilku osób, minimum na życie.

Zakres (``household_scope``):
- moje konto osobiste,
- konta wspólne, do których należę,
- konta osobiste pozostałych członków tych kont wspólnych - tylko gdy ich
  właściciel sam włączył ``share_in_household_analysis`` (zgoda). Bez
  wspólnego konta nikt nie widzi cudzych danych, nawet ze zgodą.

Z cudzych kont pokazujemy wyłącznie sumy (kategoria × miesiąc), nigdy
tytułów, sklepów ani pojedynczych pozycji.

Przelewy wewnątrz domu nie liczą się podwójnie: wydatek „Wpłata do
wspólnego…” na koncie osobistym i powiązany z nim przychód na koncie
wspólnym są pomijane, a wydatki z konta wspólnego liczą się normalnie.
Kategorie rodzaju „Pomijane” (np. przelew do współmałżonka) też.

Minimum na życie = średnia miesięczna wydatków z kategorii „Niezbędne”
w ostatnich pełnych miesiącach (bez bieżącego, który jeszcze trwa).
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_type
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from .account_utils import TRANSFER_TO_SHARED_CATEGORY, ensure_personal_finance_account
from .models import CategoryKind, Daily, FinanceAccount, Income

PERIOD_CHOICES = (3, 6, 12)
DEFAULT_PERIOD = 6
EMERGENCY_MONTHS = (3, 6)

ESSENTIAL, LIFESTYLE, SAVINGS, EXCLUDED = (
    CategoryKind.ESSENTIAL, CategoryKind.LIFESTYLE, CategoryKind.SAVINGS, CategoryKind.EXCLUDED,
)
KIND_ORDER = (ESSENTIAL, LIFESTYLE, SAVINGS, EXCLUDED)
KIND_LABELS = dict(CategoryKind.KIND_CHOICES)
KIND_SHORT = {ESSENTIAL: 'Niezbędne', LIFESTYLE: 'Pozostałe', SAVINGS: 'Oszczędności', EXCLUDED: 'Pomijane'}
KIND_HINTS = {
    ESSENTIAL: 'Bez tego nie da się żyć: jedzenie, rachunki, raty, dojazdy, zdrowie.',
    LIFESTYLE: 'Można ograniczyć: restauracje, rozrywka, ubrania, prezenty.',
    SAVINGS: 'Odkładane pieniądze – nie są kosztem życia.',
    EXCLUDED: 'Nie liczą się wcale, np. przelew między domownikami albo koszty zwracane przez pracodawcę.',
}

# Rodzaj domyślny dla kategorii wbudowanych (finance.views.CATEGORIES_EXPENSES).
# Domownicy zmieniają go na stronie analizy (CategoryKind).
DEFAULT_KINDS = {
    'Zakupy spozywcze': ESSENTIAL,
    'Rachunki': ESSENTIAL,
    'Zdrowie': ESSENTIAL,
    'Transport miejski': ESSENTIAL,
    'Paliwo': ESSENTIAL,
    'Rata kredytu konsumenckiego': ESSENTIAL,
    'Spłata karty kredytowej': ESSENTIAL,
    'Drogeria': ESSENTIAL,
    'Serwis auta': ESSENTIAL,
    'Części do auta': ESSENTIAL,
    'Inwestycje': SAVINGS,
    'Oszczędności': SAVINGS,
    'Delegacje': EXCLUDED,
}

HUNDRED = Decimal('100')
CENT = Decimal('0.01')
ZERO = Decimal('0')


def _money(value):
    return (value or ZERO).quantize(CENT, rounding=ROUND_HALF_UP)


def _add_months(day, months):
    index = day.month - 1 + months
    return date_type(day.year + index // 12, index % 12 + 1, 1)


def category_kinds(categories):
    """{kategoria: rodzaj} - zapisany przez domowników albo domyślny."""
    stored = dict(CategoryKind.objects.filter(category__in=list(categories)).values_list('category', 'kind'))
    kinds = {}
    for category in categories:
        if category == TRANSFER_TO_SHARED_CATEGORY:
            kinds[category] = EXCLUDED
        else:
            kinds[category] = stored.get(category) or DEFAULT_KINDS.get(category, LIFESTYLE)
    return kinds


def save_category_kinds(changes, user):
    """``changes``: {kategoria: rodzaj}. Zwraca liczbę zmienionych kategorii."""
    current = category_kinds(changes)
    changed = 0
    for category, kind in changes.items():
        if kind not in KIND_LABELS or category == TRANSFER_TO_SHARED_CATEGORY or current.get(category) == kind:
            continue
        CategoryKind.objects.update_or_create(category=category, defaults={'kind': kind, 'updated_by': user})
        changed += 1
    return changed


# ---------------------------------------------------------------------------
# Kto jest w analizie
# ---------------------------------------------------------------------------

def person_label(user):
    return (user.first_name or user.username).strip() or user.username


@dataclass
class ScopeEntry:
    account: FinanceAccount
    label: str
    role: str            # 'me' | 'shared' | 'partner'
    included: bool
    user: object = None


@dataclass
class HouseholdScope:
    entries: list = field(default_factory=list)

    @property
    def included(self):
        return [entry for entry in self.entries if entry.included]

    @property
    def missing_partners(self):
        return [entry for entry in self.entries if entry.role == 'partner' and not entry.included]

    @property
    def has_partner(self):
        return any(entry.role == 'partner' for entry in self.entries)

    @property
    def shares_partner_data(self):
        return any(entry.role == 'partner' and entry.included for entry in self.entries)


def household_scope(user):
    personal = ensure_personal_finance_account(user)
    scope = HouseholdScope([ScopeEntry(personal, f'Ty ({person_label(user)})', 'me', True, user)])
    shared_accounts = list(
        FinanceAccount.objects.filter(account_type=FinanceAccount.SHARED, members=user)
        .prefetch_related('members').order_by('name')
    )
    partners = {}
    for account in shared_accounts:
        scope.entries.append(ScopeEntry(account, account.name, 'shared', True))
        for member in account.members.all():
            if member.pk != user.pk:
                partners[member.pk] = member
    for account in (
        FinanceAccount.objects.filter(account_type=FinanceAccount.PERSONAL, owner_id__in=partners)
        .select_related('owner').order_by('owner__username')
    ):
        scope.entries.append(ScopeEntry(
            account, person_label(account.owner), 'partner', account.share_in_household_analysis, account.owner,
        ))
    return scope


# ---------------------------------------------------------------------------
# Obliczenia
# ---------------------------------------------------------------------------

def analysis_period(months, today=None):
    """Ostatnie ``months`` pełnych miesięcy: (pierwszy dzień, pierwszy dzień po okresie)."""
    today = today or timezone.localdate()
    end = today.replace(day=1)
    return _add_months(end, -months), end


def _monthly_rows(account_ids, start, end):
    """Sumy (konto × kategoria/źródło × miesiąc) bez przelewów wewnątrz domu."""
    expense_rows = list(
        Daily.objects.filter(account_id__in=account_ids, date__gte=start, date__lt=end)
        .exclude(category=TRANSFER_TO_SHARED_CATEGORY)
        .annotate(month_start=TruncMonth('date'))
        .values('account_id', 'category', 'month_start')
        .annotate(total=Sum('cost'))
    )
    income_rows = list(
        Income.objects.filter(account_id__in=account_ids, date__gte=start, date__lt=end, linked_expense__isnull=True)
        .annotate(month_start=TruncMonth('date'))
        .values('account_id', 'source', 'month_start')
        .annotate(total=Sum('amount'))
    )
    return expense_rows, income_rows


def build_household_analysis(user, months=DEFAULT_PERIOD, today=None):
    if months not in PERIOD_CHOICES:
        months = DEFAULT_PERIOD
    scope = household_scope(user)
    start, end = analysis_period(months, today)
    month_starts = [_add_months(start, offset) for offset in range(months)]
    entries = {entry.account.pk: entry for entry in scope.included}
    account_ids = list(entries)

    expense_rows, income_rows = _monthly_rows(account_ids, start, end)
    kinds = category_kinds({row['category'] for row in expense_rows})

    def month_key(value):
        return value.date() if hasattr(value, 'date') else value

    by_kind_month = defaultdict(lambda: defaultdict(lambda: ZERO))
    by_category = defaultdict(lambda: ZERO)
    by_category_person = defaultdict(lambda: defaultdict(lambda: ZERO))
    by_person_kind = defaultdict(lambda: defaultdict(lambda: ZERO))
    active_months = set()
    for row in expense_rows:
        kind = kinds[row['category']]
        month = month_key(row['month_start'])
        label = entries[row['account_id']].label
        active_months.add(month)
        by_kind_month[kind][month] += row['total']
        by_category[row['category']] += row['total']
        by_category_person[row['category']][label] += row['total']
        by_person_kind[label][kind] += row['total']

    income_by_month = defaultdict(lambda: ZERO)
    income_by_source = defaultdict(lambda: ZERO)
    income_by_person = defaultdict(lambda: ZERO)
    for row in income_rows:
        month = month_key(row['month_start'])
        active_months.add(month)
        income_by_month[month] += row['total']
        income_by_source[row['source'] or 'Inne'] += row['total']
        income_by_person[entries[row['account_id']].label] += row['total']

    # Średnia z miesięcy, w których cokolwiek wpisano - świeże konto nie
    # zaniża minimum zerami z miesięcy sprzed pierwszego wpisu.
    divisor = Decimal(max(len(active_months), 1))

    def avg(value):
        return _money(value / divisor)

    kind_totals = {kind: sum(by_kind_month[kind].values(), ZERO) for kind in KIND_ORDER}
    essential_months = [by_kind_month[ESSENTIAL][month] for month in month_starts if month in active_months]
    spending_total = kind_totals[ESSENTIAL] + kind_totals[LIFESTYLE]
    income_total = sum(income_by_month.values(), ZERO)
    minimum = avg(kind_totals[ESSENTIAL])

    people = [entry.label for entry in scope.included]
    groups = []
    for kind in KIND_ORDER:
        names = sorted((name for name in by_category if kinds[name] == kind), key=lambda name: -by_category[name])
        if not names:
            continue
        largest = by_category[names[0]] or Decimal('1')
        base = kind_totals[kind] or Decimal('1')
        groups.append({
            'kind': kind,
            'label': KIND_LABELS[kind],
            'hint': KIND_HINTS[kind],
            'avg': avg(kind_totals[kind]),
            'rows': [{
                'name': name,
                'kind': kind,
                'avg': avg(by_category[name]),
                'share': (by_category[name] / base * HUNDRED).quantize(Decimal('0.1')),
                'bar': (by_category[name] / largest * HUNDRED).quantize(Decimal('0.1')),
                'people': [{'label': person, 'avg': avg(by_category_person[name].get(person, ZERO))} for person in people],
            } for name in names],
        })

    monthly = []
    for month in month_starts:
        essential = by_kind_month[ESSENTIAL].get(month, ZERO)
        lifestyle = by_kind_month[LIFESTYLE].get(month, ZERO)
        savings = by_kind_month[SAVINGS].get(month, ZERO)
        income = income_by_month.get(month, ZERO)
        monthly.append({
            'month': month,
            'essential': _money(essential),
            'lifestyle': _money(lifestyle),
            'savings': _money(savings),
            'income': _money(income),
            'balance': _money(income - essential - lifestyle - savings),
            'has_data': month in active_months,
        })

    savings_rate = None
    if income_total > 0:
        savings_rate = ((income_total - spending_total) / income_total * HUNDRED).quantize(Decimal('0.1'))

    return {
        'scope': scope,
        'months': months,
        'period_choices': PERIOD_CHOICES,
        'start': start,
        'end': end,
        'last_month': _add_months(end, -1),
        'active_months': len(active_months),
        'has_data': bool(active_months),
        'minimum': minimum,
        'minimum_low': _money(min(essential_months)) if essential_months else ZERO,
        'minimum_high': _money(max(essential_months)) if essential_months else ZERO,
        'spending': avg(spending_total),
        'lifestyle': avg(kind_totals[LIFESTYLE]),
        'savings': avg(kind_totals[SAVINGS]),
        'excluded': avg(kind_totals[EXCLUDED]),
        'income': avg(income_total),
        'savings_rate': savings_rate,
        'essential_share': (
            (kind_totals[ESSENTIAL] / spending_total * HUNDRED).quantize(Decimal('0.1')) if spending_total else None
        ),
        'reserves': [{'months': count, 'amount': _money(minimum * count)} for count in EMERGENCY_MONTHS],
        'groups': groups,
        'people': people,
        'people_totals': [{
            'label': person,
            'spending': avg(by_person_kind[person][ESSENTIAL] + by_person_kind[person][LIFESTYLE]),
            'income': avg(income_by_person.get(person, ZERO)),
        } for person in people],
        'income_sources': sorted(
            ({'name': name, 'avg': avg(total)} for name, total in income_by_source.items()),
            key=lambda item: -item['avg'],
        ),
        'monthly': monthly,
        'kind_choices': [(kind, KIND_SHORT[kind]) for kind in KIND_ORDER],
        'chart': {
            'labels': [month.isoformat() for month in month_starts],
            'essential': [float(row['essential']) for row in monthly],
            'lifestyle': [float(row['lifestyle']) for row in monthly],
            'savings': [float(row['savings']) for row in monthly],
            'income': [float(row['income']) for row in monthly],
        },
    }


# ---------------------------------------------------------------------------
# Szczegóły jednego miesiąca
# ---------------------------------------------------------------------------

COMPARE_MONTHS = 6


def _delta(amount, average):
    if average is None:
        return None
    return {
        'amount': _money(amount - average),
        'percent': ((amount - average) / average * HUNDRED).quantize(Decimal('1')) if average else None,
    }


def build_month_detail(user, month, today=None):
    """Jeden miesiąc całego domu na tle średniej z COMPARE_MONTHS miesięcy przed nim.

    Pozycje (tytuł, sklep, data) tylko z moich kont i kont wspólnych;
    z kont osobistych domowników wyłącznie sumy.
    """
    today = today or timezone.localdate()
    current = today.replace(day=1)
    month = month.replace(day=1)
    scope = household_scope(user)
    entries = {entry.account.pk: entry for entry in scope.included}
    account_ids = list(entries)
    end = _add_months(month, 1)
    compare_start = _add_months(month, -COMPARE_MONTHS)
    expense_rows, income_rows = _monthly_rows(account_ids, compare_start, end)
    kinds = category_kinds({row['category'] for row in expense_rows})
    people = [entry.label for entry in scope.included]

    def month_key(value):
        return value.date() if hasattr(value, 'date') else value

    now = defaultdict(lambda: ZERO)
    before = defaultdict(lambda: ZERO)
    now_person = defaultdict(lambda: defaultdict(lambda: ZERO))
    kind_now = defaultdict(lambda: ZERO)
    kind_before = defaultdict(lambda: ZERO)
    before_months = set()
    for row in expense_rows:
        key = month_key(row['month_start'])
        kind = kinds[row['category']]
        if key == month:
            now[row['category']] += row['total']
            now_person[row['category']][entries[row['account_id']].label] += row['total']
            kind_now[kind] += row['total']
        else:
            before[row['category']] += row['total']
            kind_before[kind] += row['total']
            before_months.add(key)

    income_now = defaultdict(lambda: ZERO)
    income_before = ZERO
    for row in income_rows:
        key = month_key(row['month_start'])
        if key == month:
            income_now[row['source'] or 'Inne'] += row['total']
        else:
            income_before += row['total']
            before_months.add(key)

    divisor = Decimal(len(before_months)) if before_months else None

    def average(value):
        return _money(value / divisor) if divisor else None

    items = defaultdict(list)
    own_ids = [pk for pk, entry in entries.items() if entry.role in ('me', 'shared')]
    for expense in (
        Daily.objects.filter(account_id__in=own_ids, date__gte=month, date__lt=end)
        .exclude(category=TRANSFER_TO_SHARED_CATEGORY)
        .order_by('-cost', 'date')
        .values('date', 'title', 'store', 'cost', 'category', 'account_id')
    ):
        items[expense['category']].append({**expense, 'account': entries[expense['account_id']].label})
    hidden_people = [entry.label for entry in scope.included if entry.role == 'partner']

    groups = []
    for kind in KIND_ORDER:
        names = [name for name in set(now) | set(before) if kinds[name] == kind]
        if not names:
            continue
        names.sort(key=lambda name: (-now.get(name, ZERO), -before.get(name, ZERO)))
        largest = max([now.get(name, ZERO) for name in names] + [average(before.get(name, ZERO)) or ZERO for name in names]) or Decimal('1')
        rows = []
        for name in names:
            amount = _money(now.get(name, ZERO))
            avg_value = average(before.get(name, ZERO))
            rows.append({
                'name': name,
                'amount': amount,
                'avg': avg_value,
                'delta': _delta(amount, avg_value),
                'bar': (amount / largest * HUNDRED).quantize(Decimal('0.1')),
                'avg_bar': ((avg_value or ZERO) / largest * HUNDRED).quantize(Decimal('0.1')),
                'people': [{'label': person, 'amount': _money(now_person[name].get(person, ZERO))} for person in people],
                'items': items.get(name, []),
                'hidden_amount': _money(sum((now_person[name].get(person, ZERO) for person in hidden_people), ZERO)),
            })
        groups.append({
            'kind': kind,
            'label': KIND_LABELS[kind],
            # Więcej odłożonych pieniędzy to dobra zmiana, więcej wydatków - zła.
            'good_when_up': kind == SAVINGS,
            'amount': _money(kind_now[kind]),
            'avg': average(kind_before[kind]),
            'delta': _delta(_money(kind_now[kind]), average(kind_before[kind])),
            'rows': rows,
        })

    income_total = _money(sum(income_now.values(), ZERO))
    spending = _money(kind_now[ESSENTIAL] + kind_now[LIFESTYLE])
    spending_avg = average(kind_before[ESSENTIAL] + kind_before[LIFESTYLE])
    previous = _add_months(month, -1)
    following = _add_months(month, 1)
    return {
        'scope': scope,
        'month': month,
        'in_progress': month == current,
        'previous': previous,
        'next': following if following <= current else None,
        'has_data': bool(now or income_now),
        'compare_months': len(before_months),
        'essential': _money(kind_now[ESSENTIAL]),
        'essential_avg': average(kind_before[ESSENTIAL]),
        'essential_delta': _delta(_money(kind_now[ESSENTIAL]), average(kind_before[ESSENTIAL])),
        'spending': spending,
        'spending_avg': spending_avg,
        'spending_delta': _delta(spending, spending_avg),
        'savings': _money(kind_now[SAVINGS]),
        'income': income_total,
        'income_avg': average(income_before),
        'income_delta': _delta(income_total, average(income_before)),
        'balance': _money(income_total - spending - kind_now[SAVINGS]),
        'groups': groups,
        'people': people,
        'hidden_people': hidden_people,
        'income_sources': sorted(
            ({'name': name, 'amount': _money(total)} for name, total in income_now.items()),
            key=lambda item: -item['amount'],
        ),
    }
