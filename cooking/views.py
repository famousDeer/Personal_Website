from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin # Ważne dla bezpieczeństwa klas
from django.utils import timezone

from .models import PantryMovement, PantryProduct, Recipe, RecipeStep, RecipeStepIngredient

# Stałe (Warto przenieść je do osobnego pliku constants.py w przyszłości)
KITCHEN_REGIONS = [
    "Kuchnia włoska", "Kuchnia japońska", "Kuchnia meksykańska", "Kuchnia chińska",
    "Kuchnia indyjska", "Kuchnia tajska", "Kuchnia francuska", "Kuchnia grecka",
    "Kuchnia hiszpańska", "Kuchnia amerykańska", "Kuchnia bliskowschodnia",
    "Kuchnia marokańska", "Kuchnia wietnamska", "Kuchnia polska", "Kuchnia śródziemnomorska"
]
MEAL_TYPES = ["Śniadania", "Lunche", "Obiady", "Kolacje", "Przekąski", "Desery", "Napoje i koktajle"]
DISH_TYPES = ["Pieczone", "Gotowane", "Smażone", "Grillowane", "Przygotowywane na parze", "Duszone", "Air fryer"]
PANTRY_CATEGORIES = [
    'Produkty suche', 'Nabiał', 'Warzywa i owoce', 'Mięso i ryby', 'Mrożonki',
    'Przyprawy', 'Konserwy', 'Napoje', 'Chemia domowa', 'Inne'
]


def parse_pantry_decimal(value, default='0'):
    if value in [None, '']:
        value = default
    try:
        parsed = Decimal(str(value).replace(',', '.'))
    except (InvalidOperation, ValueError):
        raise ValueError('Podaj poprawną liczbę.')
    if parsed < 0:
        raise ValueError('Ilość nie może być ujemna.')
    return parsed.quantize(Decimal('0.01'))


def validate_pantry_quantity_for_unit(quantity, unit):
    if unit == PantryProduct.UNIT_PIECE and quantity != quantity.to_integral_value():
        raise ValueError('Dla jednostki "szt." podaj liczbę całkowitą.')


def convert_pantry_quantity(quantity, source_unit, target_unit):
    if source_unit == target_unit:
        return quantity
    conversions = {
        (PantryProduct.UNIT_GRAM, PantryProduct.UNIT_KILOGRAM): Decimal('0.001'),
        (PantryProduct.UNIT_KILOGRAM, PantryProduct.UNIT_GRAM): Decimal('1000'),
        (PantryProduct.UNIT_MILLILITER, PantryProduct.UNIT_LITER): Decimal('0.001'),
        (PantryProduct.UNIT_LITER, PantryProduct.UNIT_MILLILITER): Decimal('1000'),
    }
    factor = conversions.get((source_unit, target_unit))
    if factor is None:
        raise ValueError('Jednostka ważenia nie pasuje do jednostki produktu w spiżarni.')
    return (quantity * factor).quantize(Decimal('0.01'))


def get_pantry_form_context(**extra_context):
    context = {
        'categories': PANTRY_CATEGORIES,
        'units': PantryProduct.UNIT_CHOICES,
        'today': timezone.localdate(),
    }
    context.update(extra_context)
    return context


def get_recipe_form_context(**extra_context):
    context = {
        'regions': KITCHEN_REGIONS,
        'meal_types': MEAL_TYPES,
        'dish_types': DISH_TYPES,
        'units': PantryProduct.UNIT_CHOICES,
        'pantry_categories': PANTRY_CATEGORIES,
    }
    context.update(extra_context)
    return context


def build_recipe_legacy_text(recipe):
    ingredient_lines = []
    instruction_lines = []
    for step in recipe.steps.prefetch_related('ingredients').all():
        if step.title:
            instruction_lines.append(f"<p><strong>Krok {step.order}: {step.title}</strong></p>")
        instruction_lines.append(f"<p>{step.instruction}</p>")
        if step.mix_after:
            instruction_lines.append("<p>Wymieszaj składniki.</p>")
        for ingredient in step.ingredients.all():
            ingredient_lines.append(f"<p>{ingredient.quantity} {ingredient.get_unit_display()} - {ingredient.name}</p>")
    return ''.join(ingredient_lines), ''.join(instruction_lines)


