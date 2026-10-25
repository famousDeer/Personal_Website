"""Garmin (szczegóły, okrążenia, trasy, zdrowie, logowanie ze strony), Hevy (API, CSV)
i łączenie sesji siłowni z obu źródeł. Sieć zawsze podmieniona."""
import tempfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from io import StringIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from habits.models import ActivitySettings, DailyHealth, Workout, WorkoutRoute, WorkoutSet
from habits.services import garmin, garmin_health, hevy
from habits.services.workouts import activity_snapshot, apply_operations

from .test_activity import fake_garmin_client

User = get_user_model()


# Klasy o tych samych nazwach co w garminconnect (rozpoznajemy je po nazwie,
# bo biblioteka jest importowana leniwie i nie ma jej w testach lokalnych).
class GarminConnectAuthenticationError(Exception):
    pass


class GarminConnectTooManyRequestsError(Exception):
    pass

RUN_SUMMARY = {
    'activityId': 201, 'activityName': 'Bieg z interwałami', 'startTimeLocal': '2026-10-20 07:00:00',
    'activityType': {'typeKey': 'running'}, 'distance': 8000.0, 'duration': 2400.0, 'averageHR': 150.0,
    'maxHR': 178.0, 'elevationGain': 54.2, 'elevationLoss': 50.0, 'averageRunningCadenceInStepsPerMinute': 172.4,
    'aerobicTrainingEffect': 3.4, 'anaerobicTrainingEffect': 1.2, 'trainingEffectLabel': 'TEMPO',
    'activityTrainingLoad': 96.3, 'vO2MaxValue': 51.0, 'hasPolyline': True, 'locationName': 'Kraków',
    'hrTimeInZone_1': 120.0, 'hrTimeInZone_2': 600.0, 'hrTimeInZone_3': 900.0, 'hrTimeInZone_4': 700.0, 'hrTimeInZone_5': 80.0,
    'minTemperature': -1.0,
}
SWIM_SUMMARY = {
    'activityId': 202, 'activityName': 'Basen', 'startTimeLocal': '2026-10-21 18:00:00',
    'activityType': {'typeKey': 'lap_swimming'}, 'distance': 1500.0, 'duration': 2100.0,
    'poolLength': 2500.0, 'unitOfPoolLength': {'unitKey': 'meter', 'factor': 100.0},
    'activeLengths': 60, 'averageSwolf': 41.0, 'totalNumberOfStrokes': 1100, 'avgStrokes': 18.3,
}
GYM_SUMMARY = {
    'activityId': 203, 'activityName': 'Siła', 'startTimeLocal': '2026-10-22 18:05:00',
    'activityType': {'typeKey': 'strength_training'}, 'distance': 0.0, 'duration': 3600.0,
    'averageHR': 112.0, 'calories': 410.0,
}
HEVY_WORKOUT = {
    'id': 'b459cba5-cd6d-463c-abd6-54f8eafcadcb',
    'title': 'Push',
    'description': 'Dobra forma',
    # 16:20 UTC = 18:20 w Warszawie (czas letni do 25.10.2026)
    'start_time': '2026-10-22T16:20:00Z',
    'end_time': '2026-10-22T17:10:00Z',
    'exercises': [
        {'index': 0, 'title': 'Bench Press (Barbell)', 'sets': [
            {'index': 0, 'type': 'warmup', 'weight_kg': 40, 'reps': 10},
            {'index': 1, 'type': 'normal', 'weight_kg': 80, 'reps': 5},
            {'index': 2, 'type': 'failure', 'weight_kg': 80, 'reps': 4},
        ]},
        {'index': 1, 'title': 'Plank', 'sets': [
            {'index': 0, 'type': 'normal', 'weight_kg': None, 'reps': None, 'duration_seconds': 60},
        ]},
    ],
}


