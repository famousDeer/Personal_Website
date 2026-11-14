"""Dziennik posiłków: co domownik zjadł danego dnia, kcal i makro, cel dzienny.

Dziennik jest osobisty (per request.user) - w odróżnieniu od wspólnej
spiżarni. Pozycję dodaje się tak:
- wyszukiwarka (DiarySearchView + static/cooking/js/diary.js): produkty
  spiżarni, przepisy i baza produktów spożywczych (Food) w jednej liście;
  wybrana pozycja przychodzi jako product=/recipe=/food= z ilością i miarą;
- „Ostatnio i najczęściej”: repeat=<id wpisu> kopiuje wcześniejszą pozycję;
- sama nazwa (bez JS): produkt spiżarni albo pozycja bazy o tej nazwie,
  a z jednostką „porcja” - przepis;
- dowolna nazwa + kcal wpisane ręcznie (jedzenie na mieście).
Własny produkt do bazy dodaje FoodCreateView.
"""
from datetime import timedelta
from collections import Counter
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from utils.navigation import redirect_back, safe_next
from utils.tools import parse_date_input
from utils.undo import delete_with_undo

from .models import (
    UNIT_GLASS,
    UNIT_GRAM,
    UNIT_MILLILITER,
    UNIT_PACKAGE,
    UNIT_PIECE,
    UNIT_TABLESPOON,
    UNIT_TEASPOON,
    Food,
    MealEntry,
    NutritionGoal,
    PantryProduct,
    Recipe,
)
from .services.foods import food_from_catalog_entry, food_grams, search_foods
from .services.nutrition import (
    REASON_UNIT,
    NutritionIndex,
    Nutrients,
    food_nutrients,
    grams,
    named_nutrients,
    parse_nutrient,
    recipe_portion,
)
from .services.pantry_names import product_label
from .services.product_catalog import (
    SUPPORTED_BARCODE_PATTERN,
    CatalogUnavailable,
    lookup_open_food_facts,
    search_open_food_facts,
)
from .services.receipt_parser import normalize_text
from .services.recipe_units import VOLUME_UNITS, amount_text

MAX_GOAL_KCAL = 20000
MAX_ENTRY_KCAL = Decimal('20000')
DIARY_UNITS = [
    (UNIT_GRAM, 'g'),
    (UNIT_MILLILITER, 'ml'),
    (UNIT_PIECE, 'szt.'),
    (UNIT_PACKAGE, 'opak.'),
    (UNIT_TABLESPOON, 'łyżka'),
    (UNIT_TEASPOON, 'łyżeczka'),
    (UNIT_GLASS, 'szklanka'),
    (MealEntry.UNIT_PORTION, 'porcja przepisu'),
]
UNIT_LABELS = dict(DIARY_UNITS)
SEARCH_LIMIT = 15
RECENT_DAYS = 60
RECENT_LIMIT = 8
MEALS = dict(MealEntry.MEAL_CHOICES)


def _day(value):
    try:
        return parse_date_input(value or '')
    except ValueError:
        return timezone.localdate()


def _diary_url(day):
    url = reverse('cooking:diary')
    return url if day == timezone.localdate() else f'{url}?date={day.isoformat()}'


def default_meal(now=None):
    hour = (now or timezone.localtime()).hour
    if hour < 11:
        return MealEntry.MEAL_BREAKFAST
    if hour < 16:
        return MealEntry.MEAL_LUNCH
    return MealEntry.MEAL_DINNER


def _amount(entry):
    if entry.quantity is None:
        return ''
    if entry.unit == MealEntry.UNIT_PORTION:
        return f'{entry.quantity.normalize():f}'.replace('.', ',') + ' × porcja'
    return amount_text(entry.quantity, entry.unit)


def _sum(entries):
    total = Nutrients()
    for entry in entries:
        total += Nutrients(entry.kcal, entry.protein, entry.fat, entry.carbs)
    return total


