import json
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from habits.models import ActivitySettings, Workout, WorkoutSet
from habits.services import garmin

User = get_user_model()


def workout_payload(**overrides):
    data = {
        'uid': '11111111-1111-4111-8111-111111111111',
        'kind': 'run',
        'date': '2026-10-20',
        'time': '07:15',
        'title': '',
        'duration': 1710,
        'distance': 5200,
        'avgHr': 152,
        'notes': 'Lekko',
        'sets': [],
    }
    data.update(overrides)
    return data


class ActivityPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('dawid', password='x')

    def test_page_requires_login(self):
        response = self.client.get(reverse('habits:index'))
        self.assertEqual(response.status_code, 302)

    def test_page_and_service_worker(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('habits:index'))
        self.assertContains(response, 'activity-config')
        self.assertContains(response, 'activity-app.js')
        sw = self.client.get(reverse('habits:sw'))
        self.assertEqual(sw['Content-Type'], 'application/javascript; charset=utf-8')
        self.assertContains(sw, "CACHE_PREFIX = 'aktywnosc-'")
        self.assertContains(sw, '"/habits/api/"')


class ActivityManifestTests(TestCase):
    def test_manifest_is_installable(self):
        response = self.client.get(reverse('habits:manifest'))
        self.assertEqual(response['Content-Type'], 'application/manifest+json; charset=utf-8')
        data = json.loads(response.content)
        self.assertEqual((data['name'], data['start_url'], data['scope'], data['display']),
                         ('Aktywność', '/habits/', '/', 'standalone'))
        self.assertEqual({icon['sizes'] for icon in data['icons']}, {'192x192', '512x512'})
        self.assertIn('maskable', [icon.get('purpose') for icon in data['icons']])

    @override_settings(ALLOWED_HOSTS=['raspberrypi.local'])
    def test_manifest_from_http_points_to_https(self):
        data = json.loads(self.client.get(reverse('habits:manifest'), HTTP_HOST='raspberrypi.local').content)
        self.assertEqual(data['start_url'], 'https://raspberrypi.local/habits/')

    def test_page_links_manifest_and_worker_caches_it(self):
        user = User.objects.create_user('dawid', password='x')
        self.client.force_login(user)
        page = self.client.get(reverse('habits:index'))
        self.assertContains(page, 'rel="manifest" href="/habits/manifest.webmanifest"')
        self.assertContains(page, 'apple-touch-icon')
        sw = self.client.get(reverse('habits:sw')).content.decode()
        self.assertIn('"/habits/manifest.webmanifest"', sw)
        self.assertIn('habits/icons/activity-192', sw)


class ActivityApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('dawid', password='x')
        self.other = User.objects.create_user('ania', password='x')
        self.client.force_login(self.user)

    def sync(self, ops):
        return self.client.post(reverse('habits:api-sync'), data=json.dumps({'ops': ops}), content_type='application/json')

    def test_api_returns_401_without_login(self):
        self.client.logout()
        response = self.client.get(reverse('habits:api-snapshot'))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error'], 'auth')

    def test_snapshot_has_only_own_visible_workouts(self):
        Workout.objects.create(user=self.user, kind='swim', date=date(2026, 10, 1), distance_m=1500)
        Workout.objects.create(user=self.user, kind='run', date=date(2026, 10, 2), is_deleted=True)
        Workout.objects.create(user=self.other, kind='run', date=date(2026, 10, 3))
        data = self.client.get(reverse('habits:api-snapshot')).json()
        self.assertEqual([w['kind'] for w in data['snapshot']['workouts']], ['swim'])
        self.assertEqual(data['snapshot']['settings']['weeklyGoal'], 3)
        self.assertFalse(data['snapshot']['settings']['garmin']['enabled'])
        self.assertTrue(data['csrf_token'])

    def test_save_is_idempotent_and_updates(self):
        op = {'id': 'a', 'type': 'save', 'workout': workout_payload()}
        self.sync([op])
        response = self.sync([op])   # ponowna wysyłka po zerwanym połączeniu
        self.assertEqual(response.json()['results'], [{'id': 'a', 'ok': True}])
        self.assertEqual(Workout.objects.count(), 1)
        workout = Workout.objects.get()
        self.assertEqual((workout.distance_m, workout.duration_s, workout.avg_hr), (5200, 1710, 152))
        self.assertEqual(workout.start_time.strftime('%H:%M'), '07:15')

        self.sync([{'id': 'b', 'type': 'save', 'workout': workout_payload(distance=6000, notes='')}])
        workout.refresh_from_db()
        self.assertEqual(workout.distance_m, 6000)
        self.assertEqual(workout.notes, '')

    def test_gym_sets_are_replaced(self):
        sets = [['Przysiad', 5, 80], ['Przysiad', 5, '82,5'], ['', 1, 1]]
        self.sync([{'id': 'a', 'type': 'save', 'workout': workout_payload(kind='gym', distance=None, sets=sets)}])
        workout = Workout.objects.get()
        self.assertEqual(
            list(workout.sets.values_list('exercise', 'reps', 'weight_kg')),
            [('Przysiad', 5, Decimal('80.00')), ('Przysiad', 5, Decimal('82.50'))],
        )
        self.sync([{'id': 'b', 'type': 'save', 'workout': workout_payload(kind='gym', sets=[['Wiosłowanie', 10, 40]])}])
        self.assertEqual(list(workout.sets.values_list('exercise', flat=True)), ['Wiosłowanie'])

    def test_invalid_operation_does_not_block_others(self):
        response = self.sync([
            {'id': 'bad', 'type': 'save', 'workout': workout_payload(date='jutro')},
            {'id': 'good', 'type': 'goal', 'value': 4},
        ])
        results = response.json()['results']
        self.assertFalse(results[0]['ok'])
        self.assertIn('data', results[0]['message'])
        self.assertTrue(results[1]['ok'])
        self.assertEqual(ActivitySettings.objects.get(user=self.user).weekly_goal, 4)
        self.assertEqual(response.json()['snapshot']['settings']['weeklyGoal'], 4)

    def test_cannot_overwrite_someone_elses_workout(self):
        Workout.objects.create(user=self.other, uid='22222222-2222-4222-8222-222222222222', kind='run', date=date(2026, 10, 1))
        response = self.sync([{'id': 'a', 'type': 'save', 'workout': workout_payload(uid='22222222-2222-4222-8222-222222222222')}])
        self.assertFalse(response.json()['results'][0]['ok'])
        self.assertEqual(Workout.objects.get(uid='22222222-2222-4222-8222-222222222222').user, self.other)
        self.sync([{'id': 'b', 'type': 'delete', 'uid': '22222222-2222-4222-8222-222222222222'}])
        self.assertTrue(Workout.objects.filter(uid='22222222-2222-4222-8222-222222222222').exists())

    def test_delete_and_undo(self):
        self.sync([{'id': 'a', 'type': 'save', 'workout': workout_payload()}])
        self.sync([{'id': 'b', 'type': 'delete', 'uid': workout_payload()['uid']}])
        self.assertFalse(Workout.objects.exists())

        garmin_workout = Workout.objects.create(user=self.user, kind='run', date=date(2026, 10, 1), source='garmin', garmin_id=5)
        self.sync([{'id': 'c', 'type': 'delete', 'uid': garmin_workout.uid}])
        garmin_workout.refresh_from_db()
        self.assertTrue(garmin_workout.is_deleted)   # ślad, żeby Garmin nie pobrał go ponownie
        self.sync([{'id': 'd', 'type': 'save', 'workout': workout_payload(uid=garmin_workout.uid)}])   # „Cofnij”
        garmin_workout.refresh_from_db()
        self.assertFalse(garmin_workout.is_deleted)
        self.assertEqual(garmin_workout.source, 'garmin')

    def test_malformed_body(self):
        response = self.client.post(reverse('habits:api-sync'), data='nie json', content_type='application/json')
        self.assertEqual(response.status_code, 400)


ACTIVITIES = [
    {
        'activityId': 101, 'activityName': 'Kraków Bieganie', 'startTimeLocal': '2026-10-20 07:10:05',
        'activityType': {'typeKey': 'running'}, 'distance': 5210.4, 'duration': 1702.9,
        'averageHR': 151.0, 'calories': 380.0,
    },
    {
        'activityId': 102, 'activityName': 'Basen', 'startTimeLocal': '2026-10-21 18:00:00',
        'activityType': {'typeKey': 'lap_swimming'}, 'distance': 1500.0, 'duration': 2100.0,
    },
    {
        'activityId': 103, 'activityName': 'Siła', 'startTimeLocal': '2026-10-22 19:00:00',
        'activityType': {'typeKey': 'strength_training'}, 'distance': 0.0, 'duration': 3300.0,
    },
    {
        'activityId': 104, 'activityName': 'Spacer', 'startTimeLocal': '2026-10-22 12:00:00',
        'activityType': {'typeKey': 'walking'}, 'distance': 3000.0, 'duration': 2400.0,
    },
]
EXERCISE_SETS = {
    'exerciseSets': [
        {'setType': 'ACTIVE', 'repetitionCount': 8, 'weight': 60000.0,
         'exercises': [{'category': 'BENCH_PRESS', 'name': 'BARBELL_BENCH_PRESS', 'probability': 97.0}]},
        {'setType': 'REST', 'duration': 90.0, 'exercises': []},
        {'setType': 'ACTIVE', 'repetitionCount': 12, 'weight': None,
         'exercises': [{'category': 'PULL_UP', 'name': None, 'probability': 80.0}]},
        {'setType': 'ACTIVE', 'repetitionCount': 10, 'weight': 12500.0,
         'exercises': [{'category': 'MYSTERY_MOVE', 'name': 'MYSTERY_MOVE', 'probability': 50.0}]},
    ]
}