class GarminParsingTests(TestCase):
    def test_details_from_summary(self):
        details = garmin.parse_details(RUN_SUMMARY)
        self.assertEqual(details['maxHr'], 178)
        self.assertEqual(details['elevationGain'], 54)
        self.assertEqual(details['cadence'], 172)
        self.assertEqual((details['aerobicTE'], details['anaerobicTE'], details['teLabel']), (3.4, 1.2, 'TEMPO'))
        self.assertEqual(details['trainingLoad'], 96)
        self.assertEqual(details['vo2max'], 51.0)
        self.assertEqual(details['hrZones'], [120, 600, 900, 700, 80])
        self.assertNotIn('hasRoute', details)   # dopiero po pobraniu trasy
        self.assertEqual(details['location'], 'Kraków')
        swim = garmin.parse_details(SWIM_SUMMARY)
        self.assertEqual((swim['poolLength'], swim['lengths'], swim['swolf'], swim['strokes']), (25.0, 60, 41, 1100))
        self.assertEqual(garmin.parse_details({}), {})

    def test_laps_and_route(self):
        laps = garmin.parse_laps({'lapDTOs': [
            {'distance': 1000.0, 'duration': 300.5, 'averageHR': 140.2, 'elevationGain': 5.0},
            {'distance': 1000.0, 'duration': 290.0, 'averageHR': 150.0},
            {'distance': 0.0, 'duration': 0.0},
        ]})
        self.assertEqual(laps, [{'d': 1000, 't': 300, 'hr': 140, 'elev': 5}, {'d': 1000, 't': 290, 'hr': 150}])
        route = garmin.parse_route({'geoPolylineDTO': {'polyline': [
            {'lat': 50.061947, 'lon': 19.936856}, {'lat': 50.0621, 'lon': 19.9371}, {'lat': None, 'lon': None},
        ]}})
        self.assertEqual(route, [[50.06195, 19.93686], [50.0621, 19.9371]])
        # Przykład z dokumentacji algorytmu Google Encoded Polyline.
        self.assertEqual(garmin.parse_route({'gPolyline': '_p~iF~ps|U_ulLnnqC_mqNvxq`@'}),
                         [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]])
        # Próbki z /details (kolejność kanałów z metricDescriptors) i sam napis polilinii.
        details_payload = {
            'metricDescriptors': [{'key': 'directLatitude', 'metricsIndex': 2}, {'key': 'directLongitude', 'metricsIndex': 0}],
            'activityDetailMetrics': [{'metrics': [19.9, 0, 50.06]}, {'metrics': [19.91, 0, 50.07]}, {'metrics': [None, 0, None]}],
        }
        self.assertEqual(garmin.parse_route(details_payload), [[50.06, 19.9], [50.07, 19.91]])
        self.assertEqual(len(garmin.parse_route('_p~iF~ps|U_ulLnnqC_mqNvxq`@')), 3)
        self.assertEqual(garmin.parse_route({'geoPolylineDTO': {'polyline': [{'lat': 0, 'lon': 0}, {'lat': 91, 'lon': 10}]}}), [])
        long_route = garmin.parse_route({'geoPolylineDTO': {'polyline': [{'lat': 50 + i / 1e4, 'lon': 19.0} for i in range(2000)]}})
        self.assertLessEqual(len(long_route), garmin.ROUTE_MAX_POINTS + 1)
        self.assertEqual(long_route[-1], [50.1999, 19.0])


