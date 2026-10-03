"""Kategorie spiżarni i reguły automatycznego wyboru kategorii.

Kategorie i reguły edytują domownicy na stronie "Kategorie" w spiżarni
(cooking.views_categories). Ten moduł jest jedynym miejscem, które je czyta:
lista do formularzy, grupy "Spożywcze"/"Dom", reguły dla podpowiedzi
kategorii (cooking.services.product_catalog) i operacje zmiany nazwy,
usuwania i porządkowania.

Odczyt jest zapamiętany w procesie, bo podpowiedź kategorii liczy się nawet
dla każdego produktu na liście. Zmiana w bazie zmienia wersję we wspólnym
cache (plikowym, jednym dla workerów gunicorna), a pozostałe procesy
sprawdzają ją najwyżej co CACHE_RECHECK_SECONDS.
"""
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError, transaction
from django.db.models.functions import Lower

from .. import category_defaults as defaults
from ..constants import PANTRY_CATEGORY_OTHER

VERSION_CACHE_KEY = 'cooking:pantry-categories:version'
CACHE_RECHECK_SECONDS = 2.0
UNCATEGORIZED_LABEL = 'Bez kategorii'
RESERVED_NAMES = {PANTRY_CATEGORY_OTHER.casefold(), UNCATEGORIZED_LABEL.casefold()}

NAME_MAX_LENGTH = 60
PATTERN_MAX_LENGTH = 80
PATTERNS_PER_RULE_MAX = 300
KEYWORD_MIN_LENGTH = 2

GROUP_LABELS = defaults.GROUP_LABELS
GROUP_ORDER = (defaults.GROUP_FOOD, defaults.GROUP_HOME)


class CategoryError(ValueError):
    """Błąd do pokazania użytkownikowi (po polsku)."""


# ---------------------------------------------------------------------------
# Normalizacja tekstu - wspólna dla dopasowania i zapisu reguł
# ---------------------------------------------------------------------------

def searchable_text(*parts):
    """Tekst do przeszukania: małe litery, znaki inne niż litery i cyfry
    zamienione na spacje ("Coca-Cola 0,5 l" -> "coca cola 0 5 l")."""
    text = ' '.join(str(part or '') for part in parts).casefold()
    return ' '.join(re.sub(r'[^\w]+', ' ', text).split())


def normalize_keyword(keyword):
    """Słowo kluczowe w tej samej postaci co przeszukiwany tekst.

    Gwiazdka na końcu zostaje i oznacza "początek słowa".
    """
    keyword = str(keyword or '').strip()
    prefix = keyword.endswith('*')
    body = searchable_text(keyword.rstrip('*'))
    if not body:
        return ''
    return f'{body}*' if prefix else body


def text_has_keyword(searchable, keyword):
    """Dopasowanie całego słowa albo frazy; gwiazdka = początek słowa.

    Dopasowanie jest do granicy słowa z lewej strony, więc "ser" nie trafi
    w "serwetki", a "czyszcząc*" trafi w "czyszczące". Działa też dla fraz
    z gwiazdką ("paper towel*" -> "paper towels").
    """
    normalized = normalize_keyword(keyword)
    if not normalized:
        return False
    padded = f' {searchable} '
    if normalized.endswith('*'):
        return f' {normalized[:-1]}' in padded
    return f' {normalized} ' in padded


def normalize_tag(tag):
    """Tag Open Food Facts bez prefiksu języka: "en:Frozen foods" -> "frozen-foods"."""
    cleaned = str(tag or '').strip().casefold()
    cleaned = re.sub(r'^[a-z]{2,3}:', '', cleaned)
    return re.sub(r'[^a-z0-9]+', '-', cleaned).strip('-')


def _split_patterns(raw):
    """Wzorce z pola tekstowego: po przecinkach, średnikach albo w liniach."""
    return [part.strip() for part in re.split(r'[,;\n]+', str(raw or '')) if part.strip()]


