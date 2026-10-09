"""Strona „Reguły importu”: reguły domownika dla importu wyciągu.

Reguły należą do aktywnego konta finansowego. Silnik i zapis są
w finance.import_rules, podpowiedzi - w finance.bank_import.
"""
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from utils.undo import delete_with_undo

from .account_utils import TRANSFER_TO_SHARED_CATEGORY, get_active_finance_account
from .bank_import import (
    EXPENSE_RULES,
    INCOME_RULES,
    ORIGIN_BUILTIN,
    ORIGIN_DEFAULT,
    ORIGIN_HISTORY,
    ORIGIN_RULE,
    BankHistoryIndex,
    _clean_text,
    _expense_store,
    parse_card_description,
    suggest_expense,
    suggest_income,
)
from .import_rules import ImportRuleError, ImportRuleIndex, move_rule, rules_of, save_rule
from .models import ImportRule, Income

KIND_PARAM = {'wydatki': ImportRule.EXPENSE, 'przychody': ImportRule.INCOME}
KIND_SLUG = {value: key for key, value in KIND_PARAM.items()}
TEST_MAX_LENGTH = 300


def _kind_from(value):
    return KIND_PARAM.get(value, ImportRule.EXPENSE)


def _page_url(kind=ImportRule.EXPENSE, anchor=''):
    url = reverse('finance:import_rules')
    if kind == ImportRule.INCOME:
        url += '?rodzaj=przychody'
    return url + (f'#{anchor}' if anchor else '')


def _rule_anchor(rule):
    return f'regula-{rule.pk}'


def builtin_pattern(keyword):
    """Słowo z EXPENSE_RULES w zapisie reguł domownika.

    Wbudowane słowa trafiają też w środek wyrazu („biedronka” w „xbiedronka”),
    chyba że mają spację z przodu. W zapisie domownika to najbliżej
    „początku słowa” (gwiazdka), a słowo ze spacjami z obu stron - całe słowo.
    """
    body = ' '.join(keyword.split())
    whole_word = keyword.startswith(' ') and keyword.endswith(' ')
    return body if whole_word or keyword.endswith(' ') else f'{body}*'


def builtin_rules(kind):
    source = EXPENSE_RULES if kind == ImportRule.EXPENSE else INCOME_RULES
    return [
        {'number': number, 'label': label, 'patterns': [builtin_pattern(keyword) for keyword in keywords]}
        for number, (label, keywords) in enumerate(source, start=1)
    ]


def label_choices(account, kind):
    # Podpowiedzi jak w podglądzie importu (ImportBankTransactionsView).
    from .views import INCOME_SOURCES, get_available_expense_categories
    if kind == ImportRule.EXPENSE:
        return [
            category for category in get_available_expense_categories(account)
            if category != TRANSFER_TO_SHARED_CATEGORY
        ]
    return sorted(set(INCOME_SOURCES).union(
        Income.objects.filter(account=account).exclude(source='').values_list('source', flat=True)
    ))


def _rule_items(account, kind):
    return [
        {'rule': rule, 'number': number, 'patterns': rule.pattern_list}
        for number, rule in enumerate(rules_of(account, kind), start=1)
    ]


def check_description(account, kind, text):
    """„Sprawdź opis z wyciągu”: co dostanie pozycja z takim opisem."""
    raw = str(text or '').strip()[:TEST_MAX_LENGTH]
    text = _clean_text(raw)
    if not text:
        return None
    history = BankHistoryIndex(account)
    rules = ImportRuleIndex(account)
    row = {'Opis': text}
    if kind == ImportRule.EXPENSE:
        # Surowy tekst: podwójna spacja oddziela sklep od miasta, jak w wyciągu.
        card = parse_card_description(raw)
        store = _expense_store(row, card)
        suggestion = suggest_expense(store, row, card, history=history, rules=rules)
    else:
        suggestion = suggest_income(row, history=history, rules=rules)
    return {'text': text, 'suggestion': suggestion, 'match': suggestion.rule_match}


class ImportRulesView(LoginRequiredMixin, View):
    template_name = 'finance/import_rules.html'

    def get(self, request):
        account = get_active_finance_account(request)
        kind = _kind_from(request.GET.get('rodzaj'))
        test_text = request.GET.get('sprawdz', '')
        counts = {
            value: ImportRule.objects.filter(account=account, kind=value).count()
            for value in KIND_SLUG
        }
        return render(request, self.template_name, {
            'account': account,
            'kind': kind,
            'kind_slug': KIND_SLUG[kind],
            'is_expense': kind == ImportRule.EXPENSE,
            'expense_count': counts[ImportRule.EXPENSE],
            'income_count': counts[ImportRule.INCOME],
            'rules': _rule_items(account, kind),
            'builtin': builtin_rules(kind),
            'test_text': test_text[:TEST_MAX_LENGTH],
            'test_result': check_description(account, kind, test_text),
            'origin_rule': ORIGIN_RULE,
            'origin_history': ORIGIN_HISTORY,
            'origin_builtin': ORIGIN_BUILTIN,
            'origin_default': ORIGIN_DEFAULT,
        })


