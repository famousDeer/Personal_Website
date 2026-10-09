"""Etap 3 importu: reguły domownika, „Zapamiętaj” w podglądzie, strona reguł."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from finance.account_utils import TRANSFER_TO_SHARED_CATEGORY
from finance.bank_import import parse_bank_csv
from finance.import_rules import (
    ImportRuleError,
    ImportRuleIndex,
    RememberRequest,
    move_rule,
    parse_patterns,
    pattern_key,
    remember_rules,
    save_rule,
    text_has_pattern,
)
from finance.models import Daily, FinanceAccount, ImportRule, Monthly

from .test_bank_import import _post_data_from_preview
from .test_bank_import_cleanup import ACCOUNT, card, upload

User = get_user_model()


def income_row(counterparty='Firma Testowa SP. Z O.O.', description='Wynagrodzenie 09/2026', amount='3000.00'):
    return [ACCOUNT, '2026-10-05', '2026-10-05', 'PRZELEW PRZYCHODZĄCY', '', counterparty, description,
            '', amount, '100.00', 'PLN']


def matches(pattern, text):
    from finance.text_utils import normalize_text
    return text_has_pattern(f' {normalize_text(text)} ', pattern_key(pattern))


class PatternTests(SimpleTestCase):
    def test_parse_patterns(self):
        self.assertEqual(parse_patterns('Fred, malinogród\nMr  Cleaner; Żabka, zabka'),
                         ['Fred', 'malinogród', 'Mr Cleaner', 'Żabka'])
        for raw, message in [
            ('', 'co najmniej jedno'),
            ('ab', 'za krótkie'),
            ('pie*rog', 'gwiazdka'),
            ('kawa**', 'gwiazdka'),
        ]:
            with self.subTest(raw=raw), self.assertRaisesMessage(ImportRuleError, message):
                parse_patterns(raw)

    def test_matching_rules(self):
        self.assertTrue(matches('ola', 'OLA SKLEP GDYNIA'))
        self.assertFalse(matches('ola', 'KOLACJA'))
        self.assertFalse(matches('ola', 'OLAF'))
        self.assertTrue(matches('pierog*', 'PIEROGARNIA POD LIPA'))
        self.assertTrue(matches('mr cleaner', 'ING*MR CLEANER KATOWICE'))
        self.assertFalse(matches('mr cleaner', 'MR CLEAN'))
        self.assertTrue(matches('Żabka', 'ZABKA Z1234'))
        self.assertTrue(matches('malinogród', 'Malinogrod'))


class RuleEngineTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='reguly', password='pass123')
        self.account = self.user.owned_finance_accounts.get(account_type='personal')

    def rule(self, patterns, label, kind=ImportRule.EXPENSE, account=None, **extra):
        return save_rule(account or self.account, kind, patterns=patterns, label=label, user=self.user, **extra)

    def parse(self, rows):
        return parse_bank_csv(upload(rows), self.account).candidates

    def test_user_rule_wins_over_builtin_history_and_investment(self):
        month = Monthly.objects.create(user=self.user, account=self.account, date=date(2026, 9, 1),
                                       total_income=Decimal('0'), total_expense=Decimal('0'))
        Daily.objects.create(user=self.user, account=self.account, month=month, date=date(2026, 9, 2),
                             title='Fred', store='Fred', category='Inne', cost=Decimal('5.00'))
        self.rule('fred', 'Zakupy spozywcze', store_name='Fred Delikatesy')
        self.rule('lidl', 'Chemia', title='Środki czystości')
        self.rule('xtb', 'Oszczędności')
        fred, lidl, xtb, biedronka = self.parse([
            card('FRED SP. Z O.O.  GDYNIA POL 2026-10-03'),
            card('LIDL GRYFA  GDYNIA POL 2026-10-03'),
            [ACCOUNT, '2026-10-05', '2026-10-05', 'PRZELEW WYCHODZĄCY', '', 'X-Trade Brokers XTB',
             'Wpłata', '-500.00', '', '100.00', 'PLN'],
            card('BIEDRONKA 1234  SOPOT POL 2026-10-03'),
        ])
        self.assertEqual((fred.category, fred.store, fred.title), ('Zakupy spozywcze', 'Fred Delikatesy', 'Zakupy spożywcze'))
        self.assertEqual(fred.suggestion_reason, 'Twoja reguła nr 1: fred')
        self.assertTrue(fred.from_user_rule)
        self.assertEqual(fred.rule_key, 'Fred')  # zapamiętanie zawsze po nazwie z wyciągu
        self.assertEqual((lidl.category, lidl.title), ('Chemia', 'Środki czystości'))
        self.assertEqual(xtb.category, 'Oszczędności')
        self.assertEqual(biedronka.category, 'Zakupy spozywcze')  # wbudowana
        self.assertFalse(biedronka.from_user_rule)

    def test_first_matching_rule_wins_and_other_accounts_are_ignored(self):
        other = FinanceAccount.objects.create(name='Inne konto', account_type=FinanceAccount.SHARED)
        self.rule('helios', 'Obce', account=other)
        general = self.rule('kino*', 'Rozrywka')
        specific = self.rule('helios', 'Kultura')
        helios, = self.parse([card('HELIOS KINO  GDYNIA POL 2026-10-03')])
        self.assertEqual(helios.category, 'Rozrywka')
        move_rule(specific, -1)
        helios, = self.parse([card('HELIOS KINO  GDYNIA POL 2026-10-03')])
        self.assertEqual((helios.category, helios.suggestion_reason), ('Kultura', 'Twoja reguła nr 1: helios'))
        self.assertEqual(list(ImportRule.objects.filter(account=self.account).values_list('pk', flat=True)),
                         [specific.pk, general.pk])

    def test_income_rule(self):
        self.rule('firma testowa', 'Premia', kind=ImportRule.INCOME, title='Premia kwartalna')
        salary, = self.parse([income_row()])
        self.assertEqual((salary.source, salary.title), ('Premia', 'Premia kwartalna'))
        self.assertEqual(salary.rule_key, 'Firma Testowa')
        self.assertEqual(salary.default_title, 'Wynagrodzenie 09/2026')

    def test_index_is_empty_without_account(self):
        self.assertFalse(ImportRuleIndex(None))


class RememberTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='zapamietaj', password='pass123')
        self.account = self.user.owned_finance_accounts.get(account_type='personal')
        self.client.force_login(self.user)

    def preview(self, rows):
        return parse_bank_csv(upload(rows), self.account)

    def confirm(self, data):
        return self.client.post('/finance/bank/import/', data, follow=True)

    def test_preview_offers_remember_with_original_store(self):
        response = self.client.post('/finance/bank/import/', {'file': upload([
            card('FRED SP. Z O.O.  GDYNIA POL 2026-10-03'),
        ])})
        self.assertContains(response, 'name="row_0_rule_key" value="Fred"')
        self.assertContains(response, 'name="row_0_remember"')
        self.assertContains(response, 'Zapamiętaj: „Fred” → <span data-bip-remember-label>Inne</span>', html=False)

    def test_remember_creates_rule_used_by_next_import(self):
        preview = self.preview([card('FRED SP. Z O.O.  GDYNIA POL 2026-10-03')])
        data = _post_data_from_preview(preview)
        data.update({'row_0_category': 'Kultura', 'row_0_remember': 'on', 'row_0_rule_key': 'Fred'})
        response = self.confirm(data)
        self.assertContains(response, 'Zapamiętane reguły na kolejne importy: 1.')
        rule = ImportRule.objects.get(account=self.account)
        self.assertEqual((rule.patterns, rule.label, rule.title, rule.store_name, rule.kind, rule.created_by),
                         ('Fred', 'Kultura', '', '', ImportRule.EXPENSE, self.user))

        again, = self.preview([card('FRED SP. Z O.O.  GDYNIA POL 2026-10-12')]).candidates
        self.assertEqual((again.category, again.suggestion_reason), ('Kultura', 'Twoja reguła nr 1: Fred'))

    def test_remember_updates_existing_rule_and_moves_it_to_top(self):
        save_rule(self.account, ImportRule.EXPENSE, patterns='fred', label='Inne')
        save_rule(self.account, ImportRule.EXPENSE, patterns='kino*', label='Rozrywka')
        move_rule(ImportRule.objects.get(label='Rozrywka'), -1)
        preview = self.preview([card('MRCLEANER  GDYNIA POL 2026-10-03'), card('FRED  GDYNIA POL 2026-10-04')])
        data = _post_data_from_preview(preview)
        data.update({
            'row_0_remember': 'on', 'row_0_rule_key': 'Mrcleaner', 'row_0_category': 'Dom',
            'row_0_store': 'Mr Cleaner', 'row_0_title': 'Sprzątanie',
            'row_1_remember': 'on', 'row_1_rule_key': 'Fred', 'row_1_category': 'Zakupy spozywcze',
            'row_1_title': 'Zakupy spożywcze',
        })
        self.confirm(data)
        rules = list(ImportRule.objects.filter(account=self.account).values_list(
            'patterns', 'label', 'title', 'store_name'))
        # Zapamiętane wskakują na górę; „fred” zmieniona, nie zdublowana.
        # Opis „Zakupy spożywcze” jest domyślny dla tej kategorii, więc nie trafia do reguły.
        self.assertEqual(rules, [
            ('fred', 'Zakupy spozywcze', '', ''),
            ('Mrcleaner', 'Dom', 'Sprzątanie', 'Mr Cleaner'),
            ('kino*', 'Rozrywka', '', ''),
        ])

    def test_remember_income(self):
        preview = self.preview([income_row()])
        data = _post_data_from_preview(preview)
        data.update({'row_0_remember': 'on', 'row_0_rule_key': 'Firma Testowa', 'row_0_source': 'Premia',
                     'row_0_default_title': 'Wynagrodzenie 09/2026', 'row_0_title': 'Wynagrodzenie 09/2026'})
        self.confirm(data)
        rule = ImportRule.objects.get(account=self.account)
        self.assertEqual((rule.kind, rule.patterns, rule.label, rule.title),
                         (ImportRule.INCOME, 'Firma Testowa', 'Premia', ''))

    def test_transfer_to_shared_is_not_remembered(self):
        preview = self.preview([card('FRED  GDYNIA POL 2026-10-03')])
        data = _post_data_from_preview(preview)
        data.update({'row_0_remember': 'on', 'row_0_rule_key': 'Fred', 'row_0_category': TRANSFER_TO_SHARED_CATEGORY})
        response = self.confirm(data)
        self.assertContains(response, 'Nie zapamiętano reguły dla „Fred”')
        self.assertFalse(ImportRule.objects.exists())

    def test_remember_skips_short_or_empty_values(self):
        created, updated = remember_rules(self.account, self.user, [
            RememberRequest(kind=ImportRule.EXPENSE, key='AB', label='Inne'),
            RememberRequest(kind=ImportRule.EXPENSE, key='Fred', label=' '),
            RememberRequest(kind='inne', key='Fred', label='Inne'),
            RememberRequest(kind=ImportRule.EXPENSE, key='Sklep, *Fred*', label='Kultura'),
        ])
        self.assertEqual((created, updated), (1, 0))
        self.assertEqual(ImportRule.objects.get().patterns, 'Sklep Fred')


class ImportRulesPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='strona-regul', password='pass123')
        self.account = self.user.owned_finance_accounts.get(account_type='personal')
        self.client.force_login(self.user)

    def test_page_lists_rules_builtin_and_checks_description(self):
        save_rule(self.account, ImportRule.EXPENSE, patterns='fred, malinogród', label='Kultura', title='Fred')
        save_rule(self.account, ImportRule.INCOME, patterns='firma', label='Premia')
        response = self.client.get('/finance/bank/rules/', {'sprawdz': 'FRED  GDYNIA  POL 2026-10-03'})
        self.assertContains(response, 'Twoje reguły <span>1</span>', html=False)
        self.assertContains(response, 'opis „Fred”')
        self.assertContains(response, 'Twoja reguła nr 1, pasuje <code>fred</code>', html=False)
        self.assertContains(response, '<li class="pc-chip is-hit">fred</li>', html=False)
        self.assertContains(response, 'Skopiuj do swoich')
        self.assertContains(response, '>biedronka*<', html=False)
        self.assertContains(response, '>dino<', html=False)

        response = self.client.get('/finance/bank/rules/', {'sprawdz': 'LIDL GRYFA  GDYNIA POL'})
        self.assertContains(response, 'Reguła wbudowana.')
        self.assertContains(response, 'wzor=Lidl')

        response = self.client.get('/finance/bank/rules/', {'rodzaj': 'przychody', 'sprawdz': 'Firma ABC'})
        self.assertContains(response, '→ <strong>Premia</strong>', html=False)

    def test_add_edit_move_and_delete(self):
        response = self.client.post('/finance/bank/rules/add/', {
            'rodzaj': 'wydatki', 'patterns': 'fred', 'label': 'Kultura', 'title': '', 'store_name': '', 'position': '1',
        })
        rule = ImportRule.objects.get()
        self.assertRedirects(response, f'/finance/bank/rules/#regula-{rule.pk}', fetch_redirect_response=False)
        self.assertEqual(rule.created_by, self.user)
        self.client.post('/finance/bank/rules/add/', {
            'rodzaj': 'wydatki', 'patterns': 'kino*', 'label': 'Rozrywka', 'position': '1',
        })
        self.assertEqual(list(ImportRule.objects.values_list('patterns', flat=True)), ['kino*', 'fred'])

        self.client.post(f'/finance/bank/rules/{rule.pk}/move/', {'direction': 'up'})
        self.assertEqual(list(ImportRule.objects.values_list('patterns', flat=True)), ['fred', 'kino*'])

        response = self.client.post(f'/finance/bank/rules/{rule.pk}/', {
            'rodzaj': 'wydatki', 'patterns': 'fred, frede*', 'label': 'Kultura', 'title': 'Kino',
            'store_name': 'Fred', 'position': '2',
        })
        rule.refresh_from_db()
        self.assertEqual((rule.patterns, rule.title, rule.store_name), ('fred\nfrede*', 'Kino', 'Fred'))
        self.assertEqual(list(ImportRule.objects.values_list('patterns', flat=True)), ['kino*', 'fred\nfrede*'])

        response = self.client.post(f'/finance/bank/rules/{rule.pk}/delete/', follow=True)
        self.assertContains(response, 'Usunięto regułę „Kultura”.')
        self.assertFalse(ImportRule.objects.filter(pk=rule.pk).exists())

    def test_form_errors_and_prefill(self):
        response = self.client.post('/finance/bank/rules/add/', {
            'rodzaj': 'wydatki', 'patterns': 'ab', 'label': 'Kultura', 'position': '1',
        })
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'za krótkie', status_code=400)
        response = self.client.post('/finance/bank/rules/add/', {
            'rodzaj': 'wydatki', 'patterns': 'fred', 'label': TRANSFER_TO_SHARED_CATEGORY, 'position': '1',
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ImportRule.objects.exists())

        response = self.client.get('/finance/bank/rules/add/', {'rodzaj': 'wydatki', 'wbudowana': '1'})
        self.assertContains(response, 'biedronka*, lidl*')
        self.assertContains(response, 'value="Zakupy spozywcze"')
        response = self.client.get('/finance/bank/rules/add/', {'rodzaj': 'przychody', 'wzor': 'Firma'})
        self.assertContains(response, '>Firma</textarea>', html=False)
        self.assertNotContains(response, 'name="store_name"')

    def test_rules_of_other_account_are_not_reachable(self):
        other = FinanceAccount.objects.create(name='Cudze', account_type=FinanceAccount.SHARED)
        rule = save_rule(other, ImportRule.EXPENSE, patterns='fred', label='Kultura')
        self.assertEqual(self.client.get(f'/finance/bank/rules/{rule.pk}/').status_code, 404)
        self.assertEqual(self.client.post(f'/finance/bank/rules/{rule.pk}/delete/').status_code, 404)
        self.assertEqual(self.client.post(f'/finance/bank/rules/{rule.pk}/move/').status_code, 404)
        self.assertTrue(ImportRule.objects.filter(pk=rule.pk).exists())

    def test_import_page_links_to_rules(self):
        self.assertContains(self.client.get('/finance/bank/import/'), 'href="/finance/bank/rules/"')