def parse_keywords(raw):
    """Lista słów kluczowych z formularza. Rzuca CategoryError z opisem."""
    keywords = []
    seen = set()
    problems = []
    for part in _split_patterns(raw):
        if len(part) > PATTERN_MAX_LENGTH:
            problems.append(f'„{part[:30]}…” jest za długie (maks. {PATTERN_MAX_LENGTH} znaków).')
            continue
        if '*' in part.rstrip('*') or part.endswith('**'):
            problems.append(f'„{part}”: gwiazdka może stać tylko na końcu słowa, np. „chleb*”.')
            continue
        normalized = normalize_keyword(part)
        if len(normalized.rstrip('*').replace(' ', '')) < KEYWORD_MIN_LENGTH:
            problems.append(f'„{part}” jest za krótkie - wpisz co najmniej {KEYWORD_MIN_LENGTH} litery.')
            continue
        if normalized not in seen:
            seen.add(normalized)
            keywords.append(normalized)
    if problems:
        raise CategoryError(' '.join(problems))
    if not keywords:
        raise CategoryError('Wpisz co najmniej jedno słowo kluczowe.')
    if len(keywords) > PATTERNS_PER_RULE_MAX:
        raise CategoryError(f'Jedna reguła może mieć najwyżej {PATTERNS_PER_RULE_MAX} słów.')
    return keywords


def parse_tags(raw):
    tags = []
    seen = set()
    for part in _split_patterns(raw):
        tag = normalize_tag(part)
        if len(tag) < KEYWORD_MIN_LENGTH:
            raise CategoryError(f'„{part}” nie wygląda na tag Open Food Facts (np. „en:frozen-foods”).')
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    if not tags:
        raise CategoryError('Wpisz co najmniej jeden tag.')
    if len(tags) > PATTERNS_PER_RULE_MAX:
        raise CategoryError(f'Jedna reguła może mieć najwyżej {PATTERNS_PER_RULE_MAX} tagów.')
    return tags


# ---------------------------------------------------------------------------
# Odczyt z zapamiętaniem
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Taxonomy:
    # [(etykieta grupy, (nazwy...)), ...] bez pustych grup i bez "Inne"
    groups: tuple
    # wszystkie nazwy w kolejności wyświetlania + "Inne" na końcu
    names: tuple
    home: frozenset
    codes: dict = field(default_factory=dict)
    # [(id reguły, nazwa kategorii, wzorce), ...] w kolejności sprawdzania
    tag_rules: tuple = ()
    keyword_rules: tuple = ()

    def is_valid(self, name):
        return name in self.names


def _taxonomy_from_defaults():
    grouped = {group: [] for group in GROUP_ORDER}
    for _code, name, group in defaults.DEFAULT_CATEGORIES:
        grouped[group].append(name)
    return _build(
        grouped,
        {code: name for code, name, _ in defaults.DEFAULT_CATEGORIES},
        [(None, name, frozenset(tags)) for name, tags in defaults.DEFAULT_TAG_RULES],
        [(None, name, tuple(words)) for name, words in defaults.DEFAULT_KEYWORD_RULES],
    )


def _build(grouped, codes, tag_rules, keyword_rules):
    groups = tuple(
        (GROUP_LABELS[group], tuple(grouped[group]))
        for group in GROUP_ORDER if grouped.get(group)
    )
    names = tuple(name for _, names in groups for name in names) + (PANTRY_CATEGORY_OTHER,)
    return Taxonomy(
        groups=groups,
        names=names,
        home=frozenset(grouped.get(defaults.GROUP_HOME, ())),
        codes=codes,
        tag_rules=tuple(tag_rules),
        keyword_rules=tuple(keyword_rules),
    )


def _load_taxonomy():
    from ..models import PantryCategory, PantryCategoryRule

    try:
        categories = list(PantryCategory.objects.order_by('group', 'position', 'id'))
        if not categories:
            # Pusta tabela: baza przed migracją albo wyczyszczona w testach.
            return _taxonomy_from_defaults()
        rules = list(
            PantryCategoryRule.objects.select_related('category')
            .order_by('kind', 'position', 'id')
        )
    except DatabaseError:
        return _taxonomy_from_defaults()

    grouped = {group: [] for group in GROUP_ORDER}
    for category in categories:
        grouped.setdefault(category.group, []).append(category.name)
    tag_rules = [
        (rule.id, rule.category.name, frozenset(rule.pattern_list))
        for rule in rules if rule.kind == PantryCategoryRule.KIND_TAGS
    ]
    keyword_rules = [
        (rule.id, rule.category.name, tuple(rule.pattern_list))
        for rule in rules if rule.kind == PantryCategoryRule.KIND_KEYWORDS
    ]
    codes = {category.code: category.name for category in categories if category.code}
    return _build(grouped, codes, tag_rules, keyword_rules)