class DiaryView(LoginRequiredMixin, View):
    def get(self, request):
        day = _day(request.GET.get('date'))
        today = timezone.localdate()
        entries = list(MealEntry.objects.filter(user=request.user, date=day))
        for entry in entries:
            entry.amount = _amount(entry)
        meals = [
            {'label': label, 'entries': rows, 'total': _sum(rows)}
            for code, label in MealEntry.MEAL_CHOICES
            if (rows := [entry for entry in entries if entry.meal == code])
        ]
        total = _sum(entries)
        goal = NutritionGoal.objects.filter(user=request.user).values_list('kcal', flat=True).first() or 0

        week_start = day - timedelta(days=6)
        by_day = dict(
            MealEntry.objects.filter(user=request.user, date__range=(week_start, day))
            .values_list('date').annotate(kcal=Sum('kcal'))
        )
        scale = max([goal, *by_day.values()] or [0]) or 1
        week = []
        for offset in range(7):
            current = week_start + timedelta(days=offset)
            kcal = by_day.get(current, Decimal('0'))
            week.append({
                'date': current,
                'kcal': kcal,
                'url': _diary_url(current),
                'percent': int(kcal * 100 / scale),
                'over': bool(goal) and kcal > goal,
                'current': current == day,
            })

        return render(request, 'cooking/diary.html', {
            'day': day,
            'is_today': day == today,
            'prev_url': _diary_url(day - timedelta(days=1)),
            'next_url': _diary_url(day + timedelta(days=1)) if day < today else '',
            'today_url': reverse('cooking:diary'),
            'meals': meals,
            'total': total,
            'goal': goal,
            'remaining': goal - total.kcal if goal else None,
            'goal_percent': min(int(total.kcal * 100 / goal), 100) if goal else 0,
            'week': week,
            'meal_choices': MealEntry.MEAL_CHOICES,
            'default_meal': default_meal(),
            'units': DIARY_UNITS,
            'recent': recent_entries(request.user),
            'off_enabled': settings.OPEN_FOOD_FACTS_ENABLED,
        })


def recent_entries(user):
    """Najczęstsze i ostatnie pozycje (ta sama nazwa i ilość) z 60 dni -
    do dodania jednym stuknięciem. Najnowszy wpis reprezentuje grupę."""
    since = timezone.localdate() - timedelta(days=RECENT_DAYS)
    latest, counts = {}, Counter()
    for entry in MealEntry.objects.filter(user=user, date__gte=since).order_by('-date', '-id')[:400]:
        key = (entry.name.casefold(), entry.quantity, entry.unit)
        counts[key] += 1
        latest.setdefault(key, entry)
    keys = sorted(latest, key=lambda key: (-counts[key], -latest[key].pk))[:RECENT_LIMIT]
    rows = [latest[key] for key in keys]
    for entry in rows:
        entry.amount = _amount(entry)
    return rows


def _numbers(nutrients):
    return {
        'kcal': float(nutrients.kcal), 'protein': float(nutrients.protein),
        'fat': float(nutrients.fat), 'carbs': float(nutrients.carbs),
    }


def _units(weigh, base=UNIT_GRAM):
    """Miary do wyboru: [{value, label, grams}] - tylko te, które da się przeliczyć."""
    options = []
    for unit in (UNIT_PIECE, UNIT_PACKAGE, base, UNIT_TABLESPOON, UNIT_TEASPOON, UNIT_GLASS):
        weight = weigh(unit)
        if weight:
            options.append({'value': unit, 'label': UNIT_LABELS[unit], 'grams': float(weight)})
    return options


def _result(kind, obj_id, name, note, per_100, units):
    # Domyślnie pierwsza miara domowa (1 szt.), a bez niej 100 g.
    first = units[0]
    return {
        'kind': kind, 'id': obj_id, 'name': name, 'note': note, 'per_100': _numbers(per_100), 'units': units,
        'quantity': 100 if first['value'] in (UNIT_GRAM, UNIT_MILLILITER) else 1,
    }


def _product_result(index, product):
    """Wynik dla produktu spiżarni albo None, gdy nie ma wartości odżywczych."""
    per_100 = index.per_100(product)
    if per_100 is None:
        return None
    food = index.food(product.name)
    base = UNIT_MILLILITER if product.unit in VOLUME_UNITS else UNIT_GRAM
    units = _units(lambda unit: grams(1, unit, product) or (food_grams(1, unit, food) if food else None), base)
    return _result(
        'product', product.pk, product.label, f'{per_100.kcal:.0f} kcal / 100 {base} · spiżarnia', per_100, units,
    )


