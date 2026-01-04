from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin # Ważne dla bezpieczeństwa klas
from .models import Recipe

# Stałe (Warto przenieść je do osobnego pliku constants.py w przyszłości)
KITCHEN_REGIONS = [
    "Kuchnia włoska", "Kuchnia japońska", "Kuchnia meksykańska", "Kuchnia chińska",
    "Kuchnia indyjska", "Kuchnia tajska", "Kuchnia francuska", "Kuchnia grecka",
    "Kuchnia hiszpańska", "Kuchnia amerykańska", "Kuchnia bliskowschodnia",
    "Kuchnia marokańska", "Kuchnia wietnamska", "Kuchnia polska", "Kuchnia śródziemnomorska"
]
MEAL_TYPES = ["Śniadania", "Lunche", "Obiady", "Kolacje", "Przekąski", "Desery", "Napoje i koktajle"]
DISH_TYPES = ["Pieczone", "Gotowane", "Smażone", "Grillowane", "Przygotowywane na parze", "Duszone", "Air fryer"]

def index(request):
    return render(request, 'cooking/index.html')

class RecipeListView(LoginRequiredMixin, View):
    def get(self, request):
        # 1. Pobieramy wszystkie przepisy użytkownika
        recipes = Recipe.objects.all()

        # 2. Pobieramy parametry z URL
        region_filter = request.GET.get('region')
        meal_filter = request.GET.get('meal')
        type_filter = request.GET.get('type')
        search_query = request.GET.get('q') # Opcjonalnie: wyszukiwanie po nazwie

        # 3. Aplikujemy filtry
        if region_filter:
            recipes = recipes.filter(kitchen_region=region_filter)
        
        if meal_filter:
            recipes = recipes.filter(meal_type=meal_filter)
            
        if type_filter:
            recipes = recipes.filter(type_of_dish=type_filter)

        if search_query:
            recipes = recipes.filter(title__icontains=search_query)

        # 4. Przygotowujemy kontekst
        context = {
            'recipes': recipes,
            # Przekazujemy listy opcji do selectów
            'regions': KITCHEN_REGIONS,
            'meal_types': MEAL_TYPES,
            'dish_types': DISH_TYPES,
            # Przekazujemy wybrane wartości, żeby select pamiętał co wybrałeś
            'current_region': region_filter,
            'current_meal': meal_filter,
            'current_type': type_filter,
            'search_query': search_query,
        }
        
        return render(request, 'cooking/recipe-list.html', context)
    
class AddRecipeView(LoginRequiredMixin, View):
    def get(self, request):
        context = {
            'regions': KITCHEN_REGIONS,
            'meal_types': MEAL_TYPES,
            'dish_types': DISH_TYPES
        }
        return render(request, 'cooking/add-recipe.html', context)

    def post(self, request):
        # Pobieranie danych z formularza
        title = request.POST.get('title')
        description = request.POST.get('description')
        ingredients = request.POST.get('ingredients')
        instructions = request.POST.get('instructions')
        
        # Pobieranie liczb (z domyślnymi wartościami w razie błędu)
        try:
            portions = int(request.POST.get('portions', 1))
            kcal = int(request.POST.get('kcal', 0))
            preparation_time = int(request.POST.get('preparation_time', 5))
        except ValueError:
            portions = 1
            kcal = 0
            preparation_time = 5

        # Pobieranie opcji wyboru
        kitchen_region = request.POST.get('kitchen_region', '')
        meal_type = request.POST.get('meal_type', '')
        type_of_dish = request.POST.get('type_of_dish', '')
        
        # Obrazek
        image = request.FILES.get('image')

        # Tworzenie obiektu
        Recipe.objects.create(
            user=request.user,
            title=title,
            description=description,
            ingredients=ingredients,
            instructions=instructions,
            portions=portions,
            kcal=kcal,
            preparation_time=preparation_time,
            kitchen_region=kitchen_region,
            meal_type=meal_type,
            type_of_dish=type_of_dish,
            image=image
        )

        return redirect('cooking:recipe-list')

class EditRecipeView(LoginRequiredMixin, View):
    def get(self, request, recipe_id):
        # Pobieramy przepis, upewniając się, że należy do użytkownika
        recipe = get_object_or_404(Recipe, id=recipe_id, user=request.user)
        recipe.kcal = str(recipe.kcal)
        context = {
            'recipe': recipe,
            'regions': KITCHEN_REGIONS,
            'meal_types': MEAL_TYPES,
            'dish_types': DISH_TYPES
        }
        return render(request, 'cooking/edit-recipe.html', context)

    def post(self, request, recipe_id):
        recipe = get_object_or_404(Recipe, id=recipe_id, user=request.user)
        
        # Aktualizacja pól
        recipe.title = request.POST.get('title')
        recipe.description = request.POST.get('description')
        recipe.ingredients = request.POST.get('ingredients')
        recipe.instructions = request.POST.get('instructions')
        recipe.portions = request.POST.get('portions')
        recipe.kcal = request.POST.get('kcal')
        recipe.preparation_time = request.POST.get('preparation_time')
        recipe.kitchen_region = request.POST.get('kitchen_region')
        recipe.meal_type = request.POST.get('meal_type')
        recipe.type_of_dish = request.POST.get('type_of_dish')
        
        if request.FILES.get('image'):
            recipe.image = request.FILES.get('image')
            
        recipe.save()
        return redirect('cooking:recipe-list')

class DeleteRecipeView(LoginRequiredMixin, View):
    def post(self, request, recipe_id):
        recipe = get_object_or_404(Recipe, id=recipe_id, user=request.user)
        recipe.delete()
        return redirect('cooking:recipe-list')