_lock = threading.Lock()
_state = {'taxonomy': None, 'version': None, 'checked_at': 0.0}


def _recheck_seconds():
    return getattr(settings, 'PANTRY_CATEGORIES_RECHECK_SECONDS', CACHE_RECHECK_SECONDS)


def taxonomy():
    """Aktualne kategorie i reguły (zapamiętane, patrz opis modułu)."""
    now = time.monotonic()
    current = _state['taxonomy']
    recheck = _recheck_seconds()
    if current is not None and recheck and now - _state['checked_at'] < recheck:
        return current
    version = cache.get(VERSION_CACHE_KEY)
    if version is None:
        version = uuid.uuid4().hex
        cache.add(VERSION_CACHE_KEY, version, None)
        version = cache.get(VERSION_CACHE_KEY, version)
    if current is not None and recheck and version == _state['version']:
        _state['checked_at'] = now
        return current
    loaded = _load_taxonomy()
    with _lock:
        _state.update(taxonomy=loaded, version=version, checked_at=now)
    return loaded


def invalidate():
    """Po każdej zmianie kategorii albo reguł (także z panelu admina)."""
    cache.set(VERSION_CACHE_KEY, uuid.uuid4().hex, None)
    with _lock:
        _state.update(taxonomy=None, version=None, checked_at=0.0)


def category_names():
    return taxonomy().names


def category_groups():
    return taxonomy().groups


def category_groups_json():
    """Grupy dla trybu zakupów offline (z "Inne" jako ostatnią grupą)."""
    return [
        {'label': label, 'categories': list(names)}
        for label, names in category_groups()
    ] + [{'label': '', 'categories': [PANTRY_CATEGORY_OTHER]}]


def is_valid_category(name):
    return taxonomy().is_valid(name)


def category_for_code(code):
    """Aktualna nazwa wbudowanej kategorii (np. po zmianie nazwy), albo ''."""
    return taxonomy().codes.get(code, '')


# ---------------------------------------------------------------------------
# Dopasowanie
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Match:
    category: str
    kind: str          # 'tags' | 'keywords'
    rule_id: object    # None dla danych domyślnych (pusta baza)
    position: int      # 1 = pierwsza reguła na liście
    pattern: str


def match_tags(tags, food_allowed=True):
    current = taxonomy()
    tags = set(tags)
    for index, (rule_id, category, known) in enumerate(current.tag_rules, start=1):
        if not food_allowed and category not in current.home:
            continue
        hit = tags & known
        if hit:
            return Match(category, 'tags', rule_id, index, sorted(hit)[0])
    return None


def match_text(searchable, food_allowed=True):
    current = taxonomy()
    for index, (rule_id, category, keywords) in enumerate(current.keyword_rules, start=1):
        if not food_allowed and category not in current.home:
            continue
        for keyword in keywords:
            if text_has_keyword(searchable, keyword):
                return Match(category, 'keywords', rule_id, index, keyword)
    return None


def tag_rule_priority(tag_values):
    """Pozycja pierwszej reguły tagów pasującej do wartości (do sortowania)."""
    current = taxonomy()
    for index, (_rule_id, _category, known) in enumerate(current.tag_rules):
        if set(tag_values) & known:
            return index
    return len(current.tag_rules)


def keyword_conflicts(keywords, exclude_rule_id=None):
    """Słowa, które są już w innych regułach: {słowo: [(pozycja, kategoria)]}."""
    wanted = set(keywords)
    conflicts = {}
    for index, (rule_id, category, existing) in enumerate(taxonomy().keyword_rules, start=1):
        if rule_id == exclude_rule_id:
            continue
        for keyword in wanted.intersection(existing):
            conflicts.setdefault(keyword, []).append((index, category))
    return conflicts


# ---------------------------------------------------------------------------
# Zmiany (strona "Kategorie")
# ---------------------------------------------------------------------------