class ImportRuleFormView(LoginRequiredMixin, View):
    """Dodawanie (rule_id=None) i edycja reguły."""

    template_name = 'finance/import_rule_form.html'

    def _rule(self, request, rule_id, kind_slug):
        account = get_active_finance_account(request)
        if rule_id is not None:
            return account, get_object_or_404(ImportRule, pk=rule_id, account=account)
        return account, ImportRule(account=account, kind=_kind_from(kind_slug))

    def _context(self, account, rule, form_values=None, prefill=None):
        kind = rule.kind
        count = rules_of(account, kind).count()
        if rule.pk is None:
            count += 1
            # Nowa reguła: domyślnie na górze, bo zwykle jest szczegółowa
            # (konkretny sklep) i ma wygrać z ogólnymi.
            default_position = 1
        else:
            default_position = rules_of(account, kind).filter(position__lt=rule.position).count() + 1
        values = {
            'patterns': ', '.join(rule.pattern_list) if rule.pk else '',
            'label': rule.label,
            'title': rule.title,
            'store_name': rule.store_name,
            'position': str(default_position),
        }
        values.update(prefill or {})
        values.update(form_values or {})
        return {
            'rule': rule,
            'kind': kind,
            'kind_slug': KIND_SLUG[kind],
            'is_expense': kind == ImportRule.EXPENSE,
            'positions': range(1, count + 1),
            'label_choices': label_choices(account, kind),
            'form_values': values,
            'back_url': _page_url(kind, _rule_anchor(rule) if rule.pk else ''),
        }

    def _prefill(self, request, kind):
        """„Skopiuj do swoich” (?wbudowana=N) albo słowo z „Sprawdź opis” (?wzor=...)."""
        prefill = {}
        number = request.GET.get('wbudowana', '')
        if number.isdigit():
            builtin = next((item for item in builtin_rules(kind) if item['number'] == int(number)), None)
            if builtin:
                prefill.update({'patterns': ', '.join(builtin['patterns']), 'label': builtin['label']})
        pattern = _clean_text(request.GET.get('wzor', ''))[:80]
        if pattern:
            prefill['patterns'] = pattern
        return prefill

    def get(self, request, rule_id=None):
        account, rule = self._rule(request, rule_id, request.GET.get('rodzaj'))
        prefill = self._prefill(request, rule.kind) if rule.pk is None else None
        return render(request, self.template_name, self._context(account, rule, prefill=prefill))

    def post(self, request, rule_id=None):
        account, rule = self._rule(request, rule_id, request.POST.get('rodzaj'))
        form_values = {
            name: request.POST.get(name, '')
            for name in ('patterns', 'label', 'title', 'store_name', 'position')
        }
        created = rule.pk is None
        try:
            label = _clean_text(form_values['label'])
            if rule.kind == ImportRule.EXPENSE and label == TRANSFER_TO_SHARED_CATEGORY:
                raise ImportRuleError(f'Kategoria „{TRANSFER_TO_SHARED_CATEGORY}” nie jest obsługiwana w imporcie.')
            position = int(form_values['position']) if form_values['position'].isdigit() else None
            rule = save_rule(
                account,
                rule.kind,
                patterns=form_values['patterns'],
                label=label,
                title=form_values['title'],
                store_name=form_values['store_name'],
                position=position,
                rule=None if created else rule,
                user=request.user,
            )
        except ImportRuleError as error:
            messages.error(request, str(error))
            return render(request, self.template_name, self._context(account, rule, form_values), status=400)

        messages.success(request, f'{"Dodano" if created else "Zapisano"} regułę „{rule.label}”.')
        return redirect(_page_url(rule.kind, _rule_anchor(rule)))


class ImportRuleDeleteView(LoginRequiredMixin, View):
    def post(self, request, rule_id):
        account = get_active_finance_account(request)
        rule = get_object_or_404(ImportRule, pk=rule_id, account=account)
        kind = rule.kind
        # Bez pytania „Na pewno?”: dymek ma „Cofnij”.
        delete_with_undo(
            request,
            rule,
            f'Usunięto regułę „{rule.label}”.',
            restored_message=f'Przywrócono regułę „{rule.label}”.',
            anchor=_rule_anchor(rule),
        )
        return redirect(_page_url(kind))


class ImportRuleMoveView(LoginRequiredMixin, View):
    def post(self, request, rule_id):
        account = get_active_finance_account(request)
        rule = get_object_or_404(ImportRule, pk=rule_id, account=account)
        move_rule(rule, -1 if request.POST.get('direction') == 'up' else 1)
        return redirect(_page_url(rule.kind, _rule_anchor(rule)))
