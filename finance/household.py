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


def build_household_analysis(user, months=DEFAULT_PERIOD, today=None):
    if months not in PERIOD_CHOICES:
        months = DEFAULT_PERIOD
    scope = household_scope(user)
    start, end = analysis_period(months, today)
    month_starts = [_add_months(start, offset) for offset in range(months)]
    entries = {entry.account.pk: entry for entry in scope.included}
    account_ids = list(entries)

    expense_rows = (
        Daily.objects.filter(account_id__in=account_ids, date__gte=start, date__lt=end)
        .exclude(category=TRANSFER_TO_SHARED_CATEGORY)
        .annotate(month_start=TruncMonth('date'))
        .values('account_id', 'category', 'month_start')
        .annotate(total=Sum('cost'))
    )
    income_rows = (
        Income.objects.filter(account_id__in=account_ids, date__gte=start, date__lt=end, linked_expense__isnull=True)
        .annotate(month_start=TruncMonth('date'))
        .values('account_id', 'source', 'month_start')
        .annotate(total=Sum('amount'))
    )
    expense_rows = list(expense_rows)
    income_rows = list(income_rows)
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
