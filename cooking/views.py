from decimal import Decimal, InvalidOperation, ROUND_CEILING

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin # Ważne dla bezpieczeństwa klas
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from .models import (
    PantryMovement,
    PantryProduct,
    Recipe,
    RecipeStep,
    RecipeStepIngredient,
    ShoppingList,
    ShoppingListItem,
)

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
ALLOWED_RECIPE_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
ALLOWED_RECIPE_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
MAX_RECIPE_IMAGE_SIZE = 5 * 1024 * 1024


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


def normalize_shopping_quantity(quantity, unit):
    quantity = quantity.quantize(Decimal('0.01'))
    if unit == PantryProduct.UNIT_PIECE:
        quantity = quantity.to_integral_value(rounding=ROUND_CEILING)
    return quantity


def find_user_pantry_product(user, name):
    return PantryProduct.objects.filter(user=user, name__iexact=name).first()


def build_shopping_suggestions(user):
    today = timezone.localdate()
    suggestions = []

    for product in PantryProduct.objects.filter(user=user).prefetch_related('movements'):
        restock_date = product.suggested_restock_date()
        restock_due = bool(restock_date and restock_date <= today)
        needs_stock = product.stock_status in ['empty', 'low']
        if not needs_stock and not restock_due:
            continue

        suggested_quantity = product.minimum_quantity - product.current_quantity
        if suggested_quantity <= 0:
            suggested_quantity = product.minimum_quantity
        if suggested_quantity <= 0:
            average = product.average_daily_consumption()
            suggested_quantity = average * Decimal(max(product.restock_lead_days, 1))
        if suggested_quantity <= 0:
            suggested_quantity = Decimal('1.00')

        reason = 'Niski stan'
        if product.current_quantity <= 0:
            reason = 'Brak w spiżarni'
        elif restock_due:
            reason = f'Kup do {restock_date.strftime("%d.%m")}'

        suggestions.append({
            'product': product,
            'quantity': normalize_shopping_quantity(suggested_quantity, product.unit),
            'reason': reason,
            'restock_date': restock_date,
        })

    return suggestions


def parse_shopping_items_from_request(request):
    names = request.POST.getlist('item_name')
    quantities = request.POST.getlist('item_quantity')
    units = request.POST.getlist('item_unit')
    categories = request.POST.getlist('item_category')
    notes = request.POST.getlist('item_note')
    rows = []
    errors = []

    for index, raw_name in enumerate(names):
        name = raw_name.strip()
        raw_quantity = quantities[index] if index < len(quantities) else ''
        raw_category = categories[index].strip() if index < len(categories) else ''
        note = notes[index].strip() if index < len(notes) else ''

        if not name and not raw_quantity and not raw_category and not note:
            continue
        if not name:
            errors.append(f'Wiersz {index + 1}: podaj nazwę produktu.')
            continue

        unit = units[index] if index < len(units) else PantryProduct.UNIT_PIECE
        try:
            quantity = parse_pantry_decimal(raw_quantity, default='1')
            if quantity <= 0:
                raise ValueError('Ilość musi być większa od zera.')
            validate_pantry_quantity_for_unit(quantity, unit)
        except ValueError as exc:
            errors.append(f'{name}: {exc}')
            continue

        product = find_user_pantry_product(request.user, name)
        category = raw_category or (product.category if product else '')
        rows.append({
            'name': name,
            'quantity': quantity,
            'unit': unit,
            'category': category,
            'note': note,
            'pantry_product': product,
        })

    return rows, errors


def get_pantry_form_context(**extra_context):
    context = {
        'categories': PANTRY_CATEGORIES,
        'units': PantryProduct.UNIT_CHOICES,
        'today': timezone.localdate(),
    }
    context.update(extra_context)
    return context