def _food_result(food):
    per_100 = Nutrients(food.kcal_100, food.protein_100, food.fat_100, food.carbs_100)
    return _result(
        'food', food.pk, food.name, f'{food.kcal_100:.0f} kcal / 100 g', per_100,
        _units(lambda unit: food_grams(1, unit, food)),
    )


class DiarySearchView(LoginRequiredMixin, View):
    """Wyszukiwarka dziennika: spiżarnia, przepisy i baza produktów (JSON)."""

    def get(self, request):
        words = normalize_text(request.GET.get('q', '')).split()
        if not words:
            return JsonResponse({'results': []})
        index = NutritionIndex()
        results, taken = [], set()

        def matches(text):
            key = normalize_text(text)
            return all(word in key for word in words)

        for product in sorted(index.products(), key=lambda product: product.label.casefold()):
            result = _product_result(index, product) if matches(product.label) else None
            if result is None:
                continue
            results.append(result)
            taken.add(normalize_text(product.name))

        for recipe in Recipe.objects.prefetch_related('ingredient_items'):
            if not matches(recipe.title):
                continue
            portion, _ = recipe_portion(recipe, index)
            if portion is None:
                continue
            # „Porcja” liczy się jak 100 g produktu o wartościach porcji.
            results.append(_result(
                'recipe', recipe.pk, recipe.title, f'{portion.kcal:.0f} kcal / porcja · przepis', portion,
                [{'value': MealEntry.UNIT_PORTION, 'label': 'porcja', 'grams': 100.0}],
            ))

        for food in search_foods(' '.join(words), SEARCH_LIMIT):
            if food.search_key in taken:
                continue  # produkt spiżarni o tej nazwie ma pierwszeństwo
            results.append(_food_result(food))
        return JsonResponse({'results': results[:SEARCH_LIMIT]})


class DiaryOffSearchView(LoginRequiredMixin, View):
    """Produkty z etykietą po nazwie, z Open Food Facts (JSON). Wybrany
    produkt diary.js pobiera potem po kodzie przez DiaryBarcodeView."""

    def get(self, request):
        try:
            found = search_open_food_facts(request.GET.get('q', ''))
        except CatalogUnavailable:
            return JsonResponse({'results': [], 'error': 'Open Food Facts teraz nie odpowiada. Spróbuj za chwilę.'})
        return JsonResponse({'results': [
            {
                'barcode': item['barcode'],
                'name': product_label(item['name'], item['brand']),
                'note': ' · '.join(filter(None, [item['quantity_text'], f'{item["kcal_100"]:.0f} kcal / 100 g'])),
            }
            for item in found
        ]})


class DiaryBarcodeView(LoginRequiredMixin, View):
    """Kod kreskowy w dzienniku: produkt spiżarni, pozycja bazy albo Open
    Food Facts - wtedy produkt zapisuje się w bazie produktów (Food)."""

    def get(self, request):
        from .views import normalize_pantry_barcode
        try:
            barcode = normalize_pantry_barcode(request.GET.get('barcode'))
        except ValueError as exc:
            return JsonResponse({'found': False, 'message': str(exc)}, status=400)
        product = PantryProduct.objects.filter(barcode=barcode).first()
        result = _product_result(NutritionIndex(), product) if product else None
        name = product.label if product else ''
        if result is None:
            food = Food.objects.filter(barcode=barcode).first()
            if food is None:
                entry = lookup_open_food_facts(barcode).entry
                food = food_from_catalog_entry(entry)
                name = name or (entry.product_name if entry else '')
            if food is not None:
                result = _food_result(food)
        if result is None:
            message = (
                f'„{name}” nie ma wartości odżywczych w Open Food Facts.' if name
                else f'Nie znam kodu {barcode}.'
            )
            return JsonResponse({'found': False, 'barcode': barcode, 'name': name, 'message': message})
        return JsonResponse({'found': True, 'result': result})


