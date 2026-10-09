"""Reguły importu wyciągu ustawione przez domownika (model ImportRule).

Reguła mówi: pozycja z wyciągu, w której pojawi się jedno ze słów, dostaje
tę kategorię (albo źródło przychodu) i opcjonalnie opis i nazwę sklepu.
Reguły należą do konta finansowego, więc na koncie wspólnym widzą je
wszyscy jego członkowie.

Kolejność podpowiedzi przy imporcie (finance.bank_import):
1. reguły domownika, od góry - wygrywa pierwsza pasująca,
2. reguła wbudowana „Inwestycje” (przelew do biura maklerskiego),
3. historia konta (jak ostatnio zapisano pozycję z tego sklepu),
4. reguły wbudowane (EXPENSE_RULES).

Zapis słów jak w kategoriach spiżarni: przecinki albo linie, całe słowo,
gwiazdka na końcu = początek słowa, kilka słów = fraza. Inaczej niż w
spiżarni polskie znaki nie mają znaczenia, bo banki często je gubią
(„ZABKA”, „Zona”).
"""
import re
from dataclasses import dataclass

from django.db import transaction

from .models import ImportRule
from .text_utils import normalize_text

PATTERN_MIN_LENGTH = 3
PATTERN_MAX_LENGTH = 80
PATTERNS_PER_RULE_MAX = 60
LABEL_MAX_LENGTH = 100
TEXT_MAX_LENGTH = 255

KINDS = (ImportRule.EXPENSE, ImportRule.INCOME)


class ImportRuleError(ValueError):
    """Błąd do pokazania użytkownikowi (po polsku)."""


# ---------------------------------------------------------------------------
# Słowa kluczowe
# ---------------------------------------------------------------------------

def split_patterns(raw):
    """Wzorce z pola tekstowego: po przecinkach, średnikach albo w liniach."""
    return [' '.join(part.split()) for part in re.split(r'[,;\n]+', str(raw or '')) if part.strip()]


def pattern_key(pattern):
    """„Żabka*” -> ('zabka', True): znormalizowana treść i czy to początek słowa."""
    pattern = str(pattern or '').strip()
    return normalize_text(pattern.rstrip('*')), pattern.endswith('*')


def parse_patterns(raw):
    """Lista wzorców z formularza (w postaci wpisanej). Rzuca ImportRuleError."""
    patterns = []
    seen = set()
    problems = []
    for part in split_patterns(raw):
        if len(part) > PATTERN_MAX_LENGTH:
            problems.append(f'„{part[:30]}…” jest za długie (maks. {PATTERN_MAX_LENGTH} znaków).')
            continue
        if '*' in part.rstrip('*') or part.endswith('**'):
            problems.append(f'„{part}”: gwiazdka może stać tylko na końcu słowa, np. „żabk*”.')
            continue
        key = pattern_key(part)
        if len(key[0].replace(' ', '')) < PATTERN_MIN_LENGTH:
            problems.append(f'„{part}” jest za krótkie - wpisz co najmniej {PATTERN_MIN_LENGTH} litery lub cyfry.')
            continue
        if key not in seen:
            seen.add(key)
            patterns.append(part)
    if problems:
        raise ImportRuleError(' '.join(problems))
    if not patterns:
        raise ImportRuleError('Wpisz co najmniej jedno słowo, np. nazwę sklepu.')
    if len(patterns) > PATTERNS_PER_RULE_MAX:
        raise ImportRuleError(f'Jedna reguła może mieć najwyżej {PATTERNS_PER_RULE_MAX} słów.')
    return patterns


def text_has_pattern(padded_text, key):
    """``padded_text`` = f' {normalize_text(...)} '. Lewa granica słowa zawsze,
    prawa - chyba że wzorzec kończy się gwiazdką."""
    body, prefix = key
    if not body:
        return False
    return f' {body}' in padded_text if prefix else f' {body} ' in padded_text


# ---------------------------------------------------------------------------
# Dopasowanie przy imporcie
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuleMatch:
    rule: ImportRule
    number: int
    pattern: str

    @property
    def reason(self):
        return f'Twoja reguła nr {self.number}: {self.pattern}'


class ImportRuleIndex:
    """Reguły konta pobrane raz na cały import (jedno zapytanie)."""

    __slots__ = ('_rules',)

    def __init__(self, account=None, rules=None):
        self._rules = {ImportRule.EXPENSE: [], ImportRule.INCOME: []}
        if rules is None:
            rules = rules_of(account) if account is not None else []
        for rule in rules:
            bucket = self._rules.get(rule.kind)
            if bucket is None:
                continue
            keys = [(pattern, pattern_key(pattern)) for pattern in rule.pattern_list]
            bucket.append((len(bucket) + 1, rule, keys))

    def __bool__(self):
        return any(self._rules.values())

    def match(self, kind, *parts):
        padded = f' {normalize_text(" ".join(str(part or "") for part in parts))} '
        if not padded.strip():
            return None
        for number, rule, keys in self._rules.get(kind, []):
            for pattern, key in keys:
                if text_has_pattern(padded, key):
                    return RuleMatch(rule=rule, number=number, pattern=pattern)
        return None