@override_settings(GARMIN_ENABLED=True)
class GarminExtrasAndMergeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('dawid', password='x')

    def test_sync_saves_details_raw_and_extras(self):
        client = fake_garmin_client(
            get_activities_by_date=[RUN_SUMMARY, SWIM_SUMMARY],
            get_activity_splits={'lapDTOs': [{'distance': 4000, 'duration': 1200}, {'distance': 4000, 'duration': 1200}]},
            get_activity_map_details={'geoPolylineDTO': {'polyline': [{'lat': 50.0, 'lon': 19.0}, {'lat': 50.01, 'lon': 19.01}]}},
        )
        garmin.sync_all(self.user, client=client)
        run = Workout.objects.get(garmin_id=201)
        self.assertEqual(run.garmin_raw['maxHR'], 178.0)
        self.assertEqual(run.details['hrZones'][2], 900)
        self.assertEqual(len(run.details['laps']), 2)
        self.assertTrue(run.extras_synced)
        self.assertEqual(WorkoutRoute.objects.get(workout=run).points, [[50.0, 19.0], [50.01, 19.01]])
        swim = Workout.objects.get(garmin_id=202)
        self.assertTrue(swim.extras_synced)
        self.assertFalse(WorkoutRoute.objects.filter(workout=swim).exists())   # basen bez trasy

        # Po zmianie pól przeliczenie z zapisanych odpowiedzi, bez sieci.
        Workout.objects.filter(pk=run.pk).update(details={'laps': run.details['laps']})
        garmin.reparse(self.user)
        run.refresh_from_db()
        self.assertEqual(run.details['maxHr'], 178)
        self.assertEqual(len(run.details['laps']), 2)
        self.assertTrue(run.details['hasRoute'])

    def test_route_wanted(self):
        outdoor = Workout(kind='run', distance_m=5000, garmin_type='running', garmin_raw={})
        self.assertTrue(garmin.route_wanted(outdoor))   # brak informacji -> próbujemy
        self.assertFalse(garmin.route_wanted(Workout(kind='run', distance_m=5000, garmin_type='treadmill_running', garmin_raw={})))
        self.assertFalse(garmin.route_wanted(Workout(kind='swim', distance_m=1500, garmin_type='lap_swimming', garmin_raw={})))
        self.assertTrue(garmin.route_wanted(Workout(kind='swim', distance_m=1500, garmin_type='open_water_swimming', garmin_raw={})))
        self.assertFalse(garmin.route_wanted(Workout(kind='run', distance_m=5000, garmin_type='running', garmin_raw={'hasPolyline': False})))
        self.assertTrue(garmin.route_wanted(Workout(kind='other', garmin_type='hiking', garmin_raw={'startLatitude': 50.0})))

    def test_route_falls_back_to_other_sources(self):
        geo = {'geoPolylineDTO': {'polyline': [{'lat': 50.0, 'lon': 19.0}, {'lat': 50.01, 'lon': 19.01}]}}
        client = fake_garmin_client(get_activities_by_date=[dict(RUN_SUMMARY, hasPolyline=None)], get_activity_details=geo)
        client.get_activity_map_details.side_effect = RuntimeError('404')
        garmin.sync_all(self.user, client=client)
        run = Workout.objects.get(garmin_id=201)
        self.assertTrue(run.details['hasRoute'])
        self.assertEqual(len(WorkoutRoute.objects.get(workout=run).points), 2)
        client.get_activity_polyline.assert_not_called()

    def test_retry_routes_and_inspect(self):
        client = fake_garmin_client(get_activities_by_date=[RUN_SUMMARY], get_activity_polyline={})
        garmin.sync_all(self.user, client=client)
        run = Workout.objects.get(garmin_id=201)
        self.assertTrue(run.extras_synced)
        self.assertFalse(WorkoutRoute.objects.exists())
        self.assertEqual(client.get_activity_polyline.call_count, 1)   # wszystkie trzy źródła sprawdzone
        self.assertEqual(garmin.retry_routes(self.user), 1)
        run.refresh_from_db()
        self.assertFalse(run.extras_synced)
        client.get_activity_map_details.return_value = {'gPolyline': '_p~iF~ps|U_ulLnnqC_mqNvxq`@'}
        garmin.sync_all(self.user, client=client)
        self.assertEqual(len(WorkoutRoute.objects.get(workout=run).points), 3)
        lines = garmin.inspect_route(client, run)
        self.assertIn('szukamy trasy: tak', lines[1])
        self.assertIn('mapdetails: klucze', lines[2])
        self.assertIn('punktów trasy: 3', lines[2])

    def test_extras_are_limited_per_run(self):
        for index in range(5):
            Workout.objects.create(user=self.user, kind='run', date=date(2026, 10, index + 1), garmin_id=900 + index)
        client = fake_garmin_client(get_activities_by_date=[])
        garmin.sync_all(self.user, client=client, extras_limit=2)
        self.assertEqual(Workout.objects.filter(extras_synced=True).count(), 2)
        self.assertEqual(sorted(Workout.objects.filter(extras_synced=True).values_list('garmin_id', flat=True)), [903, 904])

    def test_garmin_gym_attaches_to_existing_hevy_session(self):
        hevy_workout = Workout.objects.create(
            user=self.user, kind='gym', date=date(2026, 10, 22), start_time=time(18, 20), duration_s=3000,
            source='hevy', hevy_id='h-1',
        )
        WorkoutSet.objects.create(workout=hevy_workout, exercise='Bench Press (Barbell)', reps=5, weight_kg=80)
        client = fake_garmin_client(get_activities_by_date=[GYM_SUMMARY])
        added = garmin.sync_user(self.user, client=client)
        self.assertEqual(added, 1)
        self.assertEqual(Workout.objects.count(), 1)
        hevy_workout.refresh_from_db()
        self.assertEqual((hevy_workout.garmin_id, hevy_workout.avg_hr, hevy_workout.calories), (203, 112, 410))
        self.assertEqual(hevy_workout.start_time, time(18, 5))   # czas z zegarka
        self.assertEqual(list(hevy_workout.sets.values_list('exercise', flat=True)), ['Bench Press (Barbell)'])
        client.get_activity_exercise_sets.assert_not_called()

    def test_garmin_gym_far_from_hevy_session_stays_separate(self):
        Workout.objects.create(user=self.user, kind='gym', date=date(2026, 10, 22), start_time=time(7, 0),
                               duration_s=3000, source='hevy', hevy_id='h-1')
        garmin.sync_user(self.user, client=fake_garmin_client(get_activities_by_date=[GYM_SUMMARY]))
        self.assertEqual(Workout.objects.count(), 2)