class DiaryAddView(LoginRequiredMixin, View):
    def post(self, request):
        post = request.POST
        day = _day(post.get('date'))
        try:
            entry = self._build(request)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect(_diary_url(day))
        entry.user = request.user
        entry.date = day
        entry.meal = post.get('meal') if post.get('meal') in MEALS else default_meal()
        entry.save()
        messages.success(
            request,
            f'Dodano do dziennika: {entry.name} – {entry.kcal:.0f} kcal ({MEALS[entry.meal].lower()}).',
        )
        return redirect(_diary_url(day))

    def _build(self, request):
        post = request.POST
        if post.get('repeat'):
            # „Ostatnio i najczęściej”: ta sama pozycja jeszcze raz.
            source = get_object_or_404(MealEntry, pk=post.get('repeat'), user=request.user)
            return MealEntry(
                name=source.name, quantity=source.quantity, unit=source.unit, kcal=source.kcal,
                protein=source.protein, fat=source.fat, carbs=source.carbs,
                product=source.product, recipe=source.recipe, food=source.food,
            )

        quantity = parse_nutrient(post.get('quantity'), 'Ilość', Decimal('99999'))
        if quantity is not None and quantity <= 0:
            raise ValueError('Ilość musi być większa od zera.')
        unit = post.get('unit') or UNIT_GRAM
        if unit not in dict(DIARY_UNITS):
            raise ValueError('Wybierz poprawną jednostkę.')

        if post.get('recipe'):
            recipe = get_object_or_404(Recipe, pk=post.get('recipe'))
            return self._recipe_entry(recipe, quantity or Decimal('1'))

        if post.get('food') or post.get('product'):
            # Pozycja wybrana z wyszukiwarki.
            if quantity is None:
                raise ValueError('Podaj ilość.')
            if post.get('food'):
                food = get_object_or_404(Food, pk=post.get('food'))
                nutrients, reason = food_nutrients(food, quantity, unit)
                name, links = food.name, {'food': food}
            else:
                product = get_object_or_404(PantryProduct, pk=post.get('product'))
                nutrients, reason, _, _ = named_nutrients(NutritionIndex(), product.label, quantity, unit)
                name, links = product.label, {'product': product}
            if nutrients is None:
                raise ValueError(f'„{name}”: {reason}. Podaj ilość w gramach.')
            return self._entry(name, quantity, unit, nutrients, **links)

        name = post.get('name', '').strip()[:255]
        if not name:
            raise ValueError('Podaj, co zjadłeś.')

        kcal = parse_nutrient(post.get('kcal'), 'Kcal', MAX_ENTRY_KCAL)
        if kcal is not None:
            # Kcal wpisane ręcznie dotyczą całej zjedzonej ilości.
            macros = [
                parse_nutrient(post.get(field), label) or Decimal('0')
                for field, label in (('protein', 'Białko'), ('fat', 'Tłuszcz'), ('carbs', 'Węglowodany'))
            ]
            return self._entry(name, quantity, unit if quantity is not None else '', Nutrients(kcal, *macros))

        if unit == MealEntry.UNIT_PORTION:
            recipe = Recipe.objects.filter(title__iexact=name).first()
            if recipe is None:
                raise ValueError(f'Nie ma przepisu „{name}”. Wybierz przepis z podpowiedzi albo wpisz kcal ręcznie.')
            return self._recipe_entry(recipe, quantity or Decimal('1'))

        if quantity is None:
            raise ValueError('Podaj ilość albo wpisz kcal ręcznie.')
        nutrients, reason, product, food = named_nutrients(NutritionIndex(), name, quantity, unit)
        if product is None and food is None:
            raise ValueError(f'Nie znam produktu „{name}”. Wybierz go z listy, dodaj własny albo wpisz kcal ręcznie.')
        label = product.label if product else food.name
        if nutrients is None:
            hint = (
                'uzupełnij wagę sztuki w edycji produktu albo podaj ilość w gramach'
                if reason == REASON_UNIT else 'uzupełnij je w edycji produktu albo wpisz kcal ręcznie'
            )
            raise ValueError(f'„{label}”: {reason} – {hint}.')
        return self._entry(label, quantity, unit, nutrients, product=product, food=None if product else food)

    def _recipe_entry(self, recipe, portions):
        portion, _ = recipe_portion(recipe, NutritionIndex())
        if portion is None:
            raise ValueError(
                f'Przepis „{recipe.title}” nie ma kalorii: uzupełnij wartości odżywcze składników '
                'w spiżarni albo wpisz kcal na porcję w przepisie.'
            )
        return self._entry(recipe.title, portions, MealEntry.UNIT_PORTION, portion.scaled(portions), recipe=recipe)

    @staticmethod
    def _entry(name, quantity, unit, nutrients, **links):
        nutrients = nutrients.rounded()
        if nutrients.kcal > MAX_ENTRY_KCAL:
            raise ValueError('Za dużo kalorii w jednej pozycji – sprawdź ilość i jednostkę.')
        return MealEntry(
            name=name, quantity=quantity, unit=unit,
            kcal=nutrients.kcal, protein=nutrients.protein, fat=nutrients.fat, carbs=nutrients.carbs,
            **links,
        )


