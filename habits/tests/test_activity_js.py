"""Statystyki aktywności w przeglądarce (habits/static/habits/js/activity-stats.js).

Uruchamiane w Node; bez Node (np. obraz na Raspberry Pi) testy są pomijane.
"""
import json
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

SCRIPT = Path(__file__).resolve().parents[1] / 'static' / 'habits' / 'js' / 'activity-stats.js'
NODE = shutil.which('node')


def w(day, kind='run', **extra):
    data = {'uid': f'{kind}-{day}', 'kind': kind, 'date': day, 'time': '', 'sets': []}
    data.update(extra)
    return data


@skipUnless(NODE, 'brak Node.js')
class ActivityStatsJsTests(SimpleTestCase):
    def run_js(self, expression, workouts=()):
        program = (
            f'const S = require({json.dumps(str(SCRIPT))});'
            f'const list = {json.dumps(list(workouts))};'
            f'process.stdout.write(JSON.stringify({expression}));'
        )
        result = subprocess.run([NODE, '-e', program], capture_output=True, text=True, timeout=20, check=True)
        return json.loads(result.stdout)

    def test_week_starts_on_monday(self):
        # 2026-10-24 to sobota, poniedziałek tego tygodnia: 2026-10-19.
        self.assertEqual(self.run_js('S.isoFromDay(S.weekStart(S.dayNumber("2026-10-24")))'), '2026-10-19')
        self.assertEqual(self.run_js('S.isoFromDay(S.weekStart(S.dayNumber("2026-10-19")))'), '2026-10-19')
        self.assertEqual(self.run_js('S.isoFromDay(S.weekStart(S.dayNumber("2026-10-25")))'), '2026-10-19')

    def test_goal_counts_days_and_streak_survives_current_week(self):
        workouts = [
            # dwa tygodnie temu: 3 dni -> cel
            w('2026-10-05'), w('2026-10-07', 'swim'), w('2026-10-09', 'gym'),
            # poprzedni tydzień: 3 dni (dwa treningi jednego dnia liczą się raz)
            w('2026-10-12'), w('2026-10-12', 'gym'), w('2026-10-14'), w('2026-10-16', 'swim'),
            # ten tydzień: na razie 1 dzień
            w('2026-10-20', 'swim'),
        ]
        info = self.run_js('S.weeklyGoal(list, 3, "2026-10-22")', workouts)
        self.assertEqual((info['done'], info['missing'], info['met']), (1, 2, False))
        self.assertEqual(info['streak'], 2)
        self.assertTrue(info['streakAtRisk'])
        self.assertEqual(info['bestStreak'], 2)
        self.assertEqual(info['daysLeft'], 4)   # czw–nd
        self.assertEqual(info['daysSinceLast'], 2)
        self.assertEqual(info['weekDays'][1]['kinds'], ['swim'])
        self.assertTrue(info['weekDays'][3]['isToday'])
        message = self.run_js('S.motivation(S.weeklyGoal(list, 3, "2026-10-22"))', workouts)
        self.assertIn('Brakuje 2 dni', message)
        self.assertIn('seria 2 tygodni', message)

    def test_streak_broken_by_empty_week_and_met_this_week(self):
        workouts = [w('2026-09-28'), w('2026-10-13'), w('2026-10-20'), w('2026-10-21')]
        info = self.run_js('S.weeklyGoal(list, 2, "2026-10-22")', workouts)
        self.assertTrue(info['met'])
        self.assertEqual(info['streak'], 1)   # poprzedni tydzień miał 1 dzień
        self.assertIn('Cel tygodnia osiągnięty', self.run_js('S.motivation(S.weeklyGoal(list, 2, "2026-10-22"))', workouts))

    def test_motivation_for_empty_and_lost_week(self):
        self.assertIn('Zacznij', self.run_js('S.motivation(S.weeklyGoal([], 3, "2026-10-22"))'))
        lost = self.run_js('S.motivation(S.weeklyGoal(list, 3, "2026-10-25"))', [w('2026-10-01')])
        self.assertIn('poniedziałek', lost)

    def test_heatmap(self):
        workouts = [
            w('2026-10-20', 'run', duration=1800),
            w('2026-10-20', 'gym', duration=3600),
            w('2025-01-01'),   # poza zakresem
        ]
        data = self.run_js('S.heatmap(list, "2026-10-22", 53)', workouts)
        self.assertEqual(len(data['weeks']), 53)
        self.assertEqual(data['activeDays'], 1)
        last = data['weeks'][-1]
        self.assertEqual(last['start'], '2026-10-19')
        tuesday = last['days'][1]
        self.assertEqual(tuesday['kind'], 'gym')   # dłuższy trening tego dnia
        self.assertEqual(len(tuesday['workouts']), 2)
        self.assertTrue(last['days'][6]['future'])

    def test_formatting_and_parsing(self):
        self.assertEqual(self.run_js('S.parseDuration("45")'), 2700)
        self.assertEqual(self.run_js('S.parseDuration("28:30")'), 1710)
        self.assertEqual(self.run_js('S.parseDuration("1:05:00")'), 3900)
        self.assertEqual(self.run_js('S.parseDuration("1h 5min")'), 3900)
        self.assertIsNone(self.run_js('S.parseDuration("")'))
        self.assertIsNone(self.run_js('S.parseDuration("abc") || null'))
        self.assertEqual(self.run_js('S.formatDuration(3900)'), '1:05:00')
        self.assertEqual(self.run_js('S.formatPace("run", 329)'), '5:29/km')
        self.assertEqual(self.run_js('S.formatDistance("run", 5210)'), '5,2 km')
        self.assertEqual(self.run_js('S.formatDistance("swim", 1500)'), '1500 m')   # pl: grupowanie od 5 cyfr
        summary = self.run_js('S.summarize(list[0])', [w('2026-10-20', duration=1710, distance=5200)])
        self.assertEqual(summary, 'Bieganie · 5,2 km · 28:30 · 5:29/km')
        self.assertEqual(
            self.run_js('S.setsSummary([["A",8,60],["A",8,60],["A",6,65],["B",12,null]])'),
            '2 × 8 × 60 kg, 6 × 65 kg, 12 powt.',
        )

    def test_month_comparison_uses_same_part_of_previous_month(self):
        workouts = [
            w('2026-10-02', distance=5000), w('2026-10-10', 'swim', distance=1000),
            w('2026-09-20', distance=8000), w('2026-09-25', distance=9000),   # po 24. dniu - poza porównaniem
        ]
        data = self.run_js('S.monthComparison(list, "2026-10-24")', workouts)
        self.assertEqual(data['current']['runDistance'], 5000)
        self.assertEqual(data['current']['swimDistance'], 1000)
        self.assertEqual(data['current']['days'], 2)
        self.assertEqual(data['previous']['runDistance'], 8000)

    def test_progress_series_and_records(self):
        workouts = [
            w('2026-10-01', distance=5000, duration=1600),
            w('2026-10-05', distance=10000, duration=3000),
            w('2026-10-08', distance=3000, duration=840),   # szybkie, ale za krótkie na rekord tempa
            w('2026-10-02', 'gym', sets=[['Przysiad', 5, 80], ['Przysiad', 5, 85], ['Wiosłowanie', 10, 40]]),
            w('2026-10-09', 'gym', sets=[['Przysiad', 5, 90]]),
        ]
        volume = self.run_js('S.weeklyVolume(list, "run", "2026-10-09", 3)', workouts)
        self.assertEqual([point['value'] for point in volume], [0, 5000, 13000])
        paces = self.run_js('S.paceSeries(list, "run").map(p => Math.round(p.pace))', workouts)
        self.assertEqual(paces, [320, 300, 280])
        self.assertEqual(self.run_js('S.exerciseNames(list)', workouts), ['Przysiad', 'Wiosłowanie'])
        progress = self.run_js('S.exerciseProgress(list, "Przysiad")', workouts)
        self.assertEqual([(p['date'], p['maxWeight'], p['volume']) for p in progress],
                         [('2026-10-02', 85, 825), ('2026-10-09', 90, 450)])
        records = self.run_js('S.records(list)', workouts)
        self.assertEqual(records['run']['longest']['date'], '2026-10-05')
        self.assertEqual(records['run']['fastest']['date'], '2026-10-05')
        self.assertEqual([(r['exercise'], r['weight']) for r in records['gym']], [('Przysiad', 90), ('Wiosłowanie', 40)])
