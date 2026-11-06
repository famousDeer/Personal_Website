import datetime
import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from habits.models import DailyHealth, Workout

from . import services
from .models import Category, Entry, TimelogSettings


def at(day, hour, minute=0):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute)))


DAY = datetime.date(2026, 10, 20)


class TimelogTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('ala', password='x')
        services.ensure_categories(self.user)
        self.work = Category.objects.get(user=self.user, name='Praca')
        self.phone = Category.objects.get(user=self.user, name='Telefon i social media')
        self.sleep = Category.objects.get(user=self.user, auto=Category.SLEEP)
        self.sport = Category.objects.get(user=self.user, auto=Category.SPORT)

    def freeze(self, moment):
        patcher = mock.patch('timelog.services.now', return_value=moment)
        patcher.start()
        self.addCleanup(patcher.stop)


class TimerTests(TimelogTestCase):
    def test_switch_closes_previous_entry(self):
        services.start_timer(self.user, self.work, at=at(DAY, 9))
        services.start_timer(self.user, self.phone, at=at(DAY, 11))
        work, phone = Entry.objects.order_by('start')
        self.assertEqual(work.end, at(DAY, 11))
        self.assertIsNone(phone.end)

    def test_same_category_keeps_running_entry(self):
        first = services.start_timer(self.user, self.work, at=at(DAY, 9))
        again = services.start_timer(self.user, self.work, at=at(DAY, 10))
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(Entry.objects.count(), 1)

    def test_quick_switch_corrects_category(self):
        study = Category.objects.get(user=self.user, name='Nauka')
        services.start_timer(self.user, self.work, at=at(DAY, 9))
        services.start_timer(self.user, study, at=at(DAY, 9) + datetime.timedelta(seconds=20))
        entry = Entry.objects.get()
        self.assertEqual(entry.category, study)
        self.assertEqual(entry.start, at(DAY, 9))

    def test_stop(self):
        services.start_timer(self.user, self.work, at=at(DAY, 9))
        stopped = services.stop_timer(self.user, at=at(DAY, 12))
        self.assertEqual(stopped.end, at(DAY, 12))
        self.assertIsNone(services.running_entry(self.user))


