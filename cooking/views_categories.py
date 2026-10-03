"""Strona "Kategorie" w spiżarni: kategorie i reguły automatycznego wyboru.

Kategorie są wspólne dla całego domu (jak spiżarnia), więc może je zmieniać
każdy zalogowany domownik. Logika zmian jest w cooking.services.categories,
tu są tylko formularze i komunikaty.
"""
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from .constants import PANTRY_CATEGORY_OTHER
from .models import PantryCategory, PantryCategoryRule, PantryProduct
from .services import categories as service
from .services.polish import polish_count

TAB_CATEGORIES = 'kategorie'
TAB_RULES = 'reguly'
KIND_PARAM = {'slowa': PantryCategoryRule.KIND_KEYWORDS, 'tagi': PantryCategoryRule.KIND_TAGS}
KIND_SLUG = {value: key for key, value in KIND_PARAM.items()}


def _page_url(tab=TAB_CATEGORIES, anchor=''):
    url = reverse('cooking:pantry-categories')
    if tab == TAB_RULES:
        url += '?tab=reguly'
    return url + (f'#{anchor}' if anchor else '')


def _rule_anchor(rule):
    return f'regula-{rule.pk}'


def _product_counts():
    return dict(
        PantryProduct.objects.exclude(category='').values_list('category')
        .annotate(total=Count('id')).values_list('category', 'total')
    )


def _rules_json(kind):
    rules = (
        PantryCategoryRule.objects.filter(kind=kind).select_related('category')
        .order_by('position', 'id')
    )
    return [
        {'rule': rule, 'number': index, 'patterns': rule.pattern_list}
        for index, rule in enumerate(rules, start=1)
    ]


def _keyword_conflict_warning(keywords, rule):
    conflicts = service.keyword_conflicts(keywords, exclude_rule_id=rule.pk)
    if not conflicts:
        return ''
    my_number = next(
        (index for index, (rule_id, _c, _w) in enumerate(service.taxonomy().keyword_rules, start=1)
         if rule_id == rule.pk),
        None,
    )
    parts = []
    for keyword, places in sorted(conflicts.items()):
        where = ', '.join(f'nr {number} ({category})' for number, category in places)
        parts.append(f'„{keyword}” – także w regule {where}')
    first_other = min(number for places in conflicts.values() for number, _ in places)
    tail = (
        'Ta reguła jest wyżej, więc wygrywa.'
        if my_number is not None and my_number < first_other
        else 'Wygrywa reguła wyżej na liście – przesuń tę regułę, jeśli ma mieć pierwszeństwo.'
    )
    return 'Powtórzone słowa: ' + '; '.join(parts) + '. ' + tail


class PantryCategoriesView(LoginRequiredMixin, View):
    template_name = 'cooking/pantry_categories.html'

    def get(self, request):
        tab = TAB_RULES if request.GET.get('tab') == TAB_RULES else TAB_CATEGORIES
        counts = _product_counts()
        rule_counts = dict(
            PantryCategoryRule.objects.filter(kind=PantryCategoryRule.KIND_KEYWORDS)
            .values_list('category').annotate(total=Count('id')).values_list('category', 'total')
        )
        categories = list(PantryCategory.objects.order_by('group', 'position', 'id'))
        groups = []
        for value, label in PantryCategory.GROUP_CHOICES:
            members = [category for category in categories if category.group == value]
            for category in members:
                category.product_count = counts.get(category.name, 0)
                category.rule_count = rule_counts.get(category.pk, 0)
            groups.append({'value': value, 'label': label, 'categories': members})

        test_name = request.GET.get('sprawdz', '').strip()[:160]
        test_result = None
        if test_name:
            match = service.match_text(service.searchable_text(test_name))
            test_result = {'name': test_name, 'match': match}

        keyword_rules = _rules_json(PantryCategoryRule.KIND_KEYWORDS)
        tag_rules = _rules_json(PantryCategoryRule.KIND_TAGS)
        return render(request, self.template_name, {
            'tab': tab,
            'groups': groups,
            'category_total': len(categories),
            'other_name': PANTRY_CATEGORY_OTHER,
            'other_count': counts.get(PANTRY_CATEGORY_OTHER, 0),
            'uncategorized_count': PantryProduct.objects.filter(category='').count(),
            'group_choices': PantryCategory.GROUP_CHOICES,
            'keyword_rules': keyword_rules,
            'tag_rules': tag_rules,
            'test_result': test_result,
            'test_name': test_name,
        })