class GarminHealthTests(TestCase):
    RAW = {
        'summary': {'totalSteps': 11234, 'dailyStepGoal': 10000, 'restingHeartRate': 52, 'averageStressLevel': -1,
                    'bodyBatteryHighestValue': 88, 'bodyBatteryLowestValue': 21,
                    'moderateIntensityMinutes': 20, 'vigorousIntensityMinutes': 15},
        'sleep': {'dailySleepDTO': {'sleepTimeSeconds': 26400, 'deepSleepSeconds': 5400, 'remSleepSeconds': 6000,
                                    'sleepScores': {'overall': {'value': 81}}}},
        'hrv': {'hrvSummary': {'lastNightAvg': 47, 'status': 'BALANCED'}},
        'readiness': [{'score': 72, 'level': 'HIGH'}],
        'status': {'mostRecentVO2Max': {'generic': {'vo2MaxPreciseValue': 50.6}},
                   'mostRecentTrainingStatus': {'latestTrainingStatusData': {'3312': {'trainingStatusFeedbackPhrase': 'PRODUCTIVE_6'}}}},
        'maxMetrics': [{'generic': {'vo2MaxPreciseValue': 50.8}}],
        'race': {'time5K': 1395, 'time10K': 2950, 'timeHalfMarathon': 6610, 'timeMarathon': 14100},
    }

    def setUp(self):
        self.user = User.objects.create_user('dawid', password='x')

    def test_fields_from_raw(self):
        fields = garmin_health.fields_from_raw(self.RAW)
        self.assertEqual(fields['steps'], 11234)
        self.assertEqual(fields['resting_hr'], 52)
        self.assertNotIn('stress_avg', fields)   # -1 = brak pomiaru
        self.assertEqual((fields['body_battery_high'], fields['body_battery_low']), (88, 21))
        self.assertEqual(fields['intensity_minutes'], 50)
        self.assertEqual((fields['sleep_s'], fields['sleep_score']), (26400, 81))
        self.assertEqual((fields['hrv_avg'], fields['hrv_status']), (47, 'BALANCED'))
        self.assertEqual((fields['readiness'], fields['readiness_level']), (72, 'HIGH'))
        self.assertEqual(fields['training_status'], 'PRODUCTIVE')
        self.assertEqual(fields['vo2max'], Decimal('50.8'))   # max metrics przed statusem
        self.assertEqual((fields['race_5k_s'], fields['race_half_s']), (1395, 6610))

    def health_client(self, **overrides):
        values = dict(
            get_user_summary=self.RAW['summary'],
            get_sleep_data={**self.RAW['sleep'], 'sleepMovement': list(range(500))},
            get_hrv_data=self.RAW['hrv'],
            get_training_readiness=self.RAW['readiness'],
            get_training_status=self.RAW['status'],
        )
        values.update(overrides)
        return fake_garmin_client(**values)

    def fill_month(self, skip=()):
        """Kompletne dni z ostatnich 30 poza `skip` (przesunięcia od dziś)."""
        today = timezone.localdate()
        for offset in range(garmin_health.BACKFILL_DAYS):
            if offset not in skip:
                garmin_health.save_day(self.user, today - timedelta(days=offset),
                                       {'summary': self.RAW['summary'], 'sleep': self.RAW['sleep']}, today=today)

    def test_sync_health_saves_days_and_throttles(self):
        self.fill_month(skip=(0, 1))
        client = self.health_client()
        client.get_max_metrics.side_effect = RuntimeError('brak')
        with self.assertLogs('habits.services.garmin_health', 'WARNING') as logs:
            self.assertEqual(garmin_health.sync_health(self.user, client), 2)
        self.assertIn('maxMetrics za', logs.output[0])   # błąd widoczny w logach
        today = DailyHealth.objects.get(user=self.user, date=timezone.localdate())
        self.assertEqual((today.steps, today.sleep_score, today.vo2max), (11234, 81, Decimal('50.6')))
        self.assertNotIn('sleepMovement', today.raw['sleep'])   # długie serie przycięte
        self.assertEqual(today.raw['_errors'], ['maxMetrics'])
        yesterday = DailyHealth.objects.get(user=self.user, date=timezone.localdate() - timedelta(days=1))
        self.assertIsNone(yesterday.vo2max)   # forma tylko dla dzisiaj
        self.assertEqual(client.get_training_status.call_count, 1)
        self.assertEqual(garmin_health.sync_health(self.user, client), 0)   # za wcześnie
        client.get_max_metrics.side_effect = None
        self.assertEqual(garmin_health.sync_health(self.user, client, force=True), 2)

    def test_missing_and_failed_days_are_filled_later(self):
        # Tydzień bez danych, jak po chwilowej blokadzie Garmina przy imporcie.
        self.fill_month(skip=(0, 1, 7, 8, 9, 10, 11, 12, 13))
        today = timezone.localdate()
        failing = self.health_client()
        failing.get_user_summary.side_effect = RuntimeError('403 Cloudflare')
        with self.assertLogs('habits.services.garmin_health', 'WARNING'), \
                mock.patch.object(garmin_health, 'pause') as pause:
            garmin_health.sync_health(self.user, failing, force=True)
        self.assertTrue(pause.called)   # większe pobieranie ma odstępy
        self.assertEqual(failing.get_sleep_data.call_count, 2 + 7)   # dziś, wczoraj i 7 luk
        gap = DailyHealth.objects.get(user=self.user, date=today - timedelta(days=7))
        self.assertEqual((gap.raw['_errors'], gap.raw['_attempts'], gap.steps), (['summary'], 1, None))

        # Tego samego dnia luk nie ponawiamy; następnego dnia - tak.
        ok = self.health_client()
        garmin_health.sync_health(self.user, ok, force=True)
        self.assertEqual(ok.get_sleep_data.call_count, 2)
        with mock.patch.object(garmin_health.timezone, 'localdate', return_value=today + timedelta(days=1)):
            self.assertEqual(garmin_health.gap_days(self.user, today + timedelta(days=1))[0], today - timedelta(days=7))
            garmin_health.sync_health(self.user, ok, force=True)
        gap.refresh_from_db()
        self.assertEqual((gap.steps, gap.raw['_errors'], gap.raw['_attempts']), (11234, [], 0))

    def test_gap_retries_are_limited(self):
        self.fill_month(skip=(5,))
        today = timezone.localdate()
        day = today - timedelta(days=5)
        DailyHealth.objects.create(user=self.user, date=day, raw={'_attempts': garmin_health.MAX_ATTEMPTS, '_checked': '2000-01-01'})
        self.assertEqual(garmin_health.gap_days(self.user, today), [])   # dzień bez zegarka nie wraca w kółko

    def test_interactive_refresh_skips_gaps(self):
        client = self.health_client()
        garmin_health.sync_health(self.user, client, force=True, fill_gaps=False)
        self.assertEqual(client.get_sleep_data.call_count, 2)

    def test_health_report(self):
        self.fill_month(skip=(3,))
        lines = garmin_health.report(self.user)
        self.assertEqual(len(lines), 31)
        self.assertIn('11234', lines[1])
        self.assertIn('summary,sleep', lines[1])
        self.assertIn('brak wiersza', lines[4])

    def test_snapshot_includes_recent_health(self):
        DailyHealth.objects.create(user=self.user, date=timezone.localdate(), steps=5000, hrv_status='BALANCED', vo2max=Decimal('50.1'))
        DailyHealth.objects.create(user=self.user, date=timezone.localdate() - timedelta(days=400), steps=1)
        health = activity_snapshot(self.user)['health']
        self.assertEqual(health, [{'date': timezone.localdate().isoformat(), 'steps': 5000, 'hrvStatus': 'BALANCED', 'vo2max': 50.1}])