def get_shopping_form_context(request, **extra_context):
    context = {
        'categories': PANTRY_CATEGORIES,
        'units': PantryProduct.UNIT_CHOICES,
        'pantry_products': PantryProduct.objects.filter(user=request.user),
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


def validate_recipe_image(uploaded_file):
    if not uploaded_file:
        return None

    if uploaded_file.size > MAX_RECIPE_IMAGE_SIZE:
        raise ValueError('Zdjęcie przepisu może mieć maksymalnie 5 MB.')

    filename = uploaded_file.name.lower()
    extension = '.' + filename.rsplit('.', 1)[-1] if '.' in filename else ''
    content_type = getattr(uploaded_file, 'content_type', '')
    if extension not in ALLOWED_RECIPE_IMAGE_EXTENSIONS:
        raise ValueError('Zdjęcie musi być plikiem JPG, PNG albo WEBP.')
    if content_type and content_type not in ALLOWED_RECIPE_IMAGE_TYPES:
        raise ValueError('Zdjęcie ma nieobsługiwany typ MIME.')

    try:
        image = Image.open(uploaded_file)
        image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError('Nie udało się odczytać zdjęcia. Wgraj poprawny plik obrazu.') from exc
    finally:
        uploaded_file.seek(0)

    return uploaded_file


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

@login_required
def index(request):
    return render(request, 'cooking/index.html')

class RecipeListView(LoginRequiredMixin, View):
    def get(self, request):
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
        
        try:
            image = validate_recipe_image(request.FILES.get('image'))
        except ValueError as exc:
            messages.error(request, str(exc))
            return render(
                request,
                'cooking/add-recipe.html',
                get_recipe_form_context(form_values=request.POST),
            )

        # Tworzenie obiektu
        try:
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
        except Exception as exc:
            transaction.set_rollback(True)
            messages.error(request, f'Nie udało się dodać przepisu: {exc}')
            return render(
                request,
                'cooking/add-recipe.html',
                get_recipe_form_context(form_values=request.POST),
            )

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
            try:
                recipe.image = validate_recipe_image(request.FILES.get('image'))
            except ValueError as exc:
                messages.error(request, str(exc))
                return render(
                    request,
                    'cooking/edit-recipe.html',
                    get_recipe_form_context(recipe=recipe),
                )
            
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


class ShoppingListView(LoginRequiredMixin, View):
    def get(self, request):
        shopping_lists = ShoppingList.objects.filter(user=request.user).annotate(
            item_count=Count('items'),
            purchased_count=Count('items', filter=Q(items__is_purchased=True)),
        ).prefetch_related('items')
        for shopping_list in shopping_lists:
            shopping_list.progress_percent = int(
                (shopping_list.purchased_count / shopping_list.item_count) * 100
            ) if shopping_list.item_count else 0
        active_lists = [shopping_list for shopping_list in shopping_lists if shopping_list.status == ShoppingList.ACTIVE]
        completed_lists = [
            shopping_list
            for shopping_list in shopping_lists
            if shopping_list.status == ShoppingList.COMPLETED
        ][:6]
        suggestions = build_shopping_suggestions(request.user)

        context = {
            'active_lists': active_lists,
            'completed_lists': completed_lists,
            'suggestions': suggestions,
            'active_count': len(active_lists),
            'suggestions_count': len(suggestions),
            'total_items_count': sum(shopping_list.item_count for shopping_list in active_lists),
            'purchased_items_count': sum(shopping_list.purchased_count for shopping_list in active_lists),
        }
        return render(request, 'cooking/shopping.html', context)


class CreateShoppingListView(LoginRequiredMixin, View):
    def get(self, request):
        return render(request, 'cooking/shopping_form.html', get_shopping_form_context(request))

    @transaction.atomic
    def post(self, request):
        rows, errors = parse_shopping_items_from_request(request)
        title = request.POST.get('title', '').strip() or f'Lista zakupów {timezone.localdate():%d.%m.%Y}'

        if not rows:
            messages.error(request, 'Dodaj przynajmniej jedną pozycję do listy.')
            for error in errors[:4]:
                messages.error(request, error)
            return render(
                request,
                'cooking/shopping_form.html',
                get_shopping_form_context(request, form_values=request.POST, item_rows=list(zip(
                    request.POST.getlist('item_name'),
                    request.POST.getlist('item_quantity'),
                    request.POST.getlist('item_unit'),
                    request.POST.getlist('item_category'),
                    request.POST.getlist('item_note'),
                ))),
            )

        shopping_list = ShoppingList.objects.create(
            user=request.user,
            title=title,
            source=ShoppingList.MANUAL,
        )
        for row in rows:
            ShoppingListItem.objects.create(shopping_list=shopping_list, **row)

        if errors:
            messages.warning(request, f'Pominięto część pozycji: {len(errors)}.')
            for error in errors[:4]:
                messages.error(request, error)
        messages.success(request, f'Utworzono listę zakupów: {shopping_list.title}.')
        return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)