class PantryCategoryCreateView(LoginRequiredMixin, View):
    def post(self, request):
        try:
            category = service.create_category(request.POST.get('name'), request.POST.get('group'))
        except service.CategoryError as error:
            messages.error(request, str(error))
            return redirect(_page_url())
        messages.success(
            request,
            f'Dodano kategorię „{category.name}”. Dodaj jej słowa kluczowe, żeby nowe produkty trafiały tu same.',
        )
        return redirect(_page_url(anchor=f'kategoria-{category.pk}'))


class PantryCategoryEditView(LoginRequiredMixin, View):
    template_name = 'cooking/pantry_category_form.html'

    def _render(self, request, category, form_values=None, status=200):
        rules = [
            item for item in _rules_json(PantryCategoryRule.KIND_KEYWORDS)
            if item['rule'].category_id == category.pk
        ]
        return render(request, self.template_name, {
            'category': category,
            'form_values': form_values or {'name': category.name, 'group': category.group},
            'group_choices': PantryCategory.GROUP_CHOICES,
            'rules': rules,
            'product_count': PantryProduct.objects.filter(category=category.name).count(),
        }, status=status)

    def get(self, request, category_id):
        return self._render(request, get_object_or_404(PantryCategory, pk=category_id))

    def post(self, request, category_id):
        category = get_object_or_404(PantryCategory, pk=category_id)
        old_name = category.name
        try:
            category = service.update_category(category, request.POST.get('name'), request.POST.get('group'))
        except service.CategoryError as error:
            messages.error(request, str(error))
            return self._render(request, category, form_values=request.POST, status=400)
        if old_name != category.name:
            messages.success(
                request,
                f'Zmieniono nazwę na „{category.name}”. Produkty, listy zakupów i przepisy mają już nową nazwę.',
            )
        else:
            messages.success(request, f'Zapisano kategorię „{category.name}”.')
        return redirect(_page_url(anchor=f'kategoria-{category.pk}'))


class PantryCategoryDeleteView(LoginRequiredMixin, View):
    template_name = 'cooking/pantry_category_delete.html'

    def _render(self, request, category, status=200):
        usage = service.usage_counts(category.name)
        others = PantryCategory.objects.exclude(pk=category.pk).order_by('group', 'position', 'id')
        return render(request, self.template_name, {
            'category': category,
            'usage': usage,
            'used': any(usage.values()),
            'rule_count': category.rules.count(),
            'others': others,
            'other_name': PANTRY_CATEGORY_OTHER,
        }, status=status)

    def get(self, request, category_id):
        return self._render(request, get_object_or_404(PantryCategory, pk=category_id))

    def post(self, request, category_id):
        category = get_object_or_404(PantryCategory, pk=category_id)
        name = category.name
        move_to = (request.POST.get('move_to') or '').strip()
        try:
            moved = service.delete_category(category, move_to=move_to)
        except service.CategoryError as error:
            messages.error(request, str(error))
            return self._render(request, category, status=400)
        target = f'„{move_to}”' if move_to else '„Bez kategorii”'
        detail = f' {polish_count(moved, "produkt trafił", "produkty trafiły", "produktów trafiło")} do {target}.' if moved else ''
        messages.success(request, f'Usunięto kategorię „{name}”.{detail}')
        return redirect(_page_url())


class PantryCategoryMoveView(LoginRequiredMixin, View):
    def post(self, request, category_id):
        category = get_object_or_404(PantryCategory, pk=category_id)
        offset = -1 if request.POST.get('direction') == 'up' else 1
        service.move_category(category, offset)
        return redirect(_page_url(anchor=f'kategoria-{category.pk}'))


