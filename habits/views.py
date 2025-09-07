# habits/views.py 
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.views import View
from django.utils.decorators import method_decorator
from datetime import datetime
from .models import Habit, HabitRecord

def index(request):
    habits = Habit.objects.filter(user=request.user, is_active=True).order_by('start_date')
    context = {
        'habits': habits,
    }
    return render(request, 'habits/index.html', context)

def add_habit(request):
    HABITS_CATEGORY = [
        'Czytanie ksiazek', 'Kroki', 'Picie wody', 'Bieganie',
        'Yoga', 'Medytaca', 'Nauka'
    ]
    GOAL = ['Dziennie', 'Tygodniowo', 'Miesiecznie']
    if request.method == 'GET':
        context = {
            'habits_category': sorted(HABITS_CATEGORY),
            'default_date': timezone.now().date().strftime('%Y-%m-%d'),
            'days': GOAL,
        }
        return render(request, 'habits/add_habit.html', context)
    if request.method == 'POST':
        habit_name = request.POST.get('habit_name')
        description = request.POST.get('description', '')
        start_date = request.POST.get('start_date')
        end_date = request.POST.get('end_date')
        category = request.POST.get('category')
        running_days = request.POST.getlist('running_days')

        try:
            start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
            end_date = end_date if end_date else None
        except ValueError:
            messages.error(request, 'Nieprawidłowy format. Uzyj YYYY-MM-DD.')
            return redirect('habits:add')

        habit = Habit.objects.create(
            user=request.user,
            name=habit_name,
            description=description,
            start_date=start_date,
            end_date=end_date,
            category=category
        )
        messages.success(request, f'Pomyślnie dodano nawyk"{habit.name}"!')
        return redirect('habits:index')

    type_choice = [
        'Kroki', # Step tracking with a kroki, kroków 
        'Czas',  # Time tracking with a minutes
        'Waga',  # Weight tracking with a kg
        'Strony', # Page tracking with a pages
        'Woda', # Water tracking with a liters
        'Bieganie', # Running tracking with a km
    ]

    context = {
        'default_date': timezone.now().date().strftime('%Y-%m-%d'),
        'habits_category' : HABITS_CATEGORY,
        'type_choice': type_choice
        }
    return render(request, 'habits/add_habit.html', context)

def habit_list(request):
    try:
        habits = Habit.objects.filter(user=request.user).order_by('start_date')
        habit_time_left = {habit.id: (habit.end_date - timezone.now().date()).days if habit.end_date else 'N/A' for habit in habits}
        context = {
            'habits': habits,
            'habits_time_left': habit_time_left,
        }
    except Habit.DoesNotExist:
        messages.error(request, 'Nie masz żadnych nawyków.')
        context = {
            'habits': []
        }
    return render(request, 'habits/habits_list.html', context)

@method_decorator(login_required, name='dispatch')
class UpdateHabitView(View):
    def get(self, request, habit_id):
        habit = get_object_or_404(Habit, id=habit_id, user=request.user)
        context = {
            'habits_category' : HABITS_CATEGORY,
            'habit': habit
        }
        return render(request, 'habits/update_habit.html', context)

    @transaction.atomic
    def post(self, request, habit_id):
        habit = get_object_or_404(Habit, id=habit_id, user=request.user)
        habit.name = request.POST.get('habit_name')
        habit.description = request.POST.get('description', '')
        habit.start_date = request.POST.get('start_date')
        end_date = request.POST.get('end_date')
        habit.end_date = end_date if end_date else None
        habit.category = request.POST.get('category')
        habit.is_active = request.POST.get('is_active')
        try:
            habit.start_date = datetime.strptime(habit.start_date, '%Y-%m-%d').date()
            if habit.end_date:
                habit.end_date = datetime.strptime(habit.end_date, '%Y-%m-%d').date()
        except ValueError:
            messages.error(request, 'Nieprawidłowy format daty. Użyj YYYY-MM-DD.')
        habit.updated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        habit.save()
        messages.success(request, f'Pomyślnie zapisano zmiany dla nawyku "{habit.name}"!')
        return redirect('habits:list')