class GarminMappingTests(TestCase):
    def test_map_activity(self):
        run = garmin.map_activity(ACTIVITIES[0])
        self.assertEqual(run['kind'], 'run')
        self.assertEqual(run['date'], date(2026, 10, 20))
        self.assertEqual(run['start_time'].strftime('%H:%M'), '07:10')
        self.assertEqual((run['distance_m'], run['duration_s'], run['avg_hr']), (5210, 1703, 151))
        self.assertEqual(garmin.map_activity(ACTIVITIES[1])['kind'], 'swim')
        gym = garmin.map_activity(ACTIVITIES[2])
        self.assertEqual((gym['kind'], gym['distance_m']), ('gym', None))
        self.assertIsNone(garmin.map_activity(ACTIVITIES[3]))
        self.assertEqual(garmin.workout_kind('cycling'), 'other')
        self.assertEqual(garmin.workout_kind('treadmill_running'), 'run')

    def test_map_sets_converts_grams_and_names(self):
        self.assertEqual(garmin.map_sets(EXERCISE_SETS), [
            ('Wyciskanie na ławce – sztanga', 8, Decimal('60.00')),
            ('Podciąganie', 12, None),
            ('Mystery move', 10, Decimal('12.50')),
        ])


def fake_garmin_client(**overrides):
    """Klient Garmina bez sieci; każda metoda zwraca pustą odpowiedź, chyba że podano inaczej."""
    client = mock.Mock()
    client.get_activities_by_date.return_value = ACTIVITIES
    client.get_activity_exercise_sets.return_value = EXERCISE_SETS
    for name in ('get_activity_splits', 'get_activity_map_details', 'get_activity_details', 'get_activity_polyline',
                 'get_user_summary', 'get_sleep_data',
                 'get_hrv_data', 'get_training_status', 'get_max_metrics', 'get_race_predictions'):
        getattr(client, name).return_value = {}
    client.get_training_readiness.return_value = []
    for name, value in overrides.items():
        getattr(client, name).return_value = value
    return client


@override_settings(GARMIN_ENABLED=True)
class GarminSyncTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('dawid', password='x')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings_override = override_settings(GARMIN_TOKEN_DIR=self.tmp.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def fake_client(self):
        return fake_garmin_client()

    def test_sync_creates_new_and_skips_known(self):
        Workout.objects.create(user=self.user, kind='run', date=date(2026, 10, 19), source='garmin', garmin_id=102, is_deleted=True)
        client = self.fake_client()
        added = garmin.sync_user(self.user, client=client)
        self.assertEqual(added, 2)   # 101 i 103; 102 usunięty na stronie, 104 to spacer
        client.get_activity_exercise_sets.assert_called_once_with(103)
        gym = Workout.objects.get(garmin_id=103)
        self.assertEqual(gym.sets.count(), 3)
        self.assertEqual(garmin.sync_user(self.user, client=client), 0)
        settings_row = ActivitySettings.objects.get(user=self.user)
        self.assertIsNotNone(settings_row.garmin_synced_at)
        self.assertEqual(settings_row.garmin_error, '')

    def test_sync_starts_before_last_garmin_workout(self):
        Workout.objects.create(user=self.user, kind='run', date=date(2026, 10, 10), source='garmin', garmin_id=1)
        client = self.fake_client()
        client.get_activities_by_date.return_value = []
        garmin.sync_user(self.user, client=client)
        self.assertEqual(client.get_activities_by_date.call_args[0][0], '2026-10-07')

    def test_first_sync_uses_default_window(self):
        client = self.fake_client()
        client.get_activities_by_date.return_value = []
        garmin.sync_user(self.user, client=client)
        expected = (timezone.localdate() - timedelta(days=garmin.DEFAULT_DAYS)).isoformat()
        self.assertEqual(client.get_activities_by_date.call_args[0][0], expected)

    def test_errors_are_stored_for_the_page(self):
        client = self.fake_client()
        client.get_activities_by_date.side_effect = RuntimeError('429 Too Many Requests')
        with self.assertRaises(garmin.GarminError), self.assertLogs('habits.services.garmin', 'WARNING'):
            garmin.sync_user(self.user, client=client)
        self.assertIn('429', ActivitySettings.objects.get(user=self.user).garmin_error)

    def test_missing_sets_do_not_block_workout(self):
        client = self.fake_client()
        client.get_activity_exercise_sets.side_effect = RuntimeError('boom')
        with self.assertLogs('habits.services.garmin', 'WARNING'):
            garmin.sync_user(self.user, client=client)
        self.assertFalse(WorkoutSet.objects.exists())
        self.assertTrue(Workout.objects.filter(garmin_id=103).exists())

    def test_refresh_button_without_connection(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('habits:api-refresh'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('połącz', response.json()['message'])

    def test_refresh_button_syncs(self):
        Path(self.tmp.name, str(self.user.pk)).mkdir()
        Path(self.tmp.name, str(self.user.pk), 'garmin_tokens.json').write_text('{}')
        self.client.force_login(self.user)
        with mock.patch.object(garmin, 'connect', return_value=self.fake_client()):
            response = self.client.post(reverse('habits:api-refresh'))
        data = response.json()
        self.assertEqual(data['message'], 'Garmin: nowe treningi 3.')
        self.assertTrue(data['snapshot']['settings']['garmin']['connected'])
        self.assertEqual(len(data['snapshot']['workouts']), 3)