@override_settings(HEVY_ENABLED=True)
class HevyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('dawid', password='x')
        ActivitySettings.objects.create(user=self.user, hevy_api_key='key-123')

    def test_parse_workout(self):
        parsed = hevy.parse_workout(HEVY_WORKOUT)
        self.assertEqual(parsed['start'], datetime(2026, 10, 22, 18, 20))
        self.assertEqual(parsed['end'], datetime(2026, 10, 22, 19, 10))
        self.assertEqual([(s['exercise'], s['reps'], s['weight_kg'], s['set_type'], s['duration_s']) for s in parsed['sets']], [
            ('Bench Press (Barbell)', 10, Decimal('40.00'), 'warmup', None),
            ('Bench Press (Barbell)', 5, Decimal('80.00'), '', None),
            ('Bench Press (Barbell)', 4, Decimal('80.00'), 'failure', None),
            ('Plank', None, None, '', 60),
        ])

    def test_sync_pages_events_and_merges_with_garmin(self):
        garmin_gym = Workout.objects.create(
            user=self.user, kind='gym', date=date(2026, 10, 22), start_time=time(18, 5), duration_s=3600,
            source='garmin', garmin_id=203, avg_hr=112,
        )
        WorkoutSet.objects.create(workout=garmin_gym, exercise='Wyciskanie na ławce', reps=5, weight_kg=80)
        deleted = Workout.objects.create(user=self.user, kind='gym', date=date(2026, 10, 1), source='hevy', hevy_id='old')
        pages = [
            {'page': 1, 'page_count': 2, 'events': [{'type': 'updated', 'workout': HEVY_WORKOUT}]},
            {'page': 2, 'page_count': 2, 'events': [{'type': 'deleted', 'id': 'old'}]},
        ]
        with mock.patch.object(hevy, 'request', side_effect=pages) as request:
            added = hevy.sync_user(self.user)
        self.assertEqual(added, 0)   # połączony z treningiem z zegarka
        self.assertEqual(request.call_args_list[0].args[:2], ('key-123', '/workouts/events'))
        self.assertEqual(request.call_args_list[0].args[2]['since'], '1970-01-01T00:00:00Z')
        self.assertEqual(request.call_args_list[1].args[2]['page'], 2)
        garmin_gym.refresh_from_db()
        self.assertEqual(garmin_gym.hevy_id, HEVY_WORKOUT['id'])
        self.assertEqual((garmin_gym.title, garmin_gym.notes, garmin_gym.avg_hr), ('Push', 'Dobra forma', 112))
        self.assertEqual(garmin_gym.sets.count(), 4)
        self.assertEqual(garmin_gym.sets.filter(set_type='warmup').count(), 1)
        self.assertFalse(Workout.objects.filter(pk=deleted.pk).exists())
        self.assertIsNotNone(ActivitySettings.objects.get(user=self.user).hevy_synced_at)

        # Kolejna synchronizacja pyta tylko o zmiany od ostatniej (z zapasem).
        with mock.patch.object(hevy, 'request', return_value={'page': 1, 'page_count': 1, 'events': [
            {'type': 'deleted', 'id': HEVY_WORKOUT['id']},
        ]}) as request:
            hevy.sync_user(self.user)
        self.assertNotEqual(request.call_args.args[2]['since'], '1970-01-01T00:00:00Z')
        garmin_gym.refresh_from_db()
        self.assertIsNone(garmin_gym.hevy_id)   # trening z zegarka zostaje
        self.assertEqual(garmin_gym.sets.count(), 0)

    def test_standalone_hevy_workout_and_error(self):
        with mock.patch.object(hevy, 'request', return_value={'page': 1, 'page_count': 1, 'events': [
            {'type': 'updated', 'workout': HEVY_WORKOUT},
        ]}):
            self.assertEqual(hevy.sync_user(self.user), 1)
        workout = Workout.objects.get()
        self.assertEqual((workout.source, workout.kind, workout.start_time, workout.duration_s), ('hevy', 'gym', time(18, 20), 3000))
        with mock.patch.object(hevy, 'request', side_effect=hevy.HevyError('Hevy nie przyjął klucza API.')):
            with self.assertRaises(hevy.HevyError):
                hevy.sync_user(self.user)
        self.assertIn('klucza', ActivitySettings.objects.get(user=self.user).hevy_error)

    def test_request_maps_http_errors(self):
        error = hevy.urllib.error.HTTPError('u', 401, 'Unauthorized', {}, None)
        with mock.patch.object(hevy.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaisesMessage(hevy.HevyError, 'Hevy Pro'):
                hevy.request('key', '/user/info')

    def test_csv_import_kg_is_idempotent(self):
        csv_text = (
            'title,start_time,end_time,description,exercise_title,superset_id,exercise_notes,set_index,set_type,weight_kg,reps,distance_km,duration_seconds,rpe\n'
            '"Push","22 Oct 2026, 18:20","22 Oct 2026, 19:10","","Bench Press (Barbell)",,"",0,warmup,40,10,,,\n'
            '"Push","22 Oct 2026, 18:20","22 Oct 2026, 19:10","","Bench Press (Barbell)",,"",1,normal,80,5,,,\n'
            '"Legs","20 Oct 2026, 17:00","20 Oct 2026, 18:00","Ciężko","Squat (Barbell)",,"",0,normal,100,5,,,8\n'
        )
        upload = lambda: SimpleUploadedFile('workouts.csv', csv_text.encode('utf-8'), content_type='text/csv')
        self.assertEqual(hevy.import_csv(self.user, upload()), (2, 0))
        self.assertEqual(hevy.import_csv(self.user, upload()), (0, 2))
        self.assertEqual(Workout.objects.count(), 2)
        push = Workout.objects.get(title='Push')
        self.assertTrue(push.hevy_id.startswith('csv:'))
        self.assertEqual((push.start_time, push.duration_s), (time(18, 20), 3000))
        self.assertEqual(list(push.sets.values_list('set_type', 'weight_kg')), [('warmup', Decimal('40.00')), ('', Decimal('80.00'))])

        # Później Pro i API: ten sam trening przejmuje identyfikator z API.
        hevy.save_parsed(self.user, hevy.parse_workout(HEVY_WORKOUT))
        self.assertEqual(Workout.objects.count(), 2)
        push.refresh_from_db()
        self.assertEqual(push.hevy_id, HEVY_WORKOUT['id'])

    def test_csv_import_polish_dates_and_warmup_exercise(self):
        csv_text = (
            '"title","start_time","end_time","description","exercise_title","superset_id","exercise_notes","set_index","set_type","weight_kg","reps","distance_km","duration_seconds","rpe"\n'
            '"Ramiona","19 paź 2026, 16:02","19 paź 2026, 16:49","","Rozgrzewka",,"",0,"normal",,,,120,\n'
            '"Ramiona","19 paź 2026, 16:02","19 paź 2026, 16:49","","Uginanie ramion (hantle)",,"",0,"normal",7.5,10,,,\n'
            '"Ramiona","19 paź 2026, 16:02","19 paź 2026, 16:49","","Wioślarz (maszyna)",,"",0,"normal",,,0.5,120,\n'
            '"Brzuch","3 lut 2026, 22:01","3 lut 2026, 22:14","","Crunch",,"",0,"normal",,10,,,\n'
            '"Arms","19 Aug 2026, 19:48","19 Aug 2026, 20:36","","Warm Up",,"",0,"normal",,,,106,\n'
        )
        self.assertEqual(hevy.import_csv(self.user, SimpleUploadedFile('w.csv', csv_text.encode('utf-8'))), (3, 0))
        arms = Workout.objects.get(title='Ramiona')
        self.assertEqual((arms.date, arms.start_time, arms.duration_s), (date(2026, 10, 19), time(16, 2), 2820))
        self.assertEqual(list(arms.sets.values_list('exercise', 'set_type', 'weight_kg', 'duration_s')), [
            ('Rozgrzewka', 'warmup', None, 120),
            ('Uginanie ramion (hantle)', '', Decimal('7.50'), None),
            ('Wioślarz (maszyna)', '', None, 120),
        ])
        self.assertEqual(Workout.objects.get(title='Brzuch').date, date(2026, 2, 3))
        self.assertEqual(Workout.objects.get(title='Arms').sets.get().set_type, 'warmup')
        bad = csv_text.splitlines()[0] + '\n"X","przedwczoraj","","","Crunch",,"",0,"normal",,10,,,\n'
        with self.assertRaisesMessage(hevy.HevyError, 'Nie rozpoznaję daty'):
            hevy.import_csv(self.user, SimpleUploadedFile('x.csv', bad.encode('utf-8')))

    def test_csv_import_lbs_and_bad_file(self):
        csv_text = (
            'title,start_time,end_time,description,exercise_title,superset_id,exercise_notes,set_index,set_type,weight_lbs,reps,distance_miles,duration_seconds,rpe\n'
            '"Arms","2026-10-18 10:00:00","2026-10-18 11:00:00","","Bicep Curl (Dumbbell)",,"",0,normal,25,12,,,\n'
        )
        hevy.import_csv(self.user, SimpleUploadedFile('w.csv', csv_text.encode('utf-8')))
        self.assertEqual(WorkoutSet.objects.get().weight_kg, Decimal('11.34'))
        with self.assertRaisesMessage(hevy.HevyError, 'eksport z Hevy'):
            hevy.import_csv(self.user, SimpleUploadedFile('x.csv', b'a,b\n1,2\n'))

    def test_deleted_on_page_stays_deleted(self):
        workout, _ = hevy.save_parsed(self.user, hevy.parse_workout(HEVY_WORKOUT))
        apply_operations([{'id': '1', 'type': 'delete', 'uid': workout.uid}], self.user)
        workout.refresh_from_db()
        self.assertTrue(workout.is_deleted)
        hevy.save_parsed(self.user, hevy.parse_workout(HEVY_WORKOUT))
        workout.refresh_from_db()
        self.assertTrue(workout.is_deleted)
        self.assertEqual(activity_snapshot(self.user)['workouts'], [])


class SetSerializationTests(TestCase):
    def test_set_types_round_trip_through_phone(self):
        user = User.objects.create_user('dawid', password='x')
        workout = {'uid': 'u-1', 'kind': 'gym', 'date': '2026-10-22', 'sets': [
            ['Przysiad', 10, 40, 'warmup', None], ['Przysiad', 5, 100], ['Deska', None, None, '', 60],
        ]}
        self.assertTrue(apply_operations([{'id': 'a', 'type': 'save', 'workout': workout}], user)[0]['ok'])
        sets = activity_snapshot(user)['workouts'][0]['sets']
        self.assertEqual(sets, [['Przysiad', 10, 40.0, 'warmup', None], ['Przysiad', 5, 100.0], ['Deska', None, None, '', 60]])
        bad = dict(workout, sets=[['Przysiad', 5, 100, 'superset']])
        self.assertFalse(apply_operations([{'id': 'b', 'type': 'save', 'workout': bad}], user)[0]['ok'])


@override_settings(GARMIN_ENABLED=True, HEVY_ENABLED=True)
class ConnectionsViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('dawid', password='x')
        self.client.force_login(self.user)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(GARMIN_TOKEN_DIR=self.tmp.name)
        override.enable()
        self.addCleanup(override.disable)
        self.url = reverse('habits:connections')

    def garmin_class(self, login_result=(None, None)):
        instance = mock.Mock()
        instance.login.return_value = login_result
        instance.client.dump.side_effect = lambda path: Path(path, 'garmin_tokens.json').write_text('{}')
        return mock.Mock(return_value=instance), instance

    def test_page_renders_both_sections(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'Połącz z Garminem')
        self.assertContains(response, 'Połącz z Hevy')
        self.assertContains(response, 'Importuj')

    def test_garmin_login_without_2fa(self):
        cls, instance = self.garmin_class()
        with mock.patch.object(garmin, '_garmin_class', return_value=cls), \
                mock.patch.object(garmin, 'start_import') as start_import:
            response = self.client.post(self.url, {'action': 'garmin-login', 'email': 'a@b.pl', 'password': 'tajne'}, follow=True)
        cls.assert_called_once_with('a@b.pl', 'tajne', return_on_mfa=True)
        self.assertTrue(garmin.is_connected(self.user))
        start_import.assert_called_once_with(self.user)
        self.assertContains(response, 'Połączono z Garmin Connect')
        self.assertNotContains(response, 'tajne')

    def test_garmin_login_with_2fa(self):
        cls, instance = self.garmin_class(login_result=('needs_mfa', {'cookies': ['x']}))
        with mock.patch.object(garmin, '_garmin_class', return_value=cls), mock.patch.object(garmin, 'start_import'):
            response = self.client.post(self.url, {'action': 'garmin-login', 'email': 'a@b.pl', 'password': 'tajne'}, follow=True)
            self.assertContains(response, 'Wpisz kod')
            self.assertFalse(garmin.is_connected(self.user))
            self.assertIn('garmin_mfa', self.client.session)
            self.client.post(self.url, {'action': 'garmin-code', 'code': '123 456'})
        instance.resume_login.assert_called_once_with({'cookies': ['x']}, '123456')
        self.assertTrue(garmin.is_connected(self.user))
        self.assertNotIn('garmin_mfa', self.client.session)

    def test_garmin_wrong_password_shows_reason_and_logs_detail(self):
        cls, instance = self.garmin_class()
        instance.login.side_effect = GarminConnectAuthenticationError('401 Unauthorized (Invalid Username or Password)')
        with mock.patch.object(garmin, '_garmin_class', return_value=cls), \
                self.assertLogs('habits.services.garmin', 'WARNING') as logs:
            response = self.client.post(self.url, {'action': 'garmin-login', 'email': 'a@b.pl', 'password': 'zle'}, follow=True)
        self.assertContains(response, 'Garmin odrzucił e-mail lub hasło')
        self.assertContains(response, 'Google, Apple albo Facebook')
        self.assertContains(response, 'Invalid Username or Password')
        self.assertIn('Invalid Username or Password', logs.output[0])
        self.assertNotIn('zle', logs.output[0])
        self.assertFalse(garmin.is_connected(self.user))

    def test_friendly_errors(self):
        self.assertIn('zablokowane', garmin.friendly_error(GarminConnectAuthenticationError("Widget authentication failed: 'Account Locked'")))
        self.assertIn('kodu weryfikacyjnego', garmin.friendly_error(GarminConnectAuthenticationError('MFA verification failed: x')))
        self.assertIn('zbyt wiele prób', garmin.friendly_error(GarminConnectTooManyRequestsError('All login strategies rate limited')))
        self.assertIn('Cloudflare', garmin.friendly_error(RuntimeError('All login strategies exhausted: HTTP 403 (Cloudflare bot challenge)')))

    def test_garmin_disconnect(self):
        Path(self.tmp.name, str(self.user.pk)).mkdir()
        Path(self.tmp.name, str(self.user.pk), 'garmin_tokens.json').write_text('{}')
        self.client.post(self.url, {'action': 'garmin-disconnect'})
        self.assertFalse(garmin.is_connected(self.user))

    def test_hevy_key_and_csv(self):
        with mock.patch.object(hevy, 'check_key') as check, mock.patch.object(hevy, 'start_import') as start_import:
            self.client.post(self.url, {'action': 'hevy-key', 'api_key': ' abc-123 '})
        check.assert_called_once_with('abc-123')
        start_import.assert_called_once()
        self.assertEqual(ActivitySettings.objects.get(user=self.user).hevy_api_key, 'abc-123')
        response = self.client.get(self.url)
        self.assertContains(response, '…-123')

        csv_file = SimpleUploadedFile('w.csv', (
            'title,start_time,end_time,exercise_title,set_type,weight_kg,reps\n'
            'Push,"22 Oct 2026, 18:20","22 Oct 2026, 19:10",Bench Press (Barbell),normal,80,5\n'
        ).encode('utf-8'))
        response = self.client.post(self.url, {'action': 'hevy-csv', 'file': csv_file}, follow=True)
        self.assertContains(response, 'nowe treningi 1')
        self.client.post(self.url, {'action': 'hevy-disconnect'})
        self.assertEqual(ActivitySettings.objects.get(user=self.user).hevy_api_key, '')

    def test_route_api_is_per_user(self):
        workout = Workout.objects.create(user=self.user, kind='run', date=date(2026, 10, 1))
        WorkoutRoute.objects.create(workout=workout, points=[[50.0, 19.0], [50.1, 19.1]])
        response = self.client.get(reverse('habits:api-route', args=[workout.uid]))
        self.assertEqual(response.json()['points'], [[50.0, 19.0], [50.1, 19.1]])
        other = User.objects.create_user('ania', password='x')
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse('habits:api-route', args=[workout.uid])).status_code, 404)