def save_recipe_structure(recipe, request):
    step_titles = request.POST.getlist('step_title')
    step_instructions = request.POST.getlist('step_instruction')
    step_durations = request.POST.getlist('step_duration_minutes')
    mixed_steps = set(request.POST.getlist('step_mix_after'))
    ingredient_steps = request.POST.getlist('ingredient_step')
    ingredient_names = request.POST.getlist('ingredient_name')
    ingredient_quantities = request.POST.getlist('ingredient_quantity')
    ingredient_units = request.POST.getlist('ingredient_unit')
    ingredient_categories = request.POST.getlist('ingredient_category')

    recipe.steps.all().delete()
    created_steps = []
    for index, instruction in enumerate(step_instructions):
        instruction = instruction.strip()
        title = step_titles[index].strip() if index < len(step_titles) else ''
        if not instruction and not title:
            continue
        duration = None
        if index < len(step_durations) and step_durations[index]:
            duration = int(step_durations[index])
        created_steps.append(RecipeStep.objects.create(
            recipe=recipe,
            order=len(created_steps) + 1,
            title=title,
            instruction=instruction or title,
            mix_after=str(index) in mixed_steps,
            duration_minutes=duration,
        ))

    if not created_steps:
        created_steps.append(RecipeStep.objects.create(
            recipe=recipe,
            order=1,
            title='Przygotowanie',
            instruction=request.POST.get('instructions', '').strip() or 'Przygotuj przepis krok po kroku.',
        ))

    ingredient_order_by_step = {}
    for index, raw_name in enumerate(ingredient_names):
        name = raw_name.strip()
        if not name:
            continue
        step_index = int(ingredient_steps[index]) if index < len(ingredient_steps) and ingredient_steps[index] else 0
        if step_index >= len(created_steps):
            step_index = len(created_steps) - 1
        quantity = parse_pantry_decimal(ingredient_quantities[index] if index < len(ingredient_quantities) else '0')
        unit = ingredient_units[index] if index < len(ingredient_units) else PantryProduct.UNIT_GRAM
        validate_pantry_quantity_for_unit(quantity, unit)
        ingredient_order_by_step[step_index] = ingredient_order_by_step.get(step_index, 0) + 1
        RecipeStepIngredient.objects.create(
            step=created_steps[step_index],
            order=ingredient_order_by_step[step_index],
            name=name,
            quantity=quantity,
            unit=unit,
            category=ingredient_categories[index].strip() if index < len(ingredient_categories) else '',
        )

    recipe.ingredients, recipe.instructions = build_recipe_legacy_text(recipe)
    recipe.save(update_fields=['ingredients', 'instructions', 'updated_at'])

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
        return render(request, 'cooking/add-recipe.html', get_recipe_form_context())

    @transaction.atomic
    def post(self, request):
        # Pobieranie danych z formularza
        title = request.POST.get('title')
        description = request.POST.get('description')
        ingredients = request.POST.get('ingredients') or ''
        instructions = request.POST.get('instructions') or ''
        
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
        recipe = Recipe.objects.create(
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
        save_recipe_structure(recipe, request)

        return redirect('cooking:recipe-list')

class EditRecipeView(LoginRequiredMixin, View):
    def get(self, request, recipe_id):
        # Pobieramy przepis, upewniając się, że należy do użytkownika
        recipe = get_object_or_404(Recipe.objects.prefetch_related('steps__ingredients'), id=recipe_id, user=request.user)
        recipe.kcal = str(recipe.kcal)
        context = get_recipe_form_context(recipe=recipe)
        return render(request, 'cooking/edit-recipe.html', context)

    @transaction.atomic
    def post(self, request, recipe_id):
        recipe = get_object_or_404(Recipe, id=recipe_id, user=request.user)
        
        # Aktualizacja pól
        recipe.title = request.POST.get('title')
        recipe.description = request.POST.get('description')
        recipe.ingredients = request.POST.get('ingredients') or ''
        recipe.instructions = request.POST.get('instructions') or ''
        recipe.portions = request.POST.get('portions')
        recipe.kcal = request.POST.get('kcal')
        recipe.preparation_time = request.POST.get('preparation_time')
        recipe.kitchen_region = request.POST.get('kitchen_region')
        recipe.meal_type = request.POST.get('meal_type')
        recipe.type_of_dish = request.POST.get('type_of_dish')
        
        if request.FILES.get('image'):
            recipe.image = request.FILES.get('image')
            
        recipe.save()
        save_recipe_structure(recipe, request)
        return redirect('cooking:recipe-list')

class DeleteRecipeView(LoginRequiredMixin, View):
    def post(self, request, recipe_id):
        recipe = get_object_or_404(Recipe, id=recipe_id, user=request.user)
        recipe.delete()
        return redirect('cooking:recipe-list')


class PantryListView(LoginRequiredMixin, View):
    def get(self, request):
        products = PantryProduct.objects.filter(user=request.user).prefetch_related('movements')
        search_query = request.GET.get('q', '').strip()
        status_filter = request.GET.get('status', '').strip()
        category_filter = request.GET.get('category', '').strip()

        if search_query:
            products = products.filter(Q(name__icontains=search_query) | Q(category__icontains=search_query))
        if category_filter:
            products = products.filter(category=category_filter)

        product_cards = []
        total_current_quantity = Decimal('0')
        today = timezone.localdate()

        for product in products:
            average_daily = product.average_daily_consumption()
            depletion_date = product.projected_depletion_date()
            restock_date = product.suggested_restock_date()
            days_left = (depletion_date - today).days if depletion_date else None
            restock_in_days = (restock_date - today).days if restock_date else None
            total_current_quantity += product.current_quantity

            card = {
                'product': product,
                'average_daily': average_daily,
                'depletion_date': depletion_date,
                'restock_date': restock_date,
                'days_left': days_left,
                'restock_in_days': restock_in_days,
                'recent_movements': product.movements.all()[:4],
            }
            if not status_filter or product.stock_status == status_filter:
                product_cards.append(card)

        grouped_cards = {}
        for card in product_cards:
            category = card['product'].category or 'Bez kategorii'
            grouped_cards.setdefault(category, []).append(card)

        category_order = PANTRY_CATEGORIES + ['Bez kategorii']
        ordered_categories = [
            category for category in category_order if category in grouped_cards
        ] + [
            category for category in sorted(grouped_cards) if category not in category_order
        ]
        product_groups = [
            {
                'name': category,
                'cards': grouped_cards[category],
                'count': len(grouped_cards[category]),
                'low_count': sum(
                    1 for card in grouped_cards[category]
                    if card['product'].stock_status in ['low', 'empty']
                ),
            }
            for category in ordered_categories
        ]

        movement_stats = PantryMovement.objects.filter(product__user=request.user).aggregate(
            total_consumed=Sum('quantity', filter=Q(movement_type=PantryMovement.CONSUME)),
            total_restocked=Sum('quantity', filter=Q(movement_type=PantryMovement.PURCHASE)),
            movement_count=Count('id'),
        )

        context = {
            'product_cards': product_cards,
            'product_groups': product_groups,
            'categories': PANTRY_CATEGORIES,
            'current_search': search_query,
            'current_status': status_filter,
            'current_category': category_filter,
            'product_count': products.count(),
            'low_stock_count': sum(1 for card in product_cards if card['product'].stock_status in ['low', 'empty']),
            'total_current_quantity': total_current_quantity,
            'total_consumed': movement_stats['total_consumed'] or Decimal('0'),
            'total_restocked': movement_stats['total_restocked'] or Decimal('0'),
            'movement_count': movement_stats['movement_count'] or 0,
            'today': today,
        }
        return render(request, 'cooking/pantry.html', context)


class AddPantryProductView(LoginRequiredMixin, View):
    def get(self, request):
        return render(request, 'cooking/pantry_form.html', get_pantry_form_context())

    def post(self, request):
        form_values = request.POST
        try:
            name = request.POST.get('name', '').strip()
            if not name:
                raise ValueError('Nazwa produktu jest wymagana.')

            unit = request.POST.get('unit') or PantryProduct.UNIT_PIECE
            current_quantity = parse_pantry_decimal(request.POST.get('current_quantity'))
            minimum_quantity = parse_pantry_decimal(request.POST.get('minimum_quantity'))
            validate_pantry_quantity_for_unit(current_quantity, unit)
            validate_pantry_quantity_for_unit(minimum_quantity, unit)
            restock_lead_days = int(request.POST.get('restock_lead_days') or 3)
            if restock_lead_days < 0:
                raise ValueError('Wyprzedzenie zakupu nie może być ujemne.')

            product = PantryProduct.objects.create(
                user=request.user,
                name=name,
                category=request.POST.get('category', '').strip(),
                unit=unit,
                current_quantity=current_quantity,
                minimum_quantity=minimum_quantity,
                restock_lead_days=restock_lead_days,
                notes=request.POST.get('notes', '').strip(),
            )
            if current_quantity > 0:
                PantryMovement.objects.create(
                    product=product,
                    movement_type=PantryMovement.PURCHASE,
                    quantity=current_quantity,
                    occurred_on=timezone.localdate(),
                    note='Stan początkowy',
                )
            messages.success(request, f'Dodano produkt: {product.name}.')
            return redirect('cooking:pantry')
        except Exception as exc:
            messages.error(request, f'Nie udało się dodać produktu: {exc}')
            return render(
                request,
                'cooking/pantry_form.html',
                get_pantry_form_context(form_values=form_values),
            )


class PantryMovementView(LoginRequiredMixin, View):
    def post(self, request, product_id):
        product = get_object_or_404(PantryProduct, id=product_id, user=request.user)
        movement_type = request.POST.get('movement_type')
        try:
            quantity = parse_pantry_decimal(request.POST.get('quantity'))
            if quantity <= 0:
                raise ValueError('Ilość musi być większa od zera.')
            validate_pantry_quantity_for_unit(quantity, product.unit)

            if movement_type == PantryMovement.CONSUME:
                product.current_quantity = max(Decimal('0.00'), product.current_quantity - quantity)
                message = f'Zapisano zużycie: {product.name}.'
            elif movement_type == PantryMovement.PURCHASE:
                product.current_quantity += quantity
                message = f'Uzupełniono produkt: {product.name}.'
            else:
                raise ValueError('Nieznany typ operacji.')

            product.save(update_fields=['current_quantity', 'updated_at'])
            PantryMovement.objects.create(
                product=product,
                movement_type=movement_type,
                quantity=quantity,
                occurred_on=timezone.localdate(),
                note=request.POST.get('note', '').strip(),
            )
            messages.success(request, message)
        except Exception as exc:
            messages.error(request, f'Nie udało się zapisać zmiany: {exc}')

        return redirect('cooking:pantry')


class CookView(LoginRequiredMixin, View):
    def get(self, request):
        selected_recipe = None
        recipe_id = request.GET.get('recipe')
        if recipe_id:
            selected_recipe = get_object_or_404(Recipe.objects.prefetch_related('steps__ingredients'), id=recipe_id)

        return render(request, 'cooking/cook.html', {
            'recipes': Recipe.objects.all(),
            'selected_recipe': selected_recipe,
            'pantry_products': PantryProduct.objects.filter(user=request.user),
            'units': PantryProduct.UNIT_CHOICES,
            'categories': PANTRY_CATEGORIES,
        })

    def post(self, request):
        recipe = None
        recipe_id = request.POST.get('recipe')
        if recipe_id:
            recipe = get_object_or_404(Recipe, id=recipe_id)

        product_names = request.POST.getlist('product_name')
        quantities = request.POST.getlist('quantity')
        units = request.POST.getlist('unit')
        categories = request.POST.getlist('category')

        consumed_count = 0
        created_count = 0
        errors = []

        for index, raw_name in enumerate(product_names):
            name = raw_name.strip()
            raw_quantity = quantities[index] if index < len(quantities) else ''
            source_unit = units[index] if index < len(units) else PantryProduct.UNIT_GRAM
            category = categories[index].strip() if index < len(categories) else ''

            if not name and not raw_quantity:
                continue
            if not name:
                errors.append(f'Wiersz {index + 1}: podaj nazwę produktu.')
                continue

            try:
                quantity = parse_pantry_decimal(raw_quantity)
                if quantity <= 0:
                    raise ValueError('Ilość musi być większa od zera.')

                product = PantryProduct.objects.filter(user=request.user, name__iexact=name).first()
                if product is None:
                    validate_pantry_quantity_for_unit(quantity, source_unit)
                    product = PantryProduct.objects.create(
                        user=request.user,
                        name=name,
                        category=category or 'Inne',
                        unit=source_unit,
                        current_quantity=Decimal('0.00'),
                        minimum_quantity=Decimal('0.00'),
                    )
                    movement_quantity = quantity
                    created_count += 1
                else:
                    movement_quantity = convert_pantry_quantity(quantity, source_unit, product.unit)
                    validate_pantry_quantity_for_unit(movement_quantity, product.unit)
                    product.current_quantity = max(Decimal('0.00'), product.current_quantity - movement_quantity)
                    if category and not product.category:
                        product.category = category
                    product.save(update_fields=['current_quantity', 'category', 'updated_at'])

                note = 'Gotowanie'
                if recipe:
                    note = f'Gotowanie: {recipe.title}'
                PantryMovement.objects.create(
                    product=product,
                    movement_type=PantryMovement.CONSUME,
                    quantity=movement_quantity,
                    occurred_on=timezone.localdate(),
                    note=note,
                )
                consumed_count += 1
            except Exception as exc:
                errors.append(f'{name}: {exc}')

        if consumed_count:
            messages.success(
                request,
                f'Gotowanie zapisane: odjęto {consumed_count} produktów. Dodano nowych produktów: {created_count}.',
            )
        if errors:
            for error in errors[:5]:
                messages.error(request, error)
            if len(errors) > 5:
                messages.error(request, f'Pozostałe błędy: {len(errors) - 5}.')
            if not consumed_count:
                return render(request, 'cooking/cook.html', {
                    'recipes': Recipe.objects.all(),
                    'selected_recipe': recipe,
                    'pantry_products': PantryProduct.objects.filter(user=request.user),
                    'units': PantryProduct.UNIT_CHOICES,
                    'categories': PANTRY_CATEGORIES,
                    'form_rows': zip(product_names, quantities, units, categories),
                })

        return redirect('cooking:pantry')