def _models():
    from .. import models
    return models


def clean_category_name(raw, exclude_id=None):
    models = _models()
    name = ' '.join(str(raw or '').split())
    if not name:
        raise CategoryError('Podaj nazwę kategorii.')
    if len(name) > NAME_MAX_LENGTH:
        raise CategoryError(f'Nazwa może mieć najwyżej {NAME_MAX_LENGTH} znaków.')
    if name.casefold() in RESERVED_NAMES:
        raise CategoryError(f'„{name}” to nazwa zarezerwowana - wybierz inną.')
    existing = models.PantryCategory.objects.annotate(lower=Lower('name')).filter(lower=name.casefold())
    if exclude_id:
        existing = existing.exclude(pk=exclude_id)
    if existing.exists():
        raise CategoryError(f'Kategoria „{name}” już istnieje.')
    return name


def _clean_group(group):
    if group not in GROUP_ORDER:
        raise CategoryError('Wybierz grupę: Spożywcze albo Dom.')
    return group


def create_category(name, group):
    models = _models()
    with transaction.atomic():
        name = clean_category_name(name)
        group = _clean_group(group)
        last = (
            models.PantryCategory.objects.filter(group=group)
            .order_by('-position').values_list('position', flat=True).first()
        )
        category = models.PantryCategory.objects.create(
            name=name, group=group, position=(last + 1) if last is not None else 0,
        )
    invalidate()
    return category


def _replace_category_text(old, new):
    """Przepisuje nazwę kategorii we wszystkich miejscach, które trzymają ją
    jako tekst. new='' oznacza "bez kategorii". Zwraca liczbę produktów."""
    models = _models()
    products = models.PantryProduct.objects.filter(category=old).update(category=new)
    models.ProductGroup.objects.filter(category=old).update(category=new)
    models.RecipeStepIngredient.objects.filter(category=old).update(category=new)
    models.ShoppingListItem.objects.filter(category=old).update(category=new)
    models.ProductCatalogEntry.objects.filter(suggested_category=old).update(suggested_category=new)
    for layout in models.ShopLayout.objects.select_for_update():
        order = list(layout.category_order or [])
        if old not in order:
            continue
        if new and new not in order:
            order = [new if name == old else name for name in order]
        else:
            order = [name for name in order if name != old]
        layout.category_order = order
        layout.save(update_fields=['category_order', 'updated_at'])
    return products


def update_category(category, name, group):
    models = _models()
    with transaction.atomic():
        category = models.PantryCategory.objects.select_for_update().get(pk=category.pk)
        name = clean_category_name(name, exclude_id=category.pk)
        group = _clean_group(group)
        old_name = category.name
        if group != category.group:
            last = (
                models.PantryCategory.objects.filter(group=group)
                .order_by('-position').values_list('position', flat=True).first()
            )
            category.position = (last + 1) if last is not None else 0
        category.name = name
        category.group = group
        category.save()
        if old_name != name:
            _replace_category_text(old_name, name)
    invalidate()
    return category


def usage_counts(name):
    models = _models()
    return {
        'products': models.PantryProduct.objects.filter(category=name).count(),
        'groups': models.ProductGroup.objects.filter(category=name).count(),
        'items': models.ShoppingListItem.objects.filter(category=name).count(),
        'ingredients': models.RecipeStepIngredient.objects.filter(category=name).count(),
    }


def delete_category(category, move_to=''):
    """Usuwa kategorię (z jej regułami); to, co ją miało, dostaje move_to."""
    models = _models()
    if move_to and move_to != PANTRY_CATEGORY_OTHER:
        if move_to == category.name or not models.PantryCategory.objects.filter(name=move_to).exists():
            raise CategoryError('Wybierz inną kategorię, do której trafią produkty.')
    with transaction.atomic():
        category = models.PantryCategory.objects.select_for_update().get(pk=category.pk)
        if models.PantryCategory.objects.count() <= 1:
            raise CategoryError('Musi zostać co najmniej jedna kategoria.')
        moved = _replace_category_text(category.name, move_to)
        category.delete()
    invalidate()
    return moved