class ResumeTests(TimelogTestCase):
    """Chwilowa kategoria (Instagram w trakcie Pracy) wraca do przerwanej czynności."""

    def test_default_phone_category_is_interruption(self):
        self.assertTrue(self.phone.is_interruption)
        self.assertFalse(self.work.is_interruption)

    def test_stop_with_resume_starts_interrupted_category_again(self):
        services.start_timer(self.user, self.work, at=at(DAY, 9), note='raport')
        phone = services.start_timer(self.user, self.phone, at=at(DAY, 10))
        self.assertEqual(phone.interrupted.category, self.work)
        stopped = services.stop_timer(self.user, at=at(DAY, 10, 15), resume=True)
        self.assertEqual(stopped.end, at(DAY, 10, 15))
        running = services.running_entry(self.user)
        self.assertEqual((running.category, running.start, running.note), (self.work, at(DAY, 10, 15), 'raport'))
        self.assertEqual(stopped.resumed, running)

    def test_plain_stop_and_ordinary_category_do_not_resume(self):
        services.start_timer(self.user, self.work, at=at(DAY, 9))
        services.start_timer(self.user, self.phone, at=at(DAY, 10))
        services.stop_timer(self.user, at=at(DAY, 10, 15))
        self.assertIsNone(services.running_entry(self.user))
        study = Category.objects.get(user=self.user, name='Nauka')
        services.start_timer(self.user, self.work, at=at(DAY, 11))
        services.start_timer(self.user, study, at=at(DAY, 12))
        self.assertIsNone(services.stop_timer(self.user, at=at(DAY, 13), resume=True).resumed)
        self.assertIsNone(services.running_entry(self.user))

    def test_glance_shorter_than_minute_disappears(self):
        services.start_timer(self.user, self.work, at=at(DAY, 9))
        services.start_timer(self.user, self.phone, at=at(DAY, 10))
        services.stop_timer(self.user, at=at(DAY, 10) + datetime.timedelta(seconds=30), resume=True)
        work = Entry.objects.get()
        self.assertEqual((work.category, work.start, work.end), (self.work, at(DAY, 9), None))

    def test_interruption_right_after_start_keeps_work(self):
        services.start_timer(self.user, self.work, at=at(DAY, 9))
        services.start_timer(self.user, self.phone, at=at(DAY, 9) + datetime.timedelta(seconds=20))
        self.assertEqual(Entry.objects.count(), 2)
        services.stop_timer(self.user, at=at(DAY, 9, 5), resume=True)
        self.assertEqual(services.running_entry(self.user).category, self.work)

    def test_nested_interruptions_unwind_back_to_work(self):
        youtube = Category.objects.create(user=self.user, name='YouTube', is_interruption=True)
        services.start_timer(self.user, self.work, at=at(DAY, 9))
        services.start_timer(self.user, youtube, at=at(DAY, 10))
        services.start_timer(self.user, self.phone, at=at(DAY, 10, 10))
        services.stop_timer(self.user, at=at(DAY, 10, 20), resume=True)
        self.assertEqual(services.running_entry(self.user).category, youtube)
        services.stop_timer(self.user, at=at(DAY, 10, 30), resume=True)
        self.assertEqual(services.running_entry(self.user).category, self.work)

    def test_no_resume_after_long_interruption_or_to_hidden_category(self):
        services.start_timer(self.user, self.work, at=at(DAY, 9))
        services.start_timer(self.user, self.phone, at=at(DAY, 10))
        self.assertIsNone(services.stop_timer(self.user, at=at(DAY, 13, 30), resume=True).resumed)
        services.start_timer(self.user, self.work, at=at(DAY, 14))
        services.start_timer(self.user, self.phone, at=at(DAY, 15))
        Category.objects.filter(pk=self.work.pk).update(is_archived=True)
        self.assertIsNone(services.stop_timer(self.user, at=at(DAY, 15, 10), resume=True).resumed)

    def test_day_page_offers_return_and_category_flag_saves(self):
        self.client.force_login(self.user)
        services.start_timer(self.user, self.work, at=timezone.now() - datetime.timedelta(minutes=30))
        services.start_timer(self.user, self.phone, at=timezone.now() - datetime.timedelta(minutes=5))
        self.assertContains(self.client.get(reverse('timelog:index')), 'Wróć do: Praca')
        response = self.client.post(reverse('timelog:stop'), {'resume': '1'}, follow=True)
        self.assertContains(response, 'Wracasz do: Praca')
        self.assertEqual(services.running_entry(self.user).category, self.work)
        self.client.post(reverse('timelog:categories'), {
            'action': 'save', 'id': self.work.pk, 'name': 'Praca', 'value': 'good', 'icon': 'bi-briefcase',
            'color': 1, 'is_interruption': '1',
        })
        self.work.refresh_from_db()
        self.assertTrue(self.work.is_interruption)


class FlattenTests(TimelogTestCase):
    def test_later_entry_wins_overlap(self):
        work = Entry.objects.create(user=self.user, category=self.work, start=at(DAY, 9), end=at(DAY, 17))
        gym = Entry.objects.create(user=self.user, category=self.sport, start=at(DAY, 12), end=at(DAY, 13))
        lo, hi = services.day_bounds(DAY)
        segments = services.flatten([work, gym], lo, hi, until=at(DAY, 23))
        self.assertEqual(
            [(s.hour, e.hour, entry.category.name) for s, e, entry in
             [(timezone.localtime(a), timezone.localtime(b), c) for a, b, c in segments]],
            [(9, 12, 'Praca'), (12, 13, 'Sport'), (13, 17, 'Praca')],
        )
        self.assertEqual(services.gaps(segments, lo, at(DAY, 18)), [(lo, at(DAY, 9)), (at(DAY, 17), at(DAY, 18))])

    def test_day_data_counts_overnight_entry_once_per_day(self):
        self.freeze(at(DAY, 12))
        Entry.objects.create(user=self.user, category=self.phone, start=at(DAY - datetime.timedelta(days=1), 23), end=at(DAY, 1))
        data = services.day_data(self.user, DAY)
        self.assertEqual(data['tracked'], 3600)
        self.assertEqual(data['untracked'], 11 * 3600)

    def test_period_summary_values(self):
        self.freeze(at(DAY + datetime.timedelta(days=1), 12))
        Entry.objects.create(user=self.user, category=self.phone, start=at(DAY, 20), end=at(DAY, 22))
        Entry.objects.create(user=self.user, category=self.sleep, start=at(DAY, 23), end=at(DAY + datetime.timedelta(days=1), 7))
        summary = services.period_summary(self.user, DAY, DAY + datetime.timedelta(days=6))
        self.assertEqual(summary['elapsed_days'], 2)
        self.assertEqual(summary['by_value'][Category.WASTE], 2 * 3600)
        self.assertEqual(summary['awake'], 2 * 3600)
        self.assertEqual(summary['totals'][self.sleep], 8 * 3600)