class GenerateShoppingListView(LoginRequiredMixin, View):
    @transaction.atomic
    def post(self, request):
        suggestions = build_shopping_suggestions(request.user)
        if not suggestions:
            messages.info(request, 'Nie znaleziono produktów wymagających uzupełnienia.')
            return redirect('cooking:shopping-list')

        shopping_list = ShoppingList.objects.create(
            user=request.user,
            title=f'Automatyczna lista {timezone.localdate():%d.%m.%Y}',
            source=ShoppingList.AUTOMATIC,
        )
        for suggestion in suggestions:
            product = suggestion['product']
            ShoppingListItem.objects.create(
                shopping_list=shopping_list,
                pantry_product=product,
                name=product.name,
                quantity=suggestion['quantity'],
                unit=product.unit,
                category=product.category,
                note=suggestion['reason'],
            )

        messages.success(request, f'Utworzono automatyczną listę z {len(suggestions)} pozycjami.')
        return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)


class ShoppingListDetailView(LoginRequiredMixin, View):
    def get(self, request, list_id):
        shopping_list = get_object_or_404(
            ShoppingList.objects.prefetch_related('items__pantry_product'),
            id=list_id,
            user=request.user,
        )
        items = list(shopping_list.items.all())
        item_count = len(items)
        purchased_count = sum(1 for item in items if item.is_purchased)
        progress_percent = int((purchased_count / item_count) * 100) if item_count else 0

        context = get_shopping_form_context(
            request,
            shopping_list=shopping_list,
            items=items,
            item_count=item_count,
            purchased_count=purchased_count,
            remaining_count=item_count - purchased_count,
            progress_percent=progress_percent,
        )
        return render(request, 'cooking/shopping_detail.html', context)


class EditShoppingListView(LoginRequiredMixin, View):
    def get(self, request, list_id):
        shopping_list = get_object_or_404(ShoppingList, id=list_id, user=request.user)
        return render(request, 'cooking/shopping_edit.html', {
            'shopping_list': shopping_list,
        })

    def post(self, request, list_id):
        shopping_list = get_object_or_404(ShoppingList, id=list_id, user=request.user)
        title = request.POST.get('title', '').strip()
        if not title:
            messages.error(request, 'Nazwa listy jest wymagana.')
            return render(request, 'cooking/shopping_edit.html', {
                'shopping_list': shopping_list,
                'form_values': request.POST,
            })

        shopping_list.title = title
        shopping_list.save(update_fields=['title', 'updated_at'])
        messages.success(request, 'Zapisano nazwę listy.')
        return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)


class DeleteShoppingListView(LoginRequiredMixin, View):
    def post(self, request, list_id):
        shopping_list = get_object_or_404(ShoppingList, id=list_id, user=request.user)
        title = shopping_list.title
        shopping_list.delete()
        messages.success(request, f'Usunięto listę: {title}.')
        return redirect('cooking:shopping-list')


class AddShoppingListItemView(LoginRequiredMixin, View):
    def post(self, request, list_id):
        shopping_list = get_object_or_404(ShoppingList, id=list_id, user=request.user)
        if shopping_list.status == ShoppingList.COMPLETED:
            messages.info(request, 'Ta lista została już zakończona.')
            return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)

        try:
            name = request.POST.get('name', '').strip()
            if not name:
                raise ValueError('Nazwa produktu jest wymagana.')
            unit = request.POST.get('unit') or PantryProduct.UNIT_PIECE
            quantity = parse_pantry_decimal(request.POST.get('quantity'), default='1')
            if quantity <= 0:
                raise ValueError('Ilość musi być większa od zera.')
            validate_pantry_quantity_for_unit(quantity, unit)
            product = find_user_pantry_product(request.user, name)
            ShoppingListItem.objects.create(
                shopping_list=shopping_list,
                pantry_product=product,
                name=name,
                quantity=quantity,
                unit=unit,
                category=request.POST.get('category', '').strip() or (product.category if product else ''),
                note=request.POST.get('note', '').strip(),
            )
            shopping_list.save(update_fields=['updated_at'])
            messages.success(request, f'Dodano pozycję: {name}.')
        except Exception as exc:
            messages.error(request, f'Nie udało się dodać pozycji: {exc}')

        return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)


class UpdateShoppingListItemView(LoginRequiredMixin, View):
    def post(self, request, item_id):
        item = get_object_or_404(ShoppingListItem, id=item_id, shopping_list__user=request.user)
        shopping_list = item.shopping_list
        if shopping_list.status == ShoppingList.COMPLETED:
            messages.info(request, 'Ta lista została już zakończona.')
            return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)

        try:
            name = request.POST.get('name', '').strip()
            if not name:
                raise ValueError('Nazwa produktu jest wymagana.')
            unit = request.POST.get('unit') or PantryProduct.UNIT_PIECE
            quantity = parse_pantry_decimal(request.POST.get('quantity'), default='1')
            if quantity <= 0:
                raise ValueError('Ilość musi być większa od zera.')
            validate_pantry_quantity_for_unit(quantity, unit)
            product = find_user_pantry_product(request.user, name)

            item.name = name
            item.quantity = quantity
            item.unit = unit
            item.category = request.POST.get('category', '').strip() or (product.category if product else '')
            item.note = request.POST.get('note', '').strip()
            item.pantry_product = product
            item.save(update_fields=[
                'name',
                'quantity',
                'unit',
                'category',
                'note',
                'pantry_product',
                'updated_at',
            ])
            shopping_list.save(update_fields=['updated_at'])
            messages.success(request, f'Zapisano pozycję: {item.name}.')
        except Exception as exc:
            messages.error(request, f'Nie udało się zapisać pozycji: {exc}')

        return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)


