"""Dziennik posiłków: co domownik zjadł danego dnia, kcal i makro, cel dzienny.

Dziennik jest osobisty (per request.user) - w odróżnieniu od wspólnej
spiżarni. Pozycję dodaje się na trzy sposoby:
- produkt ze spiżarni + ilość (g, ml, szt., opak.) - wartości liczy
  services/nutrition.py z danych produktu albo Open Food Facts;
- przepis + liczba porcji (jednostka „porcja” albo przycisk „Zjadłem porcję”
  na liście przepisów);
- dowolna nazwa + kcal wpisane ręcznie (jedzenie na mieście).
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from utils.navigation import redirect_back
from utils.tools import parse_date_input
from utils.undo import delete_with_undo

from .models import (
    UNIT_GRAM,
    UNIT_MILLILITER,
    UNIT_PACKAGE,
    UNIT_PIECE,
    MealEntry,
    NutritionGoal,
    PantryProduct,
    Recipe,
)
from .services.nutrition import (
    REASON_UNIT,
    NutritionIndex,
    Nutrients,
    parse_nutrient,
    product_nutrients,
    recipe_portion,
)
from .services.pantry_names import product_label
from .services.recipe_units import amount_text

MAX_GOAL_KCAL = 20000
MAX_ENTRY_KCAL = Decimal('20000')
DIARY_UNITS = [
    (UNIT_GRAM, 'g'),
    (UNIT_MILLILITER, 'ml'),
    (UNIT_PIECE, 'szt.'),
    (UNIT_PACKAGE, 'opak.'),
    (MealEntry.UNIT_PORTION, 'porcja przepisu'),
]
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

        names = {
            product_label(name, brand)
            for name, brand in PantryProduct.objects.values_list('name', 'brand')
        }
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
            'product_names': sorted(names, key=str.casefold),
            'recipe_titles': list(Recipe.objects.order_by('title').values_list('title', flat=True)),
        })


class DiaryAddView(LoginRequiredMixin, View):
    def post(self, request):
        post = request.POST
        day = _day(post.get('date'))
        try:
            entry = self._build(post)
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

    def _build(self, post):
        quantity = parse_nutrient(post.get('quantity'), 'Ilość', Decimal('99999'))
        if quantity is not None and quantity <= 0:
            raise ValueError('Ilość musi być większa od zera.')
        unit = post.get('unit') or UNIT_GRAM
        if unit not in dict(DIARY_UNITS):
            raise ValueError('Wybierz poprawną jednostkę.')

        if post.get('recipe'):
            recipe = get_object_or_404(Recipe, pk=post.get('recipe'))
            return self._recipe_entry(recipe, quantity or Decimal('1'))

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

        index = NutritionIndex()
        product = index.product(name)
        if product is None:
            raise ValueError(f'Nie ma produktu „{name}” w spiżarni. Wpisz kcal ręcznie.')
        if quantity is None:
            raise ValueError('Podaj ilość albo wpisz kcal ręcznie.')
        nutrients, reason = product_nutrients(index, product, quantity, unit)
        if nutrients is None:
            hint = (
                'uzupełnij wagę sztuki w edycji produktu albo podaj ilość w gramach'
                if reason == REASON_UNIT else 'uzupełnij je w edycji produktu albo wpisz kcal ręcznie'
            )
            raise ValueError(f'„{product.label}”: {reason} – {hint}.')
        return self._entry(product.label, quantity, unit, nutrients, product=product)

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