@override_settings(GARMIN_ENABLED=True, HEVY_ENABLED=True)
class CommandOutputTests(TestCase):
    """Komendy mówią, co zrobiły - wcześniej zły --user kończył się ciszą."""

    def setUp(self):
        self.user = User.objects.create_user('Dawid_T', password='x')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(GARMIN_TOKEN_DIR=self.tmp.name)
        override.enable()
        self.addCleanup(override.disable)

    def run_command(self, *args, name='sync_garmin'):
        out = StringIO()
        call_command(name, *args, stdout=out, stderr=out)
        return out.getvalue()

    def connect(self):
        Path(self.tmp.name, str(self.user.pk)).mkdir()
        Path(self.tmp.name, str(self.user.pk), 'garmin_tokens.json').write_text('{}')

    def test_unknown_user_lists_logins(self):
        with self.assertRaisesMessage(CommandError, 'Loginy na stronie: Dawid_T'):
            self.run_command('--health-report', '--user', 'dawid')
        with self.assertRaisesMessage(CommandError, 'Nie ma użytkownika'):
            self.run_command('--user', 'ania', name='sync_hevy')

    def test_login_is_case_insensitive_and_not_connected_is_reported(self):
        output = self.run_command('--user', 'dawid_t')
        self.assertIn('nie jest połączone', output)
        self.assertIn(str(Path(self.tmp.name, str(self.user.pk))), output)
        self.assertIn('Nikt nie ma połączonego Garmina', self.run_command())
        self.assertIn('Nikt (z wybranych) nie ma klucza API Hevy', self.run_command(name='sync_hevy'))

    def test_health_report_without_connection(self):
        DailyHealth.objects.create(user=self.user, date=timezone.localdate(), steps=1234, raw={'summary': {'totalSteps': 1234}})
        output = self.run_command('--health-report', '--user', 'Dawid_T')
        self.assertIn('NIE jest połączony', output)
        self.assertIn('1234', output)

    def test_sync_prints_health_coverage(self):
        self.connect()
        today = timezone.localdate()
        for offset in (0, 1, 2, 6):
            DailyHealth.objects.create(user=self.user, date=today - timedelta(days=offset), steps=1000)
        with mock.patch.object(garmin, 'sync_all', return_value=2):
            output = self.run_command('--user', 'Dawid_T')
        self.assertIn('nowych treningów 2', output)
        self.assertIn('z danymi 4', output)
        expected = garmin_health._ranges([today - timedelta(days=d) for d in (3, 4, 5)])
        self.assertIn(expected, output)

    def test_ranges(self):
        self.assertEqual(garmin_health._ranges([date(2026, 10, 18), date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 24)]),
                         '18–20 paź, 24 paź')

