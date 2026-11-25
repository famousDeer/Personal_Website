from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cooking.models import PushSubscription, SentNotification

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
        self.today = timezone.localdate()

    def test_all_routes_require_login(self):
        self.client.logout()
        self.assertRedirects(self.client.get(self.index), f'{reverse("login")}?next={self.index}')
        for name in ('add', 'edit', 'status', 'delete'):
            path = reverse(f'chores:{name}', args=[] if name == 'add' else [1])
            with self.subTest(name=name):
                self.assertRedirects(self.client.post(path), f'{reverse("login")}?next={path}')

    def test_create_and_edit_shared_task_with_multiple_assignees(self):
        self.assertNotContains(self.client.get(self.add), self.inactive.username)
        response = self.client.post(self.add, {
            'title': 'Wynieść szkło', 'notes': 'Pojemnik za bramą',
            'due_on': self.today.isoformat(), 'assignees': [self.anna.pk, self.bartek.pk],
        })
        self.assertRedirects(response, self.index)
        chore = Chore.objects.get()
        self.assertEqual(chore.due_on, self.today)
        self.assertCountEqual(chore.assignees.all(), [self.anna, self.bartek])

        self.client.force_login(self.bartek)
        self.assertContains(self.client.get(f'{self.index}?widok=moje'), chore.title)
        edit = reverse('chores:edit', args=[chore.pk])
        self.assertContains(self.client.get(edit), self.today.isoformat())
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
        chore = Chore.objects.create(title='Podlać kwiaty', due_on=self.today)
        chore.assignees.add(self.anna, self.bartek)
        later = Chore.objects.create(title='Umyć okna', due_on=self.today + timedelta(days=1))
        later.assignees.add(self.anna)
        other = Chore.objects.create(title='Odebrać paczkę', due_on=self.today)
        other.assignees.add(self.bartek)

        home = reverse('index')
        self.assertContains(self.client.get(home), '1 zadanie na dziś lub po terminie')
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
    def test_cron_targets_active_assignees_and_deduplicates_daily_summary(self):
        today = timezone.localdate()
        phones = {}
        for name in ('anna', 'bartek', 'celina', 'darek'):
            user = User.objects.create_user(username=name, is_active=name != 'darek')
            phones[name] = PushSubscription.objects.create(
                user=user, endpoint=f'https://przyklad.test/{name}', p256dh='klucz', auth='sekret',
            )
        second_phone = PushSubscription.objects.create(
            user=phones['anna'].user, endpoint='https://przyklad.test/anna-2', p256dh='klucz', auth='sekret',
        )
        due = Chore.objects.create(title='Wyrzucić śmieci', due_on=today)
        due.assignees.add(*(phones[name].user for name in ('anna', 'bartek', 'darek')))
        overdue = Chore.objects.create(title='Umyć podłogę', due_on=today - timedelta(days=1))
        overdue.assignees.add(phones['anna'].user)
        for fields in ({}, {'due_on': today + timedelta(days=1)}, {'due_on': today, 'completed_at': timezone.now()}):
            chore = Chore.objects.create(title='Bez przypomnienia', **fields)
            chore.assignees.add(phones['celina'].user)

        with patch('cooking.services.push.send_to_subscription', return_value=True) as send:
            output = StringIO()
            call_command('send_reminders', '--kind', 'zadania', '--dry-run', stdout=output)
            send.assert_not_called()
            self.assertIn('2 zadania', output.getvalue())
            self.assertFalse(SentNotification.objects.exists())

            # Domyślna komenda uruchamiana z crona uwzględnia także zadania.
            call_command('send_reminders', stdout=StringIO())
            calls = {subscription.pk: payload for (subscription, payload), _ in send.call_args_list
                     if payload['tag'] == 'zadania'}
            self.assertEqual(set(calls), {phones['anna'].pk, phones['bartek'].pk, second_phone.pk})
            self.assertIn('2 zadania', calls[phones['anna'].pk]['body'])
            self.assertIn('1 zadanie', calls[phones['bartek'].pk]['body'])
            self.assertEqual(calls[phones['anna'].pk]['url'], f'{reverse("chores:index")}?widok=moje')
            send.reset_mock()

            call_command('send_reminders', '--kind', 'zadania', stdout=StringIO())
            send.assert_not_called()

            due.completed_at = timezone.now()
            due.save(update_fields=['completed_at'])
            with patch('chores.reminders.timezone.localdate', return_value=today + timedelta(days=1)):
                call_command('send_reminders', '--kind', 'zadania', stdout=StringIO())
            self.assertCountEqual(
                [call.args[0].pk for call in send.call_args_list],
                [phones['anna'].pk, second_phone.pk, phones['celina'].pk],
            )
