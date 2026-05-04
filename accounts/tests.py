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
