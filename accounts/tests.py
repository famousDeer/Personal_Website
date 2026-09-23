from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from finance.models import FinanceAccount

User = get_user_model()


class SharedAccountEditTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pass123', email='owner@example.com')
        self.partner = User.objects.create_user(username='partner', password='pass123', email='partner@example.com')
        self.other = User.objects.create_user(username='other', password='pass123', email='other@example.com')
        self.shared_account = FinanceAccount.objects.create(
            name='Domowy budzet',
            account_type=FinanceAccount.SHARED,
            owner=self.user,
        )
        self.shared_account.members.add(self.user, self.partner)

    def test_member_can_edit_shared_account_name(self):
        self.client.login(username='owner', password='pass123')

        response = self.client.post(
            reverse('edit_shared_account', args=[self.shared_account.id]),
            {'name': 'Wspolne rachunki', 'partner_username': 'partner'},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.shared_account.refresh_from_db()
        self.assertEqual(self.shared_account.name, 'Wspolne rachunki')

    def test_owner_can_replace_second_member(self):
        self.client.login(username='owner', password='pass123')

        response = self.client.post(
            reverse('edit_shared_account', args=[self.shared_account.id]),
            {'name': 'Domowy budzet', 'partner_username': 'other'},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.shared_account.refresh_from_db()
        self.assertEqual(
            set(self.shared_account.members.order_by('username').values_list('username', flat=True)),
            {'owner', 'other'},
        )

    def test_non_owner_member_cannot_replace_second_member(self):
        self.client.login(username='partner', password='pass123')

        response = self.client.post(
            reverse('edit_shared_account', args=[self.shared_account.id]),
            {'name': 'Nowa nazwa'},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.shared_account.refresh_from_db()
        self.assertEqual(
            set(self.shared_account.members.order_by('username').values_list('username', flat=True)),
            {'owner', 'partner'},
        )
        self.assertEqual(self.shared_account.name, 'Nowa nazwa')

    def test_non_member_cannot_edit_shared_account(self):
        self.client.login(username='other', password='pass123')

        response = self.client.get(reverse('edit_shared_account', args=[self.shared_account.id]))

        self.assertEqual(response.status_code, 404)

    def test_owner_can_delete_shared_account(self):
        self.client.login(username='owner', password='pass123')

        response = self.client.post(
            reverse('delete_shared_account', args=[self.shared_account.id]),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(FinanceAccount.objects.filter(id=self.shared_account.id).exists())

    def test_non_owner_member_cannot_delete_shared_account(self):
        self.client.login(username='partner', password='pass123')

        response = self.client.post(
            reverse('delete_shared_account', args=[self.shared_account.id]),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(FinanceAccount.objects.filter(id=self.shared_account.id).exists())


class UiTemplateTagTests(TestCase):
    """Wspólny nagłówek strony i polska odmiana liczebników."""

    def render(self, source, **context):
        from django.template import Context, Template
        return Template('{% load ui %}' + source).render(Context(context))

    def test_page_head_renders_title_eyebrow_back_link_and_actions(self):
        html = self.render(
            '{% page_head title="Nowy wydatek" eyebrow="Finanse" icon="bi-wallet2" back="/finanse/" %}'
            '<a class="btn btn-primary" href="/x/">Zapisz</a>{% endpage_head %}'
        )
        self.assertIn('<h1 class="u-page-title">Nowy wydatek</h1>', html)
        self.assertIn('bi-wallet2', html)
        self.assertIn('href="/finanse/"', html)
        self.assertIn('<div class="u-page-actions"><a class="btn btn-primary" href="/x/">Zapisz</a></div>', html)

    def test_page_head_without_actions_has_no_empty_actions_box(self):
        html = self.render('{% page_head title="Konto" %}{% endpage_head %}')
        self.assertNotIn('u-page-actions', html)
        self.assertNotIn('u-back', html)

    def test_page_head_escapes_title(self):
        html = self.render('{% page_head title=name %}{% endpage_head %}', name='<b>x</b>')
        self.assertIn('&lt;b&gt;x&lt;/b&gt;', html)

    def test_polish_plural_forms(self):
        forms = 'rachunek,rachunki,rachunków'
        cases = {0: '0 rachunków', 1: '1 rachunek', 2: '2 rachunki', 4: '4 rachunki', 5: '5 rachunków',
                 12: '12 rachunków', 14: '14 rachunków', 22: '22 rachunki', 25: '25 rachunków', 104: '104 rachunki'}
        for count, expected in cases.items():
            with self.subTest(count=count):
                self.assertEqual(self.render('{{ n|pl:f }}', n=count, f=forms), expected)

    def test_polish_plural_word_only(self):
        self.assertEqual(self.render('{{ n|pl_word:"waluta,waluty,walut" }}', n=1), 'waluta')
        self.assertEqual(self.render('{{ n|pl_word:"waluta,waluty,walut" }}', n=3), 'waluty')
        self.assertEqual(self.render('{{ n|pl_word:"waluta,waluty,walut" }}', n=7), 'walut')
