"""Buduje foods.json (wbudowana lista produktów spożywczych) z danych USDA.

Źródło: FoodData Central, paczka SR Legacy (CSV) z fdc.nal.usda.gov.
Wejście to tabela „fdc_id, opis, kcal, białko, tłuszcz, węglowodany” (TSV)
zrobiona z food.csv i food_nutrient.csv (składniki 1008, 1003, 1004, 1005)
oraz foods_usda_map.txt: polska nazwa, frazy opisu USDA i miary domowe.
Spośród pasujących opisów wygrywa najkrótszy; wynik trzeba przejrzeć.

    python build_foods.py flat.tsv foods_usda_map.txt foods.json
"""
import sys, json
flat, mapping, out = sys.argv[1:4]
rows = [l.rstrip('\n').split('\t') for l in open(flat)]
result, bad = [], 0
for line in open(mapping):
    line = line.rstrip('\n')
    if not line or line.startswith('#'):
        continue
    pl, terms, piece, spoon, glass = line.split('|')
    if not terms:
        result.append({'name': pl, 'kcal': 0, 'protein': 0, 'fat': 0, 'carbs': 0, 'usda': ''})
        continue
    need = [t.strip().lower() for t in terms.split(';') if not t.strip().startswith('!')]
    deny = [t.strip()[1:].lower() for t in terms.split(';') if t.strip().startswith('!')]
    hits = [r for r in rows if all(t in r[1].lower() for t in need) and not any(t in r[1].lower() for t in deny) and r[2]]
    if not hits:
        print('BRAK', pl, '|', terms); bad += 1; continue
    hits.sort(key=lambda r: len(r[1]))
    r = hits[0]
    item = {'name': pl, 'kcal': float(r[2]), 'protein': float(r[3] or 0), 'fat': float(r[4] or 0), 'carbs': float(r[5] or 0), 'usda': r[1], 'fdc_id': int(r[0])}
    for key, value in (('piece', piece), ('spoon', spoon), ('glass', glass)):
        if value:
            item[key] = float(value)
    result.append(item)
    print(f"{pl[:26]:26} {r[2]:>5} {len(hits):>3}  {r[1][:95]}")
json.dump(result, open(out, 'w'), ensure_ascii=False, indent=0)
print(len(result), 'pozycji, braków', bad)
