"""Logika składników w przeglądarce (cooking/static/cooking/js/recipe-ingredients.js).

Uruchamiana w Node; bez Node (np. obraz na Raspberry Pi) testy są pomijane.
"""
import json
import shutil
import subprocess
from pathlib import Path

from django.test import SimpleTestCase
from unittest import skipUnless

SCRIPT = Path(__file__).resolve().parent / 'static' / 'cooking' / 'js' / 'recipe-ingredients.js'
NODE = shutil.which('node')
INDEX = [
    {'name': 'Mięso mielone', 'unit': 'g', 'category': 'Mięso i ryby', 'kind': 'product'},
    {'name': 'Cebula', 'unit': 'szt', 'category': 'Warzywa i owoce', 'kind': 'product'},
    {'name': 'Sól', 'unit': 'g', 'category': 'Przyprawy', 'kind': 'product'},
    {'name': 'Jogurt naturalny', 'unit': 'g', 'category': 'Nabiał', 'kind': 'group'},
    {'name': 'Jogurt naturalny Pilos', 'unit': 'g', 'category': 'Nabiał', 'kind': 'product'},
    {'name': 'Mąka pszenna', 'unit': 'kg', 'category': 'Produkty suche', 'kind': 'product'},
]


@skipUnless(NODE, 'brak Node.js')
class RecipeIngredientsJsTests(SimpleTestCase):
    def run_js(self, expression):
        program = (
            f'const R = require({json.dumps(str(SCRIPT))});'
            f'const index = {json.dumps(INDEX)};'
            f'process.stdout.write(JSON.stringify({expression}));'
        )
        result = subprocess.run([NODE, '-e', program], capture_output=True, text=True, timeout=20, check=True)
        return json.loads(result.stdout)

    def parse(self, text):
        return [
            [item['name'], item['quantity'], item['unit']]
            for item in self.run_js(f'R.parseList({json.dumps(text)}, index)')
        ]

    def test_parses_common_list_formats(self):
        self.assertEqual(self.parse(
            '- 500 g mięsa mielonego\n2 cebule\n1/2 cebuli\nMąka pszenna: 300 g\n'
            '• Jogurt naturalny - 1 opak.\n1½ kg ziemniaków\n20 dag sera\n1. 200 ml śmietany'
        ), [
            ['Mięso mielone', '500', 'g'],
            ['Cebula', '2', 'szt'],
            ['Cebula', '0,5', 'szt'],
            ['Mąka pszenna', '300', 'g'],
            ['Jogurt naturalny', '1', 'opak'],
            ['Ziemniaków', '1,5', 'kg'],
            ['Sera', '200', 'g'],
            ['Śmietany', '200', 'ml'],
        ])

    def test_kitchen_measures_and_no_amount(self):
        items = self.run_js('R.parseList("2 łyżki oliwy\\n1 1/2 szklanki mleka\\nszczypta soli\\nsól do smaku\\npieprz\\n1 łyżeczka cukru", index)')
        self.assertEqual([(item['quantity'], item['unit']) for item in items], [
            ('2', 'lyzka'), ('1,5', 'szklanka'), ('1', 'szczypta'), ('', 'do_smaku'), ('', 'do_smaku'), ('1', 'lyzeczka'),
        ])
        self.assertEqual(items[2]['name'], 'Sól')
        self.assertIn('do smaku', items[4]['note'])

    def test_amount_text_declension(self):
        self.assertEqual(self.run_js('["1","2","5","12","22","0,5"].map(q => R.amountText(q, "lyzka"))'),
                         ['1 łyżka', '2 łyżki', '5 łyżek', '12 łyżek', '22 łyżki', '0,5 łyżki'])
        self.assertEqual(self.run_js('[R.amountText("", "do_smaku"), R.amountText("3", "szczypta"), R.amountText("200", "g")]'),
                         ['do smaku', '3 szczypty', '200 g'])

    def test_search_closest_and_exact(self):
        self.assertEqual(self.run_js('R.search("ceb", index).map(e => e.name)'), ['Cebula'])
        self.assertEqual(
            self.run_js('R.search("jog", index).map(e => e.name)'),
            ['Jogurt naturalny', 'Jogurt naturalny Pilos'],  # grupa przed marką
        )
        self.assertEqual(self.run_js('R.closest("mięsa mielonego", index).entry.name'), 'Mięso mielone')
        self.assertEqual(self.run_js('R.closest("Cebul", index).entry.name'), 'Cebula')
        self.assertIsNone(self.run_js('R.closest("Pomidor", index)'))
        self.assertEqual(self.run_js('R.exact("  CEBULA ", index).name'), 'Cebula')
        self.assertIsNone(self.run_js('R.exact("Mieso mielone", index)'))  # „Gotuj” wymaga ogonków

    def test_category_rules_and_units(self):
        rules = [['Dla zwierząt', ['karma', 'dla kota']], ['Kosmetyki', ['szampon*']]]
        self.assertEqual(self.run_js(f'R.categoryFor("Karma dla kota", {json.dumps(rules)})'), 'Dla zwierząt')
        self.assertEqual(self.run_js(f'R.categoryFor("Szampony", {json.dumps(rules)})'), 'Kosmetyki')
        self.assertEqual(self.run_js(f'R.categoryFor("Serwetki", {json.dumps(rules)})'), '')
        self.assertEqual(self.run_js('[R.sameFamily("g", "kg"), R.sameFamily("ml", "g"), R.sameFamily("szt", "szt")]'),
                         [True, False, True])