class ToggleShoppingListItemView(LoginRequiredMixin, View):
    def post(self, request, item_id):
        item = get_object_or_404(ShoppingListItem, id=item_id, shopping_list__user=request.user)
        if item.shopping_list.status == ShoppingList.COMPLETED:
            messages.info(request, 'Ta lista została już zakończona.')
            return redirect('cooking:shopping-list-detail', list_id=item.shopping_list.id)

        item.is_purchased = not item.is_purchased
        item.save(update_fields=['is_purchased', 'updated_at'])
        item.shopping_list.save(update_fields=['updated_at'])
        return redirect('cooking:shopping-list-detail', list_id=item.shopping_list.id)


class DeleteShoppingListItemView(LoginRequiredMixin, View):
    def post(self, request, item_id):
        item = get_object_or_404(ShoppingListItem, id=item_id, shopping_list__user=request.user)
        shopping_list = item.shopping_list
        if shopping_list.status == ShoppingList.COMPLETED:
            messages.info(request, 'Ta lista została już zakończona.')
            return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)

        item_name = item.name
        item.delete()
        shopping_list.save(update_fields=['updated_at'])
        messages.success(request, f'Usunięto pozycję: {item_name}.')
        return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)


class CompleteShoppingListView(LoginRequiredMixin, View):
    @transaction.atomic
    def post(self, request, list_id):
        shopping_list = get_object_or_404(
            ShoppingList.objects.prefetch_related('items__pantry_product'),
            id=list_id,
            user=request.user,
        )
        if shopping_list.status == ShoppingList.COMPLETED:
            messages.info(request, 'Ta lista została już zakończona.')
            return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)

        purchased_items = [item for item in shopping_list.items.all() if item.is_purchased]
        if not purchased_items:
            messages.error(request, 'Zaznacz przynajmniej jedną kupioną pozycję.')
            return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)

        prepared_updates = []
        errors = []
        for item in purchased_items:
            product = item.pantry_product
            if product and product.user_id != request.user.id:
                product = None
            if product is None:
                product = find_user_pantry_product(request.user, item.name)

            try:
                if product is None:
                    validate_pantry_quantity_for_unit(item.quantity, item.unit)
                    prepared_updates.append((item, None, item.quantity))
                else:
                    movement_quantity = convert_pantry_quantity(item.quantity, item.unit, product.unit)
                    validate_pantry_quantity_for_unit(movement_quantity, product.unit)
                    prepared_updates.append((item, product, movement_quantity))
            except ValueError as exc:
                errors.append(f'{item.name}: {exc}')

        if errors:
            for error in errors[:5]:
                messages.error(request, error)
            if len(errors) > 5:
                messages.error(request, f'Pozostałe błędy: {len(errors) - 5}.')
            return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)

        for item, product, movement_quantity in prepared_updates:
            if product is None:
                product = PantryProduct.objects.create(
                    user=request.user,
                    name=item.name,
                    category=item.category or 'Inne',
                    unit=item.unit,
                    current_quantity=Decimal('0.00'),
                    minimum_quantity=Decimal('0.00'),
                )
            if item.category and not product.category:
                product.category = item.category
            product.current_quantity += movement_quantity
            product.save(update_fields=['current_quantity', 'category', 'updated_at'])
            PantryMovement.objects.create(
                product=product,
                movement_type=PantryMovement.PURCHASE,
                quantity=movement_quantity,
                occurred_on=timezone.localdate(),
                note=f'Lista zakupów: {shopping_list.title}',
            )
            if item.pantry_product_id != product.id:
                item.pantry_product = product
                item.save(update_fields=['pantry_product', 'updated_at'])

        shopping_list.status = ShoppingList.COMPLETED
        shopping_list.save(update_fields=['status', 'updated_at'])
        messages.success(request, f'Dodano do spiżarni {len(prepared_updates)} kupionych pozycji.')
        return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)


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