class ImportTests(TimelogTestCase):
    def sleep_row(self, day, start, end):
        ms = lambda moment: int(moment.timestamp() * 1000)  # noqa: E731
        return DailyHealth.objects.create(user=self.user, date=day, sleep_s=int((end - start).total_seconds()), raw={
            'sleep': {'dailySleepDTO': {'sleepStartTimestampGMT': ms(start), 'sleepEndTimestampGMT': ms(end)}},
        })

    def test_sleep_and_workout_become_entries_once(self):
        self.sleep_row(DAY, at(DAY - datetime.timedelta(days=1), 23, 30), at(DAY, 7))
        Workout.objects.create(user=self.user, kind=Workout.RUN, date=DAY, start_time=datetime.time(18), duration_s=3000, title='Bieg')
        services.import_automatic(self.user, DAY, DAY)
        services.import_automatic(self.user, DAY, DAY)
        sleep = Entry.objects.get(category=self.sleep)
        run = Entry.objects.get(category=self.sport)
        self.assertEqual(sleep.start, at(DAY - datetime.timedelta(days=1), 23, 30))
        self.assertEqual(sleep.source, Entry.GARMIN)
        self.assertEqual((run.start, run.end, run.note), (at(DAY, 18), at(DAY, 18, 50), 'Bieg'))

    def test_sleep_trims_forgotten_timer(self):
        services.start_timer(self.user, self.phone, at=at(DAY - datetime.timedelta(days=1), 22))
        self.sleep_row(DAY, at(DAY - datetime.timedelta(days=1), 23, 30), at(DAY, 7))
        services.import_automatic(self.user, DAY, DAY)
        phone = Entry.objects.get(category=self.phone)
        self.assertEqual(phone.end, at(DAY - datetime.timedelta(days=1), 23, 30))

    def test_edited_and_deleted_entries_stay_as_user_left_them(self):
        workout = Workout.objects.create(user=self.user, kind=Workout.GYM, date=DAY, start_time=datetime.time(7), duration_s=3600)
        self.sleep_row(DAY, at(DAY - datetime.timedelta(days=1), 23), at(DAY, 6))
        services.import_automatic(self.user, DAY, DAY)
        self.client.force_login(self.user)
        sleep = Entry.objects.get(category=self.sleep)
        self.client.post(reverse('timelog:entry-delete', args=[sleep.pk]))
        gym = Entry.objects.get(category=self.sport)
        self.client.post(reverse('timelog:entry', args=[gym.pk]), {
            'category': self.sport.pk, 'start_date': '2026-10-20', 'start_time': '07:15',
            'end_date': '2026-10-20', 'end_time': '08:00',
        })
        workout.duration_s = 100
        workout.save()
        services.import_automatic(self.user, DAY, DAY)
        self.assertFalse(Entry.objects.filter(category=self.sleep).exists())
        gym.refresh_from_db()
        self.assertEqual((gym.start, gym.source), (at(DAY, 7, 15), Entry.MANUAL))

    def test_deleted_workout_removes_entry(self):
        workout = Workout.objects.create(user=self.user, kind=Workout.RUN, date=DAY, start_time=datetime.time(7), duration_s=600)
        services.import_automatic(self.user, DAY, DAY)
        workout.is_deleted = True
        workout.save()
        services.import_automatic(self.user, DAY, DAY)
        self.assertFalse(Entry.objects.exists())