class DiaryDeleteView(LoginRequiredMixin, View):
    def post(self, request, entry_id):
        entry = get_object_or_404(MealEntry, pk=entry_id, user=request.user)
        day, name = entry.date, entry.name
        delete_with_undo(
            request, entry, f'Usunięto z dziennika: {name}.',
            restored_message=f'Przywrócono w dzienniku: {name}.',
        )
        return redirect_back(request, _diary_url(day))


class DiaryGoalView(LoginRequiredMixin, View):
    def post(self, request):
        day = _day(request.POST.get('date'))
        raw = request.POST.get('kcal', '').strip().replace(' ', '')
        try:
            kcal = int(raw or 0)
            if kcal < 0 or kcal > MAX_GOAL_KCAL:
                raise ValueError
        except ValueError:
            messages.error(request, f'Cel dzienny: podaj liczbę od 0 do {MAX_GOAL_KCAL}.')
            return redirect(_diary_url(day))
        NutritionGoal.objects.update_or_create(user=request.user, defaults={'kcal': kcal})
        messages.success(request, f'Cel dzienny: {kcal} kcal.' if kcal else 'Wyłączono cel dzienny.')
        return redirect(_diary_url(day))


class FoodCreateView(LoginRequiredMixin, View):
    """Własny produkt w bazie produktów spożywczych - wspólny dla domu."""

    template_name = 'cooking/food_form.html'
    fields = [
        ('kcal_100', 'Kcal na 100 g'), ('protein_100', 'Białko'), ('fat_100', 'Tłuszcz'),
        ('carbs_100', 'Węglowodany'), ('piece_weight', 'Waga sztuki'),
        ('spoon_weight', 'Waga łyżki'), ('glass_weight', 'Waga szklanki'),
    ]

    def _render(self, request, values, status=200):
        return render(request, self.template_name, {
            'values': values, 'back_url': safe_next(request, reverse('cooking:diary')),
        }, status=status)

    def get(self, request):
        return self._render(request, {
            'name': request.GET.get('name', '').strip()[:160], 'barcode': request.GET.get('barcode', '').strip()[:64],
        })

    def post(self, request):
        post = request.POST
        name = post.get('name', '').strip()[:160]
        try:
            if not name:
                raise ValueError('Podaj nazwę produktu.')
            key = normalize_text(name)
            if Food.objects.filter(search_key=key).exists() or Food.objects.filter(name__iexact=name).exists():
                raise ValueError(f'W bazie jest już „{name}”.')
            numbers = {field: parse_nutrient(post.get(field), label) for field, label in self.fields}
            if numbers['kcal_100'] is None:
                raise ValueError('Podaj kcal na 100 g.')
            # Kod ze skanera: następny skan znajdzie ten produkt.
            barcode = post.get('barcode', '').strip()
            if not SUPPORTED_BARCODE_PATTERN.fullmatch(barcode) or Food.objects.filter(barcode=barcode).exists():
                barcode = ''
        except ValueError as exc:
            messages.error(request, str(exc))
            return self._render(request, post, status=400)
        for field in ('protein_100', 'fat_100', 'carbs_100'):
            numbers[field] = numbers[field] or Decimal('0')
        Food.objects.create(
            name=name, search_key=key, source=Food.SOURCE_OWN, created_by=request.user, barcode=barcode,
            **{field: None if value is None else value.quantize(Decimal('0.1')) for field, value in numbers.items()},
        )
        messages.success(request, f'Dodano do bazy produktów: {name}. Znajdziesz go w wyszukiwarce.')
        return redirect(safe_next(request, reverse('cooking:diary')))