def _reorder(queryset, item, offset):
    items = list(queryset)
    index = next((i for i, other in enumerate(items) if other.pk == item.pk), None)
    if index is None:
        return False
    target = max(0, min(len(items) - 1, index + offset))
    if target == index:
        return False
    items.insert(target, items.pop(index))
    for position, other in enumerate(items):
        if other.position != position:
            other.position = position
            other.save(update_fields=['position'])
    return True


def move_category(category, offset):
    models = _models()
    with transaction.atomic():
        moved = _reorder(
            models.PantryCategory.objects.select_for_update().filter(group=category.group)
            .order_by('position', 'id'),
            category, offset,
        )
    if moved:
        invalidate()
    return moved


def _rules_of_kind(kind):
    return _models().PantryCategoryRule.objects.filter(kind=kind).order_by('position', 'id')


def save_rule(category, kind, raw_patterns, position=None, rule=None):
    """Tworzy albo zmienia regułę. position: 1 = pierwsza na liście."""
    models = _models()
    patterns = parse_tags(raw_patterns) if kind == models.PantryCategoryRule.KIND_TAGS else parse_keywords(raw_patterns)
    with transaction.atomic():
        rules = list(_rules_of_kind(kind).select_for_update())
        if rule is None:
            rule = models.PantryCategoryRule(kind=kind)
        else:
            rules = [other for other in rules if other.pk != rule.pk]
        rule.category = category
        rule.patterns = '\n'.join(patterns)
        if position is None:
            index = len(rules) if rule.pk is None else min(rule.position, len(rules))
        else:
            index = max(0, min(len(rules), int(position) - 1))
        rule.position = index
        rule.save()
        rules.insert(index, rule)
        for new_position, other in enumerate(rules):
            if other.position != new_position:
                other.position = new_position
                other.save(update_fields=['position'])
    invalidate()
    return rule


def delete_rule(rule):
    with transaction.atomic():
        kind = rule.kind
        rule.delete()
        for position, other in enumerate(_rules_of_kind(kind).select_for_update()):
            if other.position != position:
                other.position = position
                other.save(update_fields=['position'])
    invalidate()


def move_rule(rule, offset):
    with transaction.atomic():
        moved = _reorder(_rules_of_kind(rule.kind).select_for_update(), rule, offset)
    if moved:
        invalidate()
    return moved


def restore_default_rules():
    """Zastępuje wszystkie reguły domyślnymi. Kategorie zostają.

    Wbudowaną kategorię rozpoznaje po kodzie (działa też po zmianie nazwy).
    Brakujące wbudowane kategorie są tworzone od nowa. Zwraca liczbę
    utworzonych kategorii.
    """
    models = _models()
    created = 0
    with transaction.atomic():
        by_code = {
            category.code: category
            for category in models.PantryCategory.objects.select_for_update().exclude(code=None)
        }
        default_by_name = {name: (code, group) for code, name, group in defaults.DEFAULT_CATEGORIES}

        def category_for(default_name):
            nonlocal created
            code, group = default_by_name[default_name]
            if code in by_code:
                return by_code[code]
            existing = models.PantryCategory.objects.filter(name__iexact=default_name).first()
            if existing:
                if not existing.code:
                    existing.code = code
                    existing.save(update_fields=['code'])
                by_code[code] = existing
                return existing
            last = (
                models.PantryCategory.objects.filter(group=group)
                .order_by('-position').values_list('position', flat=True).first()
            )
            by_code[code] = models.PantryCategory.objects.create(
                name=default_name, group=group, code=code,
                position=(last + 1) if last is not None else 0,
            )
            created += 1
            return by_code[code]

        models.PantryCategoryRule.objects.all().delete()
        for kind, rules in (
            (models.PantryCategoryRule.KIND_TAGS, defaults.DEFAULT_TAG_RULES),
            (models.PantryCategoryRule.KIND_KEYWORDS, defaults.DEFAULT_KEYWORD_RULES),
        ):
            for position, (name, patterns) in enumerate(rules):
                values = sorted(patterns) if kind == models.PantryCategoryRule.KIND_TAGS else list(patterns)
                models.PantryCategoryRule.objects.create(
                    category=category_for(name), kind=kind, position=position,
                    patterns='\n'.join(values),
                )
    invalidate()
    return created
