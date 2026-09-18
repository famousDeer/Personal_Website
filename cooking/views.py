import json
import mimetypes
import re
from datetime import timedelta
from uuid import UUID, uuid4
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q, Sum
from django.http import FileResponse, Http404, JsonResponse
from django.urls import reverse
from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin # Ważne dla bezpieczeństwa klas
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from .constants import (
    PANTRY_CATEGORIES,
    PANTRY_SCANNER_AUTO_ACTION_ENABLED,
    PANTRY_SCANNER_AUTO_ACTION_TIMEOUT_MS,
)
from .models import (
    PantryMovement,
    PantryProduct,
    ProductCatalogEntry,
    Recipe,
    RecipeStep,
    RecipeStepIngredient,
    ShoppingList,
    ShoppingListItem,
)
from .services.product_catalog import (
    lookup_product_catalog,
    suggested_category_for_catalog_entry,
)
from .services.pantry_forecast import (
    forecast_pantry_products,
    infer_typical_shopping_weekday,
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
ALLOWED_RECIPE_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
ALLOWED_RECIPE_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
MAX_RECIPE_IMAGE_SIZE = 5 * 1024 * 1024
PANTRY_BARCODE_PATTERN = re.compile(r'^[0-9A-Za-z._-]+$')
PANTRY_MAX_QUANTITY = Decimal('99999999.99')


def parse_pantry_decimal(value, default='0'):
    if value in [None, '']:
        value = default
    try:
        parsed = Decimal(str(value).replace(',', '.'))
        if not parsed.is_finite():
            raise InvalidOperation
        parsed = parsed.quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        raise ValueError('Podaj poprawną liczbę.')
    if parsed < 0:
        raise ValueError('Ilość nie może być ujemna.')
    if parsed > PANTRY_MAX_QUANTITY:
        raise ValueError('Ilość jest zbyt duża.')
    return parsed


def normalize_pantry_barcode(value, required=True):
    barcode = ''.join(str(value or '').split())
    if not barcode:
        if required:
            raise ValueError('Kod kreskowy jest wymagany.')
        return ''
    if len(barcode) > 64 or not PANTRY_BARCODE_PATTERN.fullmatch(barcode):
        raise ValueError('Kod kreskowy ma nieprawidłowy format.')
    return barcode


def parse_json_request(request):
    try:
        payload = json.loads(request.body or b'{}')
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError('Nie udało się odczytać danych skanera.') from exc
    if not isinstance(payload, dict):
        raise ValueError('Dane skanera mają nieprawidłowy format.')
    return payload


def parse_scanner_request(request):
    if request.content_type and request.content_type.split(';', 1)[0] == 'multipart/form-data':
        return request.POST.dict()
    return parse_json_request(request)


def parse_pantry_category(value, default=''):
    category = str(value or '').strip()
    if not category:
        return default
    if category not in PANTRY_CATEGORIES:
        raise ValueError('Wybierz poprawną kategorię produktu.')
    return category


def pantry_product_json(product):
    image_url = reverse('cooking:pantry-product-image', args=[product.id]) if product.image else ''
    return {
        'id': product.id,
        'name': product.name,
        'barcode': product.barcode,
        'category': product.category,
        'unit': product.unit,
        'unit_label': product.display_unit,
        'quantity_per_scan': format(product.quantity_per_scan, '.2f'),
        'current_quantity': format(product.current_quantity, '.2f'),
        'current_package_count': product.current_package_count,
        'package_count_is_known': product.package_count_is_known,
        'image_url': image_url,
        'image_upload_url': reverse('cooking:pantry-product-image-upload', args=[product.id]),
        'stock_status': product.stock_status,
    }


def pantry_catalog_json(result):
    payload = {
        'status': result.status,
        'cache_state': result.cache_state,
        'source': ProductCatalogEntry.SOURCE_OPEN_FOOD_FACTS,
        'source_label': 'Open Food Facts',
        'cached_locally': bool(result.entry and result.entry.fetched_at),
    }
    entry = result.entry
    if result.status not in ['found', 'found_incomplete'] or not entry:
        return payload

    image_cached_locally = bool(entry.image)
    image_url = reverse('cooking:pantry-catalog-image', args=[entry.id]) if image_cached_locally else ''
    payload.update({
        'name': entry.product_name,
        'brand': entry.brand,
        'description': entry.description,
        'ingredients': entry.ingredients,
        'quantity_text': entry.quantity_text,
        'suggested_quantity_per_scan': (
            format(entry.suggested_quantity_per_scan, '.2f')
            if entry.suggested_quantity_per_scan is not None
            else '1.00'
        ),
        'suggested_unit': entry.suggested_unit or PantryProduct.UNIT_PACKAGE,
        'suggested_category': entry.suggested_category,
        'image_url': image_url,
        'image_cached_locally': image_cached_locally,
        'attribution_url': entry.attribution_url,
        'can_auto_register': bool(entry.product_name),
    })
    return payload


def parse_scan_id(value):
    try:
        return str(UUID(str(value or '')))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError('Identyfikator skanu jest nieprawidłowy.') from exc


def parse_scan_count(value, default=1):
    try:
        parsed = Decimal(str(value if value not in [None, ''] else default))
        if not parsed.is_finite() or parsed != parsed.to_integral_value():
            raise InvalidOperation
        count = int(parsed)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('Podaj poprawną liczbę opakowań.') from exc
    if count < 1 or count > 9999:
        raise ValueError('Liczba opakowań musi mieścić się w zakresie 1–9999.')
    return count


def parse_package_count(value, default=0):
    try:
        parsed = Decimal(str(value if value not in [None, ''] else default))
        if not parsed.is_finite() or parsed != parsed.to_integral_value():
            raise InvalidOperation
        count = int(parsed)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('Podaj poprawną liczbę sztuk.') from exc
    if count < 0 or count > 2147483647:
        raise ValueError('Liczba sztuk jest poza dozwolonym zakresem.')
    return count


def estimated_package_count(quantity, quantity_per_scan):
    if quantity <= 0 or quantity_per_scan <= 0:
        return 0
    return int((quantity / quantity_per_scan).to_integral_value(rounding=ROUND_CEILING))


def tracks_packages(product):
    return product.tracks_packages


def sync_package_count_from_quantity(product):
    if not tracks_packages(product):
        return False
    product.current_package_count = estimated_package_count(
        product.current_quantity,
        product.quantity_per_scan,
    )
    return True


def validate_pantry_storage_quantity(quantity):
    if quantity < 0:
        raise ValueError('Ilość nie może być ujemna.')
    if quantity > PANTRY_MAX_QUANTITY:
        raise ValueError('Ilość jest zbyt duża.')


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


def get_user_typical_shopping_weekday(user):
    purchase_dates = PantryMovement.objects.filter(
        product__user=user,
        movement_type=PantryMovement.PURCHASE,
        occurred_on__gte=timezone.localdate() - timedelta(days=180),
    ).values_list('occurred_on', flat=True).distinct()
    return infer_typical_shopping_weekday(purchase_dates)


def pantry_forecast_movements_prefetch(today):
    return Prefetch(
        'movements',
        queryset=PantryMovement.objects.filter(
            occurred_on__gte=today - timedelta(days=89),
        ).order_by('occurred_on', 'created_at'),
        to_attr='forecast_movements',
    )


def find_user_pantry_product(user, name):
    return PantryProduct.objects.filter(user=user, name__iexact=name).first()


def build_shopping_suggestions(user):
    today = timezone.localdate()
    suggestions = []
    shopping_weekday = get_user_typical_shopping_weekday(user)

    products = list(PantryProduct.objects.filter(user=user).prefetch_related(
        pantry_forecast_movements_prefetch(today),
    ))
    forecasts = forecast_pantry_products(
        products,
        today=today,
        shopping_weekday=shopping_weekday,
    )
    for product in products:
        forecast = forecasts[product.pk]
        restock_date = forecast.buy_date
        restock_due = forecast.is_due
        needs_stock = product.stock_status in ['empty', 'low']
        if not needs_stock and not restock_due:
            continue

        suggested_quantity = forecast.suggested_quantity
        if suggested_quantity <= 0:
            suggested_quantity = (
                product.quantity_per_scan
                if product.barcode or product.unit in [PantryProduct.UNIT_PIECE, PantryProduct.UNIT_PACKAGE]
                else Decimal('1.00')
            )

        reason = 'Niski stan'
        if product.current_quantity <= 0:
            reason = 'Brak w spiżarni'
        elif restock_due:
            reason = 'Prognoza: uzupełnij teraz'

        suggestions.append({
            'product': product,
            'quantity': normalize_shopping_quantity(suggested_quantity, product.unit),
            'reason': reason,
            'restock_date': restock_date,
            'forecast': forecast,
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


def validate_pantry_product_image(uploaded_file):
    if not uploaded_file:
        return None

    if uploaded_file.size > MAX_RECIPE_IMAGE_SIZE:
        raise ValueError('Zdjęcie produktu może mieć maksymalnie 5 MB.')

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
        today = timezone.localdate()
        all_products = list(PantryProduct.objects.filter(user=request.user).prefetch_related(
            pantry_forecast_movements_prefetch(today),
        ))
        search_query = request.GET.get('q', '').strip()
        status_filter = request.GET.get('status', '').strip()
        category_filter = request.GET.get('category', '').strip()

        products = all_products
        if search_query:
            normalized_search = search_query.casefold()
            products = [
                product for product in products
                if normalized_search in product.name.casefold()
                or normalized_search in product.category.casefold()
                or normalized_search in product.barcode.casefold()
            ]
        if category_filter:
            products = [product for product in products if product.category == category_filter]

        visible_products = list(products)
        catalog_by_barcode = {
            entry.lookup_barcode: entry
            for entry in ProductCatalogEntry.objects.filter(
                source=ProductCatalogEntry.SOURCE_OPEN_FOOD_FACTS,
                status=ProductCatalogEntry.STATUS_FOUND,
                lookup_barcode__in={product.barcode for product in visible_products if product.barcode},
            )
        }

        product_cards = []
        shopping_weekday = get_user_typical_shopping_weekday(request.user)
        forecasts = forecast_pantry_products(
            all_products,
            today=today,
            shopping_weekday=shopping_weekday,
        )

        for product in visible_products:
            forecast = forecasts[product.pk]
            average_daily = forecast.rate
            depletion_date = forecast.minimum_date_to
            restock_date = forecast.buy_date
            days_left = (depletion_date - today).days if depletion_date else None
            restock_in_days = (restock_date - today).days if restock_date else None
            card = {
                'product': product,
                'average_daily': average_daily,
                'depletion_date': depletion_date,
                'restock_date': restock_date,
                'days_left': days_left,
                'restock_in_days': restock_in_days,
                'catalog_entry': catalog_by_barcode.get(product.barcode),
                'forecast': forecast,
                'manual_consume_id': uuid4(),
                'manual_purchase_id': uuid4(),
            }
            if status_filter == 'low':
                matches_status = (
                    product.stock_status in ['low', 'empty'] or forecast.is_due
                )
            elif status_filter == 'ok':
                matches_status = product.stock_status == 'ok' and not forecast.is_due
            else:
                matches_status = not status_filter or product.stock_status == status_filter
            if matches_status:
                product_cards.append(card)

        grouped_cards = {}
        for card in product_cards:
            category = card['product'].category or 'Bez kategorii'
            grouped_cards.setdefault(category, []).append(card)

        category_order = [*PANTRY_CATEGORIES, 'Bez kategorii']
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
                    if card['product'].stock_status in ['low', 'empty'] or card['forecast'].is_due
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
            'units': PantryProduct.UNIT_CHOICES,
            'current_search': search_query,
            'current_status': status_filter,
            'current_category': category_filter,
            'product_count': len(product_cards),
            'has_any_products': bool(all_products),
            'filters_active': bool(search_query or status_filter or category_filter),
            'low_stock_count': sum(
                1 for card in product_cards
                if card['product'].stock_status in ['low', 'empty'] or card['forecast'].is_due
            ),
            'total_current_quantity': sum(
                (card['product'].current_quantity for card in product_cards),
                Decimal('0'),
            ),
            'total_package_count': sum(
                card['product'].current_package_count for card in product_cards
            ),
            'total_consumed': movement_stats['total_consumed'] or Decimal('0'),
            'total_restocked': movement_stats['total_restocked'] or Decimal('0'),
            'movement_count': movement_stats['movement_count'] or 0,
            'scanner_auto_action_enabled': PANTRY_SCANNER_AUTO_ACTION_ENABLED,
            'scanner_auto_action_timeout_seconds': max(
                1,
                (PANTRY_SCANNER_AUTO_ACTION_TIMEOUT_MS + 999) // 1000,
            ),
            'today': today,
        }
        return render(request, 'cooking/pantry.html', context)


class PantryBarcodeLookupView(LoginRequiredMixin, View):
    def get(self, request):
        try:
            barcode = normalize_pantry_barcode(request.GET.get('barcode'))
        except ValueError as exc:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

        product = PantryProduct.objects.filter(user=request.user, barcode=barcode).first()
        catalog = lookup_product_catalog(barcode)
        suggested_category = suggested_category_for_catalog_entry(catalog.entry)
        if product and not product.category.strip() and suggested_category:
            previous_category = product.category
            updated = PantryProduct.objects.filter(
                pk=product.pk,
                user=request.user,
                category=previous_category,
            ).update(
                category=suggested_category,
                updated_at=timezone.now(),
            )
            if updated:
                product.category = suggested_category
            else:
                product = PantryProduct.objects.filter(
                    pk=product.pk,
                    user=request.user,
                ).first()
        response = JsonResponse({
            'ok': True,
            'status': 'known' if product else 'unknown',
            'barcode': barcode,
            'product': pantry_product_json(product) if product else None,
            'catalog': pantry_catalog_json(catalog),
            'auto_action_enabled': PANTRY_SCANNER_AUTO_ACTION_ENABLED,
            'default_action': (
                PantryMovement.CONSUME
                if product and PANTRY_SCANNER_AUTO_ACTION_ENABLED
                else None
            ),
            'timeout_ms': PANTRY_SCANNER_AUTO_ACTION_TIMEOUT_MS,
        })
        response['Cache-Control'] = 'private, no-store'
        return response


class PantryCatalogImageView(LoginRequiredMixin, View):
    def get(self, request, entry_id):
        entry = ProductCatalogEntry.objects.filter(
            id=entry_id,
            status=ProductCatalogEntry.STATUS_FOUND,
        ).first()
        if not entry or not entry.image:
            raise Http404
        try:
            image_file = entry.image.open('rb')
        except (FileNotFoundError, OSError):
            raise Http404 from None
        content_type = mimetypes.guess_type(entry.image.name)[0] or 'application/octet-stream'
        response = FileResponse(image_file, content_type=content_type)
        response['Cache-Control'] = 'private, max-age=86400'
        response['X-Content-Type-Options'] = 'nosniff'
        return response


class PantryProductImageView(LoginRequiredMixin, View):
    def get(self, request, product_id):
        product = PantryProduct.objects.filter(id=product_id, user=request.user).first()
        if not product or not product.image:
            raise Http404
        try:
            image_file = product.image.open('rb')
        except (FileNotFoundError, OSError):
            raise Http404 from None
        content_type = mimetypes.guess_type(product.image.name)[0] or 'application/octet-stream'
        response = FileResponse(image_file, content_type=content_type)
        response['Cache-Control'] = 'private, max-age=86400'
        response['X-Content-Type-Options'] = 'nosniff'
        return response


class PantryProductImageUploadView(LoginRequiredMixin, View):
    @transaction.atomic
    def post(self, request, product_id):
        try:
            product = get_object_or_404(
                PantryProduct.objects.select_for_update(),
                id=product_id,
                user=request.user,
            )
            if product.image:
                return JsonResponse(
                    {'ok': False, 'error': 'Ten produkt ma już własne zdjęcie.'},
                    status=409,
                )
            image = validate_pantry_product_image(request.FILES.get('image'))
            if not image:
                raise ValueError('Wybierz zdjęcie produktu.')
            product.image = image
            product.save(update_fields=['image', 'updated_at'])
            return JsonResponse({'ok': True, 'product': pantry_product_json(product)})
        except ValueError as exc:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)


class PantryBarcodeActionView(LoginRequiredMixin, View):
    @transaction.atomic
    def post(self, request):
        try:
            payload = parse_json_request(request)
            barcode = normalize_pantry_barcode(payload.get('barcode'))
            scan_id = parse_scan_id(payload.get('scan_id'))
            action = payload.get('action')
            count = parse_scan_count(payload.get('count'))
            if action not in [PantryMovement.CONSUME, PantryMovement.PURCHASE]:
                raise ValueError('Wybierz „zużyto” albo „dodano”.')

            replay = PantryMovement.objects.select_related('product').filter(scan_id=scan_id).first()
            if replay:
                replay_quantity = (replay.product.quantity_per_scan * Decimal(count)).quantize(Decimal('0.01'))
                replay_requested_quantity = replay.requested_quantity or replay.quantity
                replay_requested_count = (
                    replay.requested_package_count
                    if replay.requested_package_count is not None
                    else replay.package_count
                )
                if (
                    replay.product.user_id != request.user.id
                    or replay.product.barcode != barcode
                    or replay.movement_type != action
                    or replay_requested_quantity != replay_quantity
                    or replay_requested_count != count
                ):
                    return JsonResponse(
                        {'ok': False, 'error': 'Ten skan został już użyty do innej operacji.'},
                        status=409,
                    )
                return JsonResponse({
                    'ok': True,
                    'idempotent_replay': True,
                    'action': action,
                    'count': replay.package_count,
                    'requested_count': replay_requested_count,
                    'quantity': format(replay.quantity, '.2f'),
                    'requested_quantity': format(replay_requested_quantity, '.2f'),
                    'stock_was_insufficient': replay.stock_was_insufficient,
                    'product': pantry_product_json(replay.product),
                })

            product = PantryProduct.objects.select_for_update().filter(
                user=request.user,
                barcode=barcode,
            ).first()
            if not product:
                return JsonResponse(
                    {'ok': False, 'error': 'Kod nie jest jeszcze przypisany do produktu.'},
                    status=404,
                )
            requested_quantity = (product.quantity_per_scan * Decimal(count)).quantize(Decimal('0.01'))
            validate_pantry_storage_quantity(requested_quantity)
            validate_pantry_quantity_for_unit(requested_quantity, product.unit)
            before = product.current_quantity
            if product.current_package_count == 0 and before > 0:
                sync_package_count_from_quantity(product)
            before_packages = product.current_package_count
            stock_was_insufficient = action == PantryMovement.CONSUME and (
                requested_quantity > before or count > before_packages
            )

            if action == PantryMovement.CONSUME:
                fulfilled_quantity = min(requested_quantity, before)
                fulfilled_package_count = min(count, before_packages)
                product.current_quantity = before - fulfilled_quantity
                product.current_package_count = before_packages - fulfilled_package_count
                if product.current_quantity == 0:
                    product.current_package_count = 0
            else:
                fulfilled_quantity = requested_quantity
                fulfilled_package_count = count
                validate_pantry_storage_quantity(before + fulfilled_quantity)
                parse_package_count(before_packages + count)
                product.current_quantity = before + fulfilled_quantity
                product.current_package_count = before_packages + count

            product.save(update_fields=['current_quantity', 'current_package_count', 'updated_at'])
            movement = PantryMovement.objects.create(
                product=product,
                movement_type=action,
                quantity=fulfilled_quantity,
                requested_quantity=requested_quantity,
                occurred_on=timezone.localdate(),
                note='Skan kodu kreskowego',
                scan_id=scan_id,
                package_count=fulfilled_package_count,
                requested_package_count=count,
                stock_was_insufficient=stock_was_insufficient,
            )
            return JsonResponse({
                'ok': True,
                'idempotent_replay': False,
                'action': action,
                'count': fulfilled_package_count,
                'requested_count': count,
                'quantity': format(fulfilled_quantity, '.2f'),
                'requested_quantity': format(requested_quantity, '.2f'),
                'before': format(before, '.2f'),
                'after': format(product.current_quantity, '.2f'),
                'before_package_count': before_packages,
                'after_package_count': product.current_package_count,
                'stock_was_insufficient': stock_was_insufficient,
                'movement_id': movement.id,
                'product': pantry_product_json(product),
            })
        except ValueError as exc:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
        except IntegrityError:
            return JsonResponse(
                {'ok': False, 'error': 'Operacja dla tego skanu została już zapisana.'},
                status=409,
            )


class PantryBarcodeRegisterView(LoginRequiredMixin, View):
    @transaction.atomic
    def post(self, request):
        try:
            payload = parse_scanner_request(request)
            image = validate_pantry_product_image(request.FILES.get('image'))
            barcode = normalize_pantry_barcode(payload.get('barcode'))
            scan_id = parse_scan_id(payload.get('scan_id'))
            name = str(payload.get('name') or '').strip()
            if not name:
                raise ValueError('Podaj nazwę produktu.')
            if len(name) > 160:
                raise ValueError('Nazwa produktu może mieć maksymalnie 160 znaków.')
            requested_category = parse_pantry_category(payload.get('category'))
            catalog_entry = ProductCatalogEntry.objects.filter(
                source=ProductCatalogEntry.SOURCE_OPEN_FOOD_FACTS,
                lookup_barcode=barcode,
                status=ProductCatalogEntry.STATUS_FOUND,
            ).first()
            catalog_category = suggested_category_for_catalog_entry(catalog_entry)
            category = requested_category or catalog_category or 'Inne'

            count = parse_scan_count(payload.get('count'))
            unit = payload.get('unit') or PantryProduct.UNIT_PIECE
            if unit not in dict(PantryProduct.UNIT_CHOICES):
                raise ValueError('Wybierz poprawną jednostkę.')
            quantity_per_scan = parse_pantry_decimal(payload.get('quantity_per_scan'), default='1')
            if quantity_per_scan <= 0:
                raise ValueError('Ilość na jeden skan musi być większa od zera.')
            validate_pantry_quantity_for_unit(quantity_per_scan, unit)

            replay = PantryMovement.objects.select_related('product').filter(scan_id=scan_id).first()
            if replay:
                replay_quantity_per_scan = convert_pantry_quantity(
                    quantity_per_scan,
                    unit,
                    replay.product.unit,
                )
                replay_quantity = (replay_quantity_per_scan * Decimal(count)).quantize(Decimal('0.01'))
                if (
                    replay.product.user_id != request.user.id
                    or replay.product.barcode != barcode
                    or replay.movement_type != PantryMovement.PURCHASE
                    or replay.product.name.casefold() != name.casefold()
                    or replay.quantity != replay_quantity
                    or replay.package_count != count
                ):
                    return JsonResponse(
                        {'ok': False, 'error': 'Ten skan został już użyty do innej operacji.'},
                        status=409,
                    )
                return JsonResponse({
                    'ok': True,
                    'idempotent_replay': True,
                    'created': False,
                    'action': PantryMovement.PURCHASE,
                    'count': replay.package_count,
                    'quantity': format(replay.quantity, '.2f'),
                    'product': pantry_product_json(replay.product),
                })

            product_with_barcode = PantryProduct.objects.select_for_update().filter(
                user=request.user,
                barcode=barcode,
            ).first()
            if product_with_barcode:
                return JsonResponse(
                    {'ok': False, 'error': 'Ten kod jest już przypisany do produktu.'},
                    status=409,
                )

            product = PantryProduct.objects.select_for_update().filter(
                user=request.user,
                name__iexact=name,
            ).first()
            created = product is None
            if product:
                if product.barcode:
                    return JsonResponse(
                        {'ok': False, 'error': 'Produkt o tej nazwie ma już inny kod kreskowy.'},
                        status=409,
                    )
                quantity_per_scan = convert_pantry_quantity(quantity_per_scan, unit, product.unit)
                unit = product.unit
                validate_pantry_quantity_for_unit(quantity_per_scan, unit)
                product.barcode = barcode
                product.quantity_per_scan = quantity_per_scan
                if not product.category.strip():
                    product.category = category
                if product.current_package_count == 0 and product.current_quantity > 0:
                    sync_package_count_from_quantity(product)
            else:
                product = PantryProduct(
                    user=request.user,
                    name=name,
                    barcode=barcode,
                    quantity_per_scan=quantity_per_scan,
                    category=category,
                    unit=unit,
                    current_quantity=Decimal('0.00'),
                    current_package_count=0,
                    minimum_quantity=Decimal('0.00'),
                    restock_lead_days=3,
                )

            quantity = (quantity_per_scan * Decimal(count)).quantize(Decimal('0.01'))
            validate_pantry_storage_quantity(quantity)
            validate_pantry_storage_quantity(product.current_quantity + quantity)
            parse_package_count(product.current_package_count + count)
            product.current_quantity += quantity
            product.current_package_count += count
            if image and not product.image:
                product.image = image
            product.save()
            movement = PantryMovement.objects.create(
                product=product,
                movement_type=PantryMovement.PURCHASE,
                quantity=quantity,
                occurred_on=timezone.localdate(),
                note='Dodano przez skaner kodu kreskowego',
                scan_id=scan_id,
                package_count=count,
            )
            return JsonResponse({
                'ok': True,
                'idempotent_replay': False,
                'created': created,
                'action': PantryMovement.PURCHASE,
                'count': count,
                'quantity': format(quantity, '.2f'),
                'movement_id': movement.id,
                'product': pantry_product_json(product),
            }, status=201 if created else 200)
        except ValueError as exc:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
        except IntegrityError:
            return JsonResponse(
                {'ok': False, 'error': 'Nie udało się przypisać kodu. Produkt lub kod już istnieje.'},
                status=409,
            )


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
            barcode = normalize_pantry_barcode(request.POST.get('barcode'), required=False)
            quantity_per_scan = parse_pantry_decimal(request.POST.get('quantity_per_scan'), default='1')
            if quantity_per_scan <= 0:
                raise ValueError('Ilość na jeden skan musi być większa od zera.')
            current_quantity = parse_pantry_decimal(request.POST.get('current_quantity'))
            raw_package_count = request.POST.get('current_package_count')
            if raw_package_count in [None, '']:
                current_package_count = (
                    estimated_package_count(current_quantity, quantity_per_scan)
                    if barcode or unit in [PantryProduct.UNIT_PIECE, PantryProduct.UNIT_PACKAGE]
                    else 0
                )
            else:
                current_package_count = parse_package_count(raw_package_count)
            if barcode or current_package_count > 0 or unit in [
                PantryProduct.UNIT_PIECE,
                PantryProduct.UNIT_PACKAGE,
            ]:
                expected_package_count = estimated_package_count(
                    current_quantity,
                    quantity_per_scan,
                )
                if current_package_count != expected_package_count:
                    raise ValueError(
                        'Liczba opakowań nie odpowiada ilości łącznej i wielkości opakowania.'
                    )
            minimum_quantity = parse_pantry_decimal(request.POST.get('minimum_quantity'))
            validate_pantry_quantity_for_unit(quantity_per_scan, unit)
            validate_pantry_quantity_for_unit(current_quantity, unit)
            validate_pantry_quantity_for_unit(minimum_quantity, unit)
            restock_lead_days = int(request.POST.get('restock_lead_days') or 3)
            if restock_lead_days < 0:
                raise ValueError('Wyprzedzenie zakupu nie może być ujemne.')
            image = validate_pantry_product_image(request.FILES.get('image'))

            with transaction.atomic():
                product = PantryProduct.objects.create(
                    user=request.user,
                    name=name,
                    barcode=barcode,
                    quantity_per_scan=quantity_per_scan,
                    category=request.POST.get('category', '').strip(),
                    unit=unit,
                    current_quantity=current_quantity,
                    current_package_count=current_package_count,
                    minimum_quantity=minimum_quantity,
                    restock_lead_days=restock_lead_days,
                    notes=request.POST.get('notes', '').strip(),
                    image=image,
                )
                if current_quantity > 0:
                    PantryMovement.objects.create(
                        product=product,
                        movement_type=PantryMovement.PURCHASE,
                        quantity=current_quantity,
                        package_count=current_package_count,
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
    @transaction.atomic
    def post(self, request, product_id):
        product = get_object_or_404(
            PantryProduct.objects.select_for_update(),
            id=product_id,
            user=request.user,
        )
        movement_type = request.POST.get('movement_type')
        try:
            operation_id = (
                parse_scan_id(request.POST.get('operation_id'))
                if request.POST.get('operation_id')
                else None
            )
            raw_package_count = request.POST.get('package_count')
            packages_were_explicit = raw_package_count not in [None, '']
            if packages_were_explicit:
                movement_package_count = parse_scan_count(raw_package_count)
                quantity = (
                    product.quantity_per_scan * Decimal(movement_package_count)
                ).quantize(Decimal('0.01'))
            else:
                quantity = parse_pantry_decimal(request.POST.get('quantity'))
                if quantity <= 0:
                    raise ValueError('Ilość musi być większa od zera.')
                movement_package_count = (
                    estimated_package_count(quantity, product.quantity_per_scan)
                    if tracks_packages(product)
                    else None
                )
            validate_pantry_quantity_for_unit(quantity, product.unit)
            if operation_id:
                replay = PantryMovement.objects.filter(scan_id=operation_id).first()
                if replay:
                    replay_requested_quantity = replay.requested_quantity or replay.quantity
                    replay_requested_packages = (
                        replay.requested_package_count
                        if replay.requested_package_count is not None
                        else replay.package_count
                    )
                    if (
                        replay.product_id != product.id
                        or replay.movement_type != movement_type
                        or replay_requested_quantity != quantity
                        or replay_requested_packages != movement_package_count
                    ):
                        raise ValueError('Identyfikator operacji został już użyty dla innej zmiany.')
                    messages.info(request, 'Ta operacja została już zapisana.')
                    return redirect('cooking:pantry')
            if product.current_package_count == 0 and product.current_quantity > 0:
                sync_package_count_from_quantity(product)
            requested_quantity = quantity
            requested_package_count = movement_package_count
            before_quantity = product.current_quantity
            before_package_count = product.current_package_count
            stock_was_insufficient = False

            if movement_type == PantryMovement.CONSUME:
                stock_was_insufficient = requested_quantity > before_quantity or bool(
                    packages_were_explicit
                    and requested_package_count > before_package_count
                )
                fulfilled_quantity = min(requested_quantity, before_quantity)
                product.current_quantity = before_quantity - fulfilled_quantity
                if packages_were_explicit:
                    fulfilled_package_count = min(
                        requested_package_count,
                        before_package_count,
                    )
                    product.current_package_count = max(
                        0,
                        before_package_count - fulfilled_package_count,
                    )
                    if product.current_quantity == 0:
                        product.current_package_count = 0
                else:
                    sync_package_count_from_quantity(product)
                    fulfilled_package_count = (
                        before_package_count - product.current_package_count
                        if tracks_packages(product)
                        else None
                    )
                message = f'Zapisano zużycie: {product.name}.'
            elif movement_type == PantryMovement.PURCHASE:
                fulfilled_quantity = requested_quantity
                fulfilled_package_count = requested_package_count
                validate_pantry_storage_quantity(before_quantity + fulfilled_quantity)
                product.current_quantity = before_quantity + fulfilled_quantity
                if packages_were_explicit:
                    parse_package_count(before_package_count + requested_package_count)
                    product.current_package_count = before_package_count + requested_package_count
                else:
                    sync_package_count_from_quantity(product)
                message = f'Uzupełniono produkt: {product.name}.'
            else:
                raise ValueError('Nieznany typ operacji.')

            product.save(update_fields=['current_quantity', 'current_package_count', 'updated_at'])
            PantryMovement.objects.create(
                product=product,
                movement_type=movement_type,
                quantity=fulfilled_quantity,
                requested_quantity=requested_quantity,
                occurred_on=timezone.localdate(),
                note=request.POST.get('note', '').strip(),
                package_count=fulfilled_package_count,
                requested_package_count=requested_package_count,
                stock_was_insufficient=stock_was_insufficient,
                scan_id=operation_id,
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
            if product.current_package_count == 0 and product.current_quantity > 0:
                sync_package_count_from_quantity(product)
            package_tracking = tracks_packages(product)
            movement_package_count = (
                estimated_package_count(movement_quantity, product.quantity_per_scan)
                if package_tracking
                else None
            )
            product.current_quantity += movement_quantity
            if package_tracking:
                sync_package_count_from_quantity(product)
            product.save(update_fields=[
                'current_quantity', 'current_package_count', 'category', 'updated_at',
            ])
            PantryMovement.objects.create(
                product=product,
                movement_type=PantryMovement.PURCHASE,
                quantity=movement_quantity,
                occurred_on=timezone.localdate(),
                note=f'Lista zakupów: {shopping_list.title}',
                package_count=movement_package_count,
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
                fulfilled_quantity = Decimal('0.00')
                before_package_count = 0
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
                    if product.current_package_count == 0 and product.current_quantity > 0:
                        sync_package_count_from_quantity(product)
                    before_package_count = product.current_package_count
                    fulfilled_quantity = min(movement_quantity, product.current_quantity)
                    product.current_quantity -= fulfilled_quantity
                    sync_package_count_from_quantity(product)
                    if category and not product.category:
                        product.category = category
                    product.save(update_fields=[
                        'current_quantity', 'current_package_count', 'category', 'updated_at',
                    ])

                package_tracking = tracks_packages(product)
                fulfilled_package_count = (
                    max(before_package_count - product.current_package_count, 0)
                    if package_tracking
                    else None
                )
                requested_package_count = (
                    estimated_package_count(movement_quantity, product.quantity_per_scan)
                    if package_tracking
                    else None
                )

                note = 'Gotowanie'
                if recipe:
                    note = f'Gotowanie: {recipe.title}'
                PantryMovement.objects.create(
                    product=product,
                    movement_type=PantryMovement.CONSUME,
                    quantity=fulfilled_quantity,
                    requested_quantity=movement_quantity,
                    occurred_on=timezone.localdate(),
                    note=note,
                    package_count=fulfilled_package_count,
                    requested_package_count=requested_package_count,
                    stock_was_insufficient=movement_quantity > fulfilled_quantity,
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
