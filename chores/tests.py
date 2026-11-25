from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from cooking.models import PushSubscription

from .models import Chore

User = get_user_model()


class ChoreViewsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.anna = User.objects.create_user(username='anna')
        cls.bartek = User.objects.create_user(username='bartek')
        cls.inactive = User.objects.create_user(username='nieaktywny', is_active=False)

    def setUp(self):
        self.client.force_login(self.anna)
        self.index = reverse('chores:index')
        self.add = reverse('chores:add')

    def test_all_routes_require_login(self):
        self.client.logout()
        self.assertRedirects(self.client.get(self.index), f'{reverse("login")}?next={self.index}')
        for name in ('add', 'edit', 'status', 'delete'):
            path = reverse(f'chores:{name}', args=[] if name == 'add' else [1])
            with self.subTest(name=name):
                self.assertRedirects(self.client.post(path), f'{reverse("login")}?next={path}')

    def test_create_and_edit_shared_task_with_multiple_assignees(self):
        add_form = self.client.get(self.add)
        self.assertNotContains(add_form, self.inactive.username)
        self.assertNotIn('due_on', add_form.context['form'].fields)
        response = self.client.post(self.add, {
            'title': 'Wynieść szkło', 'notes': 'Pojemnik za bramą',
            'assignees': [self.anna.pk, self.bartek.pk],
        })
        self.assertRedirects(response, self.index)
        chore = Chore.objects.get()
        self.assertCountEqual(chore.assignees.all(), [self.anna, self.bartek])

        self.client.force_login(self.bartek)
        self.assertContains(self.client.get(f'{self.index}?widok=moje'), chore.title)
        edit = reverse('chores:edit', args=[chore.pk])
        self.assertNotIn('due_on', self.client.get(edit).context['form'].fields)
        self.assertRedirects(self.client.post(edit, {
            'title': 'Wynieść szkło i papier', 'assignees': [self.anna.pk],
        }), self.index)
        self.assertNotContains(self.client.get(f'{self.index}?widok=moje'), 'Wynieść szkło i papier')
        self.assertContains(self.client.get(self.index), 'Wynieść szkło i papier')

    def test_rejects_missing_or_inactive_assignees(self):
        for assignees in ([], [self.inactive.pk]):
            with self.subTest(assignees=assignees):
                response = self.client.post(self.add, {'title': 'Odkurzyć', 'assignees': assignees})
                self.assertIn('assignees', response.context['form'].errors)
        self.assertFalse(Chore.objects.exists())

    def test_status_and_home_reminder_are_shared(self):
        chore = Chore.objects.create(title='Podlać kwiaty')
        chore.assignees.add(self.anna, self.bartek)
        other = Chore.objects.create(title='Odebrać paczkę')
        other.assignees.add(self.bartek)

        home = reverse('index')
        self.assertContains(self.client.get(home), '1 zadanie do zrobienia')
        status = reverse('chores:status', args=[chore.pk])
        self.assertEqual(self.client.get(status).status_code, 405)
        self.assertRedirects(self.client.post(status, {
            'stan': 'wykonane', 'widok': 'moje',
        }), f'{self.index}?widok=moje')
        self.assertNotContains(self.client.get(home), 'Moje zadania')

        self.client.force_login(self.bartek)
        self.assertNotContains(self.client.get(self.index), chore.title)
        self.assertContains(self.client.get(f'{self.index}?widok=wykonane'), chore.title)
        self.assertRedirects(self.client.post(status, {'stan': 'otwarte'}), self.index)
        self.assertContains(self.client.get(self.index), chore.title)

    def test_undo_deletion_restores_assignees(self):
        chore = Chore.objects.create(title='Umówić odbiór gabarytów')
        chore.assignees.add(self.anna, self.bartek)
        delete = reverse('chores:delete', args=[chore.pk])
        self.assertEqual(self.client.get(delete).status_code, 405)
        self.assertRedirects(self.client.post(delete), self.index)
        self.assertFalse(Chore.objects.exists())

        token = self.client.session['undo_bin'][-1]['token']
        self.client.post(reverse('undo', args=[token]), {'next': self.index})
        self.assertCountEqual(Chore.objects.get(pk=chore.pk).assignees.all(), [self.anna, self.bartek])


@override_settings(VAPID_PUBLIC_KEY='publiczny', VAPID_PRIVATE_KEY='prywatny')
class ChoreReminderTests(TestCase):
    def test_notifies_assignees_only_after_creating_the_task(self):
        anna = User.objects.create_user(username='anna')
        bartek = User.objects.create_user(username='bartek')
        celina = User.objects.create_user(username='celina')
        phones = [
            PushSubscription.objects.create(
                user=user, endpoint=f'https://przyklad.test/{device}', p256dh='klucz', auth='sekret',
            )
            for user, device in ((anna, 'a1'), (anna, 'a2'), (bartek, 'b'), (celina, 'c'))
        ]
        self.client.force_login(celina)
        add = reverse('chores:add')

        with patch('cooking.services.push.send_to_subscription', return_value=True) as send:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(add, {
                    'title': 'Wyrzucić śmieci', 'assignees': [anna.pk, bartek.pk],
                })
                self.assertEqual(response.status_code, 302)
                send.assert_not_called()

            chore = Chore.objects.get()
            calls = {subscription.pk: payload for (subscription, payload), _ in send.call_args_list}
            self.assertEqual(set(calls), {phone.pk for phone in phones[:3]})
            self.assertEqual(send.call_count, 3)
            for payload in calls.values():
                self.assertEqual(payload['title'], 'Nowe zadanie domowe')
                self.assertEqual(payload['body'], chore.title)
                self.assertEqual(payload['url'], f'{reverse("chores:index")}?widok=moje#zadanie-{chore.pk}')
                self.assertNotIn('app_badge', payload)

            send.reset_mock()
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(add, {'title': 'Bez przypisania'})
                self.client.post(reverse('chores:edit', args=[chore.pk]), {
                    'title': 'Wyrzucić szkło', 'assignees': [celina.pk],
                })
            send.assert_not_called()