class PantryCategoryRuleFormView(LoginRequiredMixin, View):
    """Dodawanie (rule_id=None) i edycja reguły."""

    template_name = 'cooking/pantry_category_rule_form.html'

    def _context(self, rule, kind, form_values, category_id=None):
        count = PantryCategoryRule.objects.filter(kind=kind).count()
        if rule.pk is None:
            count += 1
            default_position = count
        else:
            default_position = PantryCategoryRule.objects.filter(
                kind=kind, position__lt=rule.position,
            ).count() + 1
        return {
            'rule': rule,
            'kind': kind,
            'kind_slug': KIND_SLUG[kind],
            'is_tags': kind == PantryCategoryRule.KIND_TAGS,
            'categories': PantryCategory.objects.order_by('group', 'position', 'id'),
            'positions': range(1, count + 1),
            'form_values': {
                'category': str(category_id or (rule.category_id if rule.pk else '')),
                'patterns': ', '.join(rule.pattern_list) if rule.pk else '',
                'position': str(default_position),
                **(form_values or {}),
            },
        }

    def _rule(self, rule_id, kind_slug):
        if rule_id is not None:
            return get_object_or_404(PantryCategoryRule.objects.select_related('category'), pk=rule_id)
        kind = KIND_PARAM.get(kind_slug, PantryCategoryRule.KIND_KEYWORDS)
        return PantryCategoryRule(kind=kind)

    def get(self, request, rule_id=None):
        rule = self._rule(rule_id, request.GET.get('rodzaj'))
        category_id = request.GET.get('kategoria')
        return render(request, self.template_name, self._context(rule, rule.kind, None, category_id))

    def post(self, request, rule_id=None):
        rule = self._rule(rule_id, request.POST.get('rodzaj'))
        kind = rule.kind
        form_values = {
            'category': request.POST.get('category', ''),
            'patterns': request.POST.get('patterns', ''),
            'position': request.POST.get('position', ''),
        }
        category = PantryCategory.objects.filter(pk=form_values['category']).first() if form_values['category'].isdigit() else None
        try:
            if category is None:
                raise service.CategoryError('Wybierz kategorię, do której ma trafiać produkt.')
            position = int(form_values['position']) if form_values['position'].isdigit() else None
            created = rule.pk is None
            rule = service.save_rule(category, kind, form_values['patterns'], position=position, rule=None if created else rule)
        except service.CategoryError as error:
            messages.error(request, str(error))
            return render(request, self.template_name, self._context(rule, kind, form_values), status=400)

        messages.success(request, f'{"Dodano" if created else "Zapisano"} regułę dla kategorii „{category.name}”.')
        if kind == PantryCategoryRule.KIND_KEYWORDS:
            warning = _keyword_conflict_warning(rule.pattern_list, rule)
            if warning:
                messages.warning(request, warning)
        return redirect(_page_url(TAB_RULES, anchor=_rule_anchor(rule)))


class PantryCategoryRuleDeleteView(LoginRequiredMixin, View):
    def post(self, request, rule_id):
        rule = get_object_or_404(PantryCategoryRule.objects.select_related('category'), pk=rule_id)
        name = rule.category.name
        service.delete_rule(rule)
        messages.success(request, f'Usunięto regułę kategorii „{name}”.')
        return redirect(_page_url(TAB_RULES))


class PantryCategoryRuleMoveView(LoginRequiredMixin, View):
    def post(self, request, rule_id):
        rule = get_object_or_404(PantryCategoryRule, pk=rule_id)
        offset = -1 if request.POST.get('direction') == 'up' else 1
        service.move_rule(rule, offset)
        return redirect(_page_url(TAB_RULES, anchor=_rule_anchor(rule)))


class PantryCategoryRulesRestoreView(LoginRequiredMixin, View):
    def post(self, request):
        created = service.restore_default_rules()
        extra = (
            f' Utworzono od nowa {polish_count(created, "brakującą kategorię", "brakujące kategorie", "brakujących kategorii")}.'
            if created else ''
        )
        messages.success(request, f'Przywrócono domyślne reguły.{extra}')
        return redirect(_page_url(TAB_RULES))