# ---------------------------------------------------------------------------
# Zapis i kolejność
# ---------------------------------------------------------------------------

def rules_of(account, kind=None):
    queryset = ImportRule.objects.filter(account=account)
    if kind is not None:
        queryset = queryset.filter(kind=kind)
    return queryset.order_by('position', 'id')


def _clean_label(kind, label):
    label = ' '.join(str(label or '').split())
    if not label:
        raise ImportRuleError(
            'Wpisz źródło, które ma dostać przychód.' if kind == ImportRule.INCOME
            else 'Wpisz kategorię, którą ma dostać wydatek.'
        )
    if len(label) > LABEL_MAX_LENGTH:
        raise ImportRuleError(f'Nazwa jest za długa (maks. {LABEL_MAX_LENGTH} znaków).')
    return label


def _clean_optional(value):
    return ' '.join(str(value or '').split())[:TEXT_MAX_LENGTH]


def _reindex(rules):
    for position, rule in enumerate(rules):
        if rule.position != position:
            rule.position = position
            rule.save(update_fields=['position'])


def save_rule(account, kind, *, patterns, label, title='', store_name='', position=None, rule=None, user=None):
    """Tworzy albo zmienia regułę. ``position``: 1 = sprawdzana jako pierwsza."""
    if kind not in KINDS:
        raise ImportRuleError('Nieznany rodzaj reguły.')
    pattern_list = parse_patterns(patterns)
    label = _clean_label(kind, label)
    with transaction.atomic():
        rules = list(rules_of(account, kind).select_for_update())
        if rule is None:
            rule = ImportRule(account=account, kind=kind, created_by=user)
        else:
            rules = [other for other in rules if other.pk != rule.pk]
        rule.patterns = '\n'.join(pattern_list)
        rule.label = label
        rule.title = _clean_optional(title)
        rule.store_name = _clean_optional(store_name) if kind == ImportRule.EXPENSE else ''
        if position is None:
            index = len(rules) if rule.pk is None else min(rule.position, len(rules))
        else:
            index = max(0, min(len(rules), int(position) - 1))
        rule.position = index
        rule.save()
        rules.insert(index, rule)
        _reindex(rules)
    return rule


def move_rule(rule, offset):
    with transaction.atomic():
        rules = list(rules_of(rule.account_id, rule.kind).select_for_update())
        index = next((i for i, other in enumerate(rules) if other.pk == rule.pk), None)
        if index is None:
            return False
        target = index + offset
        if not 0 <= target < len(rules):
            return False
        rules.insert(target, rules.pop(index))
        _reindex(rules)
    return True


@dataclass
class RememberRequest:
    """„Zapamiętaj dla tego sklepu” z podglądu importu."""
    kind: str
    key: str            # oryginalna nazwa sklepu / kontrahenta z wyciągu
    label: str
    title: str = ''
    store_name: str = ''


def remember_rules(account, user, requests):
    """Zapisuje reguły z podglądu. Zwraca (utworzone, zmienione).

    Reguła z jednym słowem równym nazwie sklepu jest zmieniana, a nie
    dublowana. Zapamiętana reguła trafia na górę listy, żeby wygrała
    z ogólniejszymi regułami domownika.
    """
    created = updated = 0
    # Ostatnia decyzja dla tego samego sklepu wygrywa.
    unique = {}
    for request in requests:
        key = pattern_key(request.key)
        if request.kind not in KINDS or len(key[0].replace(' ', '')) < PATTERN_MIN_LENGTH:
            continue
        try:
            request.label = _clean_label(request.kind, request.label)
        except ImportRuleError:
            continue
        unique[(request.kind, key[0])] = request
    if not unique:
        return created, updated

    with transaction.atomic():
        for (kind, key), request in unique.items():
            rules = list(rules_of(account, kind).select_for_update())
            existing = next(
                (rule for rule in rules if [pattern_key(p) for p in rule.pattern_list] == [(key, False)]),
                None,
            )
            if existing is None:
                rule = ImportRule(
                    account=account,
                    kind=kind,
                    # Przecinek albo gwiazdka w nazwie sklepu zmieniłyby znaczenie wzorca.
                    patterns=' '.join(re.sub(r'[,;*]+', ' ', str(request.key)).split())[:PATTERN_MAX_LENGTH],
                    created_by=user,
                )
                created += 1
            else:
                rule = existing
                rules.remove(existing)
                updated += 1
            rule.label = request.label
            rule.title = _clean_optional(request.title)
            rule.store_name = _clean_optional(request.store_name) if kind == ImportRule.EXPENSE else ''
            rule.position = 0
            rule.save()
            _reindex([rule, *rules])
    return created, updated