class ViewTests(TimelogTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_day_page_start_and_stop(self):
        response = self.client.get(reverse('timelog:index'))
        self.assertContains(response, 'Telefon i social media')
        self.client.post(reverse('timelog:start'), {'category': self.work.pk})
        self.assertContains(self.client.get(reverse('timelog:index')), 'data-tl-since')
        self.client.post(reverse('timelog:stop'))
        self.assertIsNone(services.running_entry(self.user))

    def test_fill_gap_across_midnight(self):
        self.client.post(reverse('timelog:entry-add'), {
            'category': self.phone.pk, 'date': '2026-10-20', 'start': '23:00', 'end': '00:30',
        })
        entry = Entry.objects.get()
        self.assertEqual((entry.start, entry.end), (at(DAY, 23), at(DAY + datetime.timedelta(days=1), 0, 30)))
        response = self.client.get(reverse('timelog:day', args=['2026-10-20']))
        self.assertContains(response, 'bez wpisu')

    def test_summary_and_other_pages_render(self):
        Entry.objects.create(user=self.user, category=self.phone, start=timezone.now() - datetime.timedelta(hours=2),
                             end=timezone.now() - datetime.timedelta(hours=1))
        self.assertContains(self.client.get(reverse('timelog:summary')), 'Strata czasu')
        self.assertContains(self.client.get(reverse('timelog:summary') + '?okres=miesiac'), 'Strata czasu')
        self.assertEqual(self.client.get(reverse('timelog:categories')).status_code, 200)
        self.assertEqual(self.client.get(reverse('timelog:automation')).status_code, 200)

    def test_automation_page_uses_remote_address_for_shortcuts(self):
        TimelogSettings.objects.update_or_create(user=self.user, defaults={'api_token': 'k'})
        response = self.client.get(reverse('timelog:automation'))
        self.assertContains(response, 'http://testserver/czas/api/start/')
        self.assertContains(response, 'tylko w domowym Wi-Fi')
        with self.settings(REMOTE_BASE_URL='https://pi.tail1234.ts.net'):
            response = self.client.get(reverse('timelog:automation'))
        self.assertContains(response, 'https://pi.tail1234.ts.net/czas/api/stop/')
        self.assertNotContains(response, 'tylko w domowym Wi-Fi')

    def test_category_with_entries_is_hidden_not_deleted(self):
        Entry.objects.create(user=self.user, category=self.phone, start=at(DAY, 9), end=at(DAY, 10))
        self.client.post(reverse('timelog:categories'), {'action': 'delete', 'id': self.phone.pk})
        self.phone.refresh_from_db()
        self.assertTrue(self.phone.is_archived)

    def test_category_icon_from_picker_and_unknown_icon(self):
        self.client.post(reverse('timelog:categories'), {'action': 'add', 'name': 'Spacer z psem', 'icon': 'tl-icon-dog', 'color': 3})
        self.assertEqual(Category.objects.get(user=self.user, name='Spacer z psem').icon, 'tl-icon-dog')
        self.client.post(reverse('timelog:categories'), {'action': 'add', 'name': 'Inne', 'icon': 'bi-x" onclick="'})
        self.assertEqual(Category.objects.get(user=self.user, name='Inne').icon, 'bi-circle')
        self.assertContains(self.client.get(reverse('timelog:categories')), 'aria-label="Pies"')

    def test_other_users_entry_is_not_reachable(self):
        other = User.objects.create_user('ola', password='x')
        services.ensure_categories(other)
        entry = Entry.objects.create(user=other, category=other.time_categories.first(), start=at(DAY, 9), end=at(DAY, 10))
        self.assertEqual(self.client.get(reverse('timelog:entry', args=[entry.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('timelog:entry-delete', args=[entry.pk])).status_code, 404)


class ApiTests(TimelogTestCase):
    def setUp(self):
        super().setUp()
        settings = TimelogSettings.for_user(self.user)
        settings.api_token = 'sekret-123'
        settings.save()
        self.auth = {'HTTP_AUTHORIZATION': 'Bearer sekret-123'}

    def test_requires_token(self):
        self.assertEqual(self.client.post(reverse('timelog:api-start'), {'category': 'praca'}).status_code, 401)
        bad = {'HTTP_AUTHORIZATION': 'Bearer zly'}
        self.assertEqual(self.client.post(reverse('timelog:api-start'), {'category': 'praca'}, **bad).status_code, 401)

    def test_start_by_name_without_polish_letters_and_stop_only_matching(self):
        response = self.client.post(reverse('timelog:api-start'), json.dumps({'category': 'telefon i SOCIAL media'}),
                                    content_type='application/json', **self.auth)
        self.assertEqual(response.json()['running']['category'], 'Telefon i social media')
        self.assertEqual(Entry.objects.get().source, Entry.SHORTCUT)
        response = self.client.post(reverse('timelog:api-stop'), {'category': 'Praca'}, **self.auth)
        self.assertIsNotNone(response.json()['running'])
        response = self.client.post(reverse('timelog:api-stop'), {'category': 'Telefon i social media'}, **self.auth)
        self.assertIsNone(response.json()['running'])

    def test_stop_interruption_resumes_unless_disabled(self):
        services.start_timer(self.user, self.work, at=timezone.now() - datetime.timedelta(minutes=30))
        self.client.post(reverse('timelog:api-start'), {'category': 'telefon i social media'}, **self.auth)
        Entry.objects.filter(category=self.phone).update(start=timezone.now() - datetime.timedelta(minutes=5))
        data = self.client.post(reverse('timelog:api-stop'), {'category': 'telefon i social media'}, **self.auth).json()
        self.assertEqual((data['stopped'], data['resumed'], data['running']['category']),
                         ('Telefon i social media', 'Praca', 'Praca'))
        self.client.post(reverse('timelog:api-start'), {'category': 'telefon i social media'}, **self.auth)
        Entry.objects.filter(category=self.phone, end__isnull=True).update(start=timezone.now() - datetime.timedelta(minutes=5))
        data = self.client.post(reverse('timelog:api-stop'), json.dumps({'resume': False}),
                                content_type='application/json', **self.auth).json()
        self.assertIsNone(data['resumed'])
        self.assertIsNone(data['running'])

    def test_unknown_category(self):
        response = self.client.post(reverse('timelog:api-start'), {'category': 'Nic'}, **self.auth)
        self.assertEqual(response.status_code, 400)
        self.assertIn('Praca', response.json()['error'])


class BubbleTests(TimelogTestCase):
    """Pływający dymek stopera: stan w JSON, start i stop z CSRF jak w przeglądarce."""

    def test_needs_login(self):
        self.assertEqual(self.client.get(reverse('timelog:bubble')).status_code, 401)

    def test_start_switch_and_stop_with_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        state = client.get(reverse('timelog:bubble')).json()
        self.assertIsNone(state['running'])
        self.assertIn('Praca', [c['name'] for c in state['categories']])
        url = reverse('timelog:bubble')
        headers = {'HTTP_X_CSRFTOKEN': state['csrfToken']}
        self.assertEqual(client.post(url, json.dumps({'action': 'stop'}), content_type='application/json').status_code, 403)
        state = client.post(url, json.dumps({'action': 'start', 'category': self.work.pk}),
                            content_type='application/json', **headers).json()
        self.assertEqual((state['running']['name'], state['running']['color']), ('Praca', self.work.color))
        self.assertEqual(Entry.objects.get().source, Entry.TIMER)
        response = client.post(url, json.dumps({'action': 'start', 'category': 'x'}), content_type='application/json', **headers)
        self.assertEqual(response.status_code, 400)
        state = client.post(url, json.dumps({'action': 'stop'}), content_type='application/json', **headers).json()
        self.assertIsNone(state['running'])

    def test_resume_to_previous_activity(self):
        self.client.force_login(self.user)
        url = reverse('timelog:bubble')
        services.start_timer(self.user, self.work, at=timezone.now() - datetime.timedelta(minutes=30))
        services.start_timer(self.user, self.phone, at=timezone.now() - datetime.timedelta(minutes=5))
        state = self.client.get(url).json()
        self.assertEqual(state['running']['resumeTo']['name'], 'Praca')
        state = self.client.post(url, json.dumps({'action': 'stop', 'resume': True}), content_type='application/json').json()
        self.assertEqual(state['running']['name'], 'Praca')
        self.assertIsNone(state['running']['resumeTo'])

    def test_bubble_on_other_pages_but_not_in_czas(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get('/'), 'data-time-bubble')
        self.assertContains(self.client.get(reverse('sport:index')), 'data-time-bubble')
        self.assertNotContains(self.client.get(reverse('timelog:index')), 'data-time-bubble')
        self.client.logout()
        self.assertNotContains(self.client.get('/'), 'data-time-bubble')
