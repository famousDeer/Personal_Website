import json
import mimetypes
from collections import defaultdict
import re
from datetime import timedelta
from uuid import UUID, uuid4
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q, Sum
from django.http import FileResponse, Http404, JsonResponse
from django.urls import reverse
from django.utils.http import urlencode
from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin # Ważne dla bezpieczeństwa klas
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from .constants import (
    PANTRY_CATEGORIES,
    PANTRY_CATEGORY_OTHER,
    PANTRY_SCANNER_AUTO_ACTION_ENABLED,
    PANTRY_SCANNER_AUTO_ACTION_TIMEOUT_MS,
)
from .models import (
    PantryMovement,
    PantryProduct,
    ProductCatalogEntry,
    ProductGroup,
    ShopLayout,
    Recipe,
    RecipeStep,
    RecipeStepIngredient,
    ShoppingList,
    ShoppingListItem,
)
from .services.product_groups import (
    GroupForecastSubject,
    packages_in_stock,
    restock_target,
    suggest_groups,
)
from .services.product_catalog import (
    cached_open_food_facts_entry,
    household_catalog_entry,
    lookup_product_catalog,
    remember_household_product,
    suggest_category_from_name,
    suggested_category_for_catalog_entry,
)
from .services.pantry_forecast import (
    forecast_pantry_products,
    infer_typical_shopping_weekday,
)
from .services.pantry_editing import (
    ADJUST_NOTE,
    convert_movement_history,
    convert_to_unit,
    delete_file_after_commit,
    sync_open_shopping_items,
    units_are_convertible,
)
from .services.pantry_sharing import UNIT_BASE
from .services.polish import completion_message, polish_count, polish_number
from .services.shopping_sync import (
    complete_shopping_list,
    refresh_pantry_purchase,
    set_item_purchased,
    shopping_item_defaults,
    shopping_item_for_product,
    sort_items_by_shop,
)
from .services.pantry_quantities import (
    PANTRY_MAX_QUANTITY,
    convert_pantry_quantity,
    estimated_package_count,
    find_pantry_product,
    normalize_shopping_quantity,
    parse_package_count,
    parse_pantry_decimal,
    sync_package_count_from_quantity,
    tracks_packages,
    validate_pantry_quantity_for_unit,
    validate_pantry_storage_quantity,
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
    remembered = result.remembered
    payload = {
        'status': result.status,
        'cache_state': result.cache_state,
        'source': (
            ProductCatalogEntry.SOURCE_HOUSEHOLD
            if remembered
            else ProductCatalogEntry.SOURCE_OPEN_FOOD_FACTS
        ),
        'source_label': 'Zapamiętane w domu' if remembered else 'Open Food Facts',
        'remembered': remembered,
        'cached_locally': bool(result.entry and result.entry.fetched_at),
    }
    entry = result.entry
    if result.status not in ['found', 'found_incomplete'] or not entry:
        return payload

    # Nazwa, kategoria i ilość pochodzą z wpisu, który wygrał wyszukiwanie.
    # Zdjęcie, marka i opis - z Open Food Facts, także dla produktu
    # zapamiętanego w domu (jeśli baza go zna).
    details = result.source_entry if remembered else entry
    image_cached_locally = bool(details and details.image)
    image_url = (
        reverse('cooking:pantry-catalog-image', args=[details.id])
        if image_cached_locally
        else ''
    )
    name_language = '' if remembered else entry.name_language
    name_is_polish = remembered or name_language == 'pl'
    payload.update({
        'name': entry.product_name,
        'name_language': name_language,
        'name_is_polish': name_is_polish,
        'brand': details.brand if details else '',
        'description': details.description if details else '',
        'ingredients': details.ingredients if details else '',
        'quantity_text': details.quantity_text if details else '',
        'suggested_quantity_per_scan': (
            format(entry.suggested_quantity_per_scan, '.2f')
            if entry.suggested_quantity_per_scan is not None
            else '1.00'
        ),
        'suggested_unit': entry.suggested_unit or PantryProduct.UNIT_PACKAGE,
        'suggested_category': entry.suggested_category,
        'image_url': image_url,
        'image_cached_locally': image_cached_locally,
        'attribution_url': details.attribution_url if details else '',
        # Szybkie "Dodaj" jednym dotknięciem tylko z nazwą, której nie trzeba
        # poprawiać. Obca nazwa idzie przez formularz, żeby do spiżarni nie
        # trafiło "Spülmittel" zamiast "Płyn do naczyń".
        'can_auto_register': bool(entry.product_name) and name_is_polish,
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


def get_household_typical_shopping_weekday():
    purchase_dates = PantryMovement.objects.filter(
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


def build_shopping_suggestions():
    """Czego brakuje w domu - po jednej pozycji na produkt albo na grupę.

    Produkt należący do grupy („ten sam jogurt, inna firma”) nie liczy się sam:
    o braku decyduje zapas całej grupy, liczony w opakowaniach. Dzięki temu
    pusty jogurt jednej firmy nie trafia na listę, gdy w lodówce stoją dwa inne.
    """
    today = timezone.localdate()
    suggestions = []
    shopping_weekday = get_household_typical_shopping_weekday()

    products = list(PantryProduct.objects.select_related('group').prefetch_related(
        pantry_forecast_movements_prefetch(today),
    ))
    loose = [product for product in products if not product.group_id]
    grouped = defaultdict(list)
    for product in products:
        if product.group_id:
            grouped[product.group_id].append(product)

    groups = list(ProductGroup.objects.filter(pk__in=grouped))
    subjects = loose + [GroupForecastSubject(group, grouped[group.pk]) for group in groups]
    forecasts = forecast_pantry_products(
        subjects,
        today=today,
        shopping_weekday=shopping_weekday,
    )

    for product in loose:
        forecast = forecasts[product.pk]
        restock_due = forecast.is_due
        if product.stock_status not in ['empty', 'low'] and not restock_due:
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
            'group': None,
            'name': product.name,
            'category': product.category,
            'quantity': normalize_shopping_quantity(suggested_quantity, product.unit),
            'unit': product.unit,
            'unit_label': product.display_unit,
            'reason': reason,
            'restock_date': forecast.buy_date,
            'forecast': forecast,
        })

    for group in groups:
        members = grouped[group.pk]
        forecast = forecasts[f'group-{group.pk}']
        in_stock = sum(packages_in_stock(member) for member in members)
        status = 'empty' if in_stock <= 0 else ('low' if in_stock <= group.minimum_packages else 'ok')
        if status == 'ok' and not forecast.is_due:
            continue

        packages = forecast.suggested_packages or int(forecast.suggested_quantity)
        packages = max(packages, max(group.minimum_packages - in_stock, 0), 1)

        reason = 'Niski stan'
        if in_stock <= 0:
            reason = 'Brak w spiżarni'
        elif forecast.is_due:
            reason = 'Prognoza: uzupełnij teraz'

        suggestions.append({
            'product': None,
            'group': group,
            'name': group.name,
            'category': group.category,
            'quantity': Decimal(packages),
            'unit': PantryProduct.UNIT_PIECE,
            'unit_label': dict(PantryProduct.UNIT_CHOICES)[PantryProduct.UNIT_PIECE],
            'reason': reason,
            'restock_date': forecast.buy_date,
            'forecast': forecast,
        })

    suggestions.sort(key=lambda suggestion: suggestion['name'].casefold())
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

        product = find_pantry_product(name)
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
        'product_groups': ProductGroup.objects.all(),
    }
    context.update(extra_context)
    return context


def pantry_rows_for_list(items):
    """Spiżarnia obok listy: to samo, co ekran „Spiżarnia” w trybie zakupów.

    Dla każdego produktu mówi, ile dopisze przycisk i czy już jest na liście.
    """
    on_list = {item.name.casefold() for item in items}
    rows = []
    for product in PantryProduct.objects.select_related('group').all():
        add = shopping_item_for_product(product)
        rows.append({
            'product': product,
            'group': add['group'],
            'on_list': add['name'].casefold() in on_list,
            'add_name': add['name'],
            'add_quantity': add['quantity'],
            'add_unit': add['unit'],
            'add_unit_label': dict(PantryProduct.UNIT_CHOICES).get(add['unit'], add['unit']),
        })
    return rows


def get_shopping_form_context(request, **extra_context):
    context = {
        'categories': PANTRY_CATEGORIES,
        'units': PantryProduct.UNIT_CHOICES,
        'pantry_products': PantryProduct.objects.all(),
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


def pantry_cards_by_category(product_cards):
    """Kafelki spiżarni pogrupowane po kategorii - widok domyślny."""
    buckets = {}
    for card in product_cards:
        category = card['product'].category or 'Bez kategorii'
        buckets.setdefault(category, []).append(card)

    category_order = [*PANTRY_CATEGORIES, 'Bez kategorii']
    ordered = [category for category in category_order if category in buckets] + [
        category for category in sorted(buckets) if category not in category_order
    ]
    return [
        {
            'name': category,
            'cards': buckets[category],
            'count': len(buckets[category]),
            'low_count': _low_count(buckets[category]),
            'group': None,
            'open': index == 0,
        }
        for index, category in enumerate(ordered)
    ]


def pantry_cards_by_group(product_cards):
    """Kafelki spiżarni pogrupowane po grupie produktów.

    Panele są domyślnie zwinięte: w tym widoku liczy się stan całej grupy,
    a marki interesują dopiero wtedy, gdy ktoś je rozwinie.
    """
    buckets = {}
    for card in product_cards:
        group = card['product'].group
        buckets.setdefault(group.pk if group else None, {'group': group, 'cards': []})
        buckets[group.pk if group else None]['cards'].append(card)

    rows = []
    for bucket in buckets.values():
        group = bucket['group']
        if group is None:
            continue
        members = group.members()
        rows.append({
            'name': group.name,
            'cards': bucket['cards'],
            'count': len(bucket['cards']),
            'low_count': _low_count(bucket['cards']),
            'group': group,
            'packages': sum(packages_in_stock(member) for member in members),
            'status': group.stock_status,
            'open': False,
        })
    rows.sort(key=lambda row: row['name'].casefold())

    loose = buckets.get(None)
    if loose:
        rows.append({
            'name': 'Bez grupy',
            'cards': loose['cards'],
            'count': len(loose['cards']),
            'low_count': _low_count(loose['cards']),
            'group': None,
            'open': False,
        })
    return rows


def _low_count(cards):
    return sum(1 for card in cards if card.get('needs_restock'))


class PantryListView(LoginRequiredMixin, View):
    def get(self, request):
        today = timezone.localdate()
        all_products = list(PantryProduct.objects.select_related('group').prefetch_related(
            'group__products',
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
        shopping_weekday = get_household_typical_shopping_weekday()
        forecasts = forecast_pantry_products(
            all_products,
            today=today,
            shopping_weekday=shopping_weekday,
        )

        # Marka w grupie z zapasem nie jest brakiem - tak samo liczy lista zakupów.
        # Bez tego pusta Bakoma świeciła "Uzupełnij teraz", choć w lodówce stały
        # dwa inne jogurty, a lista (słusznie) jej nie dodawała.
        group_packages = {}
        for product in all_products:
            if product.group_id:
                group_packages[product.group_id] = group_packages.get(product.group_id, 0) + packages_in_stock(product)

        def covered_by_group(product):
            if not product.group_id:
                return False
            return group_packages.get(product.group_id, 0) > product.group.minimum_packages

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
                'group_covered': covered_by_group(product),
                'group_packages': group_packages.get(product.group_id, 0),
            }
            needs_restock = not card['group_covered'] and (
                product.stock_status in ['low', 'empty'] or forecast.is_due
            )
            card['needs_restock'] = needs_restock
            if status_filter == 'low':
                matches_status = needs_restock
            elif status_filter == 'ok':
                matches_status = not needs_restock
            else:
                matches_status = not status_filter or product.stock_status == status_filter
            if matches_status:
                product_cards.append(card)

        # Dwa sposoby patrzenia na tę samą spiżarnię: po kategoriach (jak dotąd)
        # albo po grupach „ten sam produkt, inna firma”.
        view_mode = 'grupy' if request.GET.get('widok') == 'grupy' else 'kategorie'
        if view_mode == 'grupy':
            product_groups = pantry_cards_by_group(product_cards)
        else:
            product_groups = pantry_cards_by_category(product_cards)

        movement_stats = PantryMovement.objects.aggregate(
            total_consumed=Sum('quantity', filter=Q(movement_type=PantryMovement.CONSUME)),
            total_restocked=Sum('quantity', filter=Q(movement_type=PantryMovement.PURCHASE)),
            movement_count=Count('id'),
        )

        loose_products = [
            product for product in all_products if not product.group_id
        ]
        context = {
            'product_cards': product_cards,
            'product_groups': product_groups,
            'view_mode': view_mode,
            'group_count': ProductGroup.objects.count(),
            # Filtry mają przeżyć przełączenie widoku, więc przenosimy je w adresie.
            'query_without_view': urlencode(
                [(key, value) for key, value in request.GET.items() if key != 'widok'],
            ),
            # Narzędzia grup pokazujemy tylko w widoku grup - w kategoriach
            # byłyby tylko szumem.
            'proposals': suggest_groups(loose_products) if view_mode == 'grupy' else [],
            'loose_products': sorted(loose_products, key=lambda item: item.name) if view_mode == 'grupy' else [],
            'categories': PANTRY_CATEGORIES,
            'units': PantryProduct.UNIT_CHOICES,
            'current_search': search_query,
            'current_status': status_filter,
            'current_category': category_filter,
            'product_count': len(product_cards),
            'has_any_products': bool(all_products),
            'filters_active': bool(search_query or status_filter or category_filter),
            'low_stock_count': sum(1 for card in product_cards if card['needs_restock']),
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

        product = PantryProduct.objects.filter(barcode=barcode).first()
        catalog = lookup_product_catalog(barcode)
        suggested_category = suggested_category_for_catalog_entry(catalog.entry)
        # Produkt bez kategorii albo w "Inne" dostaje lepszą podpowiedź, gdy
        # taka jest. Kategorii wybranej świadomie nie ruszamy.
        if (
            product
            and product.category.strip() in ['', PANTRY_CATEGORY_OTHER]
            and suggested_category
            and suggested_category != PANTRY_CATEGORY_OTHER
        ):
            previous_category = product.category
            updated = PantryProduct.objects.filter(
                pk=product.pk,
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
        product = PantryProduct.objects.filter(id=product_id).first()
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
                    replay.product.barcode != barcode
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
            catalog_entry = household_catalog_entry(barcode) or cached_open_food_facts_entry(barcode)
            catalog_category = suggested_category_for_catalog_entry(catalog_entry)
            category = (
                requested_category
                or catalog_category
                or suggest_category_from_name(name)
                or PANTRY_CATEGORY_OTHER
            )

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
                    replay.product.barcode != barcode
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
                barcode=barcode,
            ).first()
            if product_with_barcode:
                return JsonResponse(
                    {'ok': False, 'error': 'Ten kod jest już przypisany do produktu.'},
                    status=409,
                )

            product = PantryProduct.objects.select_for_update().filter(
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
                    created_by=request.user,
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
            # To, co domownik zatwierdził w formularzu, staje się podpowiedzią
            # dla każdego kolejnego skanu tego kodu.
            remember_household_product(
                barcode,
                name=product.name,
                category=product.category,
                unit=product.unit,
                quantity_per_scan=product.quantity_per_scan,
            )
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

            category = parse_pantry_category(request.POST.get('category'))
            if not category:
                suggested = suggest_category_from_name(name)
                category = suggested if suggested != PANTRY_CATEGORY_OTHER else ''

            group = ProductGroup.objects.filter(pk=request.POST.get('group') or 0).first()

            with transaction.atomic():
                product = PantryProduct.objects.create(
                    created_by=request.user,
                    name=name,
                    group=group,
                    barcode=barcode,
                    quantity_per_scan=quantity_per_scan,
                    category=category,
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
                if barcode:
                    remember_household_product(
                        barcode,
                        name=product.name,
                        category=product.category,
                        unit=product.unit,
                        quantity_per_scan=product.quantity_per_scan,
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


class EditPantryProductView(LoginRequiredMixin, View):
    """Pełna edycja produktu ze wspólnej spiżarni.

    Każdy domownik może zmienić każde pole zapisane w bazie. Skutki uboczne
    (przeliczenie historii przy zmianie jednostki, ruch "Korekta" przy zmianie
    stanu, pozycje na otwartych listach zakupów) obsługuje
    services/pantry_editing.py. Pole, którego nie ma w formularzu, zostaje
    bez zmian. Usuwanie produktu: DeletePantryProductView.
    """

    template_name = 'cooking/pantry_edit.html'
    editable_fields = [
        'name', 'barcode', 'category', 'unit', 'quantity_per_scan', 'current_quantity',
        'current_package_count', 'minimum_quantity', 'restock_lead_days', 'notes', 'group_id',
    ]

    def _initial_values(self, product):
        return {field: getattr(product, field) for field in self.editable_fields}

    def _context(self, product, form_values=None, status_note=''):
        values = self._initial_values(product)
        if form_values is not None:
            values.update({
                field: form_values.get(field)
                for field in self.editable_fields
                if field in form_values
            })
        remembered = household_catalog_entry(product.barcode) if product.barcode else None
        return get_pantry_form_context(
            product=product,
            form_values=values,
            remembered_entry=remembered,
            movement_count_label=polish_count(product.movements.count(), 'ruch', 'ruchy', 'ruchów'),
            open_shopping_item_count=product.shopping_items.filter(
                is_purchased=False,
            ).exclude(shopping_list__status=ShoppingList.COMPLETED).count(),
            # Dla JS formularza: które jednostki da się przeliczyć i jak.
            unit_meta={
                unit: {
                    'group': UNIT_BASE.get(unit, (unit, 1))[0],
                    'factor': str(UNIT_BASE.get(unit, (unit, 1))[1]),
                    'label': label,
                }
                for unit, label in PantryProduct.UNIT_CHOICES
            },
        )

    def get(self, request, product_id):
        product = get_object_or_404(
            PantryProduct.objects.select_related('created_by'),
            pk=product_id,
        )
        return render(request, self.template_name, self._context(product))

    def post(self, request, product_id):
        product = get_object_or_404(PantryProduct, pk=product_id)
        try:
            with transaction.atomic():
                locked = PantryProduct.objects.select_for_update().get(pk=product.pk)
                summary = self._save(request, locked)
        except ValueError as exc:
            return self._render_error(request, product_id, f'Nie udało się zapisać zmian: {exc}', 400)
        except IntegrityError:
            return self._render_error(
                request,
                product_id,
                'Nie udało się zapisać zmian: nazwa albo kod kreskowy należy już do innego produktu.',
                409,
            )
        messages.success(request, summary)
        return redirect('cooking:pantry')

    def _render_error(self, request, product_id, message, status):
        messages.error(request, message)
        product = get_object_or_404(
            PantryProduct.objects.select_related('created_by'),
            pk=product_id,
        )
        return render(
            request,
            self.template_name,
            self._context(product, form_values=request.POST),
            status=status,
        )

    def _save(self, request, product):
        post = request.POST
        old_name = product.name
        old_barcode = product.barcode
        old_category = product.category
        old_unit = product.unit
        old_quantity = product.current_quantity
        old_package_count = product.current_package_count
        old_image = product.image

        name = post.get('name', product.name).strip()
        if not name:
            raise ValueError('Nazwa produktu jest wymagana.')
        if len(name) > 160:
            raise ValueError('Nazwa produktu może mieć maksymalnie 160 znaków.')
        if PantryProduct.objects.filter(name__iexact=name).exclude(pk=product.pk).exists():
            raise ValueError('W spiżarni jest już produkt o tej nazwie.')

        barcode = (
            normalize_pantry_barcode(post.get('barcode'), required=False)
            if 'barcode' in post
            else product.barcode
        )
        if barcode:
            clash = PantryProduct.objects.filter(barcode=barcode).exclude(pk=product.pk).first()
            if clash:
                raise ValueError(f'Kod {barcode} jest już przypisany do produktu „{clash.name}”.')

        category = (
            parse_pantry_category(post.get('category'))
            if 'category' in post
            else product.category
        )

        unit = post.get('unit') or product.unit
        if unit not in dict(PantryProduct.UNIT_CHOICES):
            raise ValueError('Wybierz poprawną jednostkę.')
        unit_changed = unit != old_unit
        unit_convertible = units_are_convertible(old_unit, unit)

        def posted_quantity(field, current, label):
            if field not in post:
                return convert_to_unit(current, old_unit, unit)
            try:
                return parse_pantry_decimal(post.get(field))
            except ValueError as exc:
                raise ValueError(f'{label}: {exc}') from None

        quantity_per_scan = posted_quantity(
            'quantity_per_scan', product.quantity_per_scan, 'Wielkość opakowania',
        )
        if quantity_per_scan <= 0:
            raise ValueError('Wielkość opakowania musi być większa od zera.')
        current_quantity = posted_quantity('current_quantity', old_quantity, 'Stan')
        validate_pantry_storage_quantity(current_quantity)
        minimum_quantity = posted_quantity('minimum_quantity', product.minimum_quantity, 'Próg minimalny')
        for value in [quantity_per_scan, current_quantity, minimum_quantity]:
            validate_pantry_quantity_for_unit(value, unit)

        old_quantity_in_new_unit = convert_to_unit(old_quantity, old_unit, unit)
        old_size_in_new_unit = convert_to_unit(product.quantity_per_scan, old_unit, unit)
        stock_changed = current_quantity != old_quantity_in_new_unit
        packaging_changed = (
            stock_changed
            or quantity_per_scan != old_size_in_new_unit
            or unit_changed
            or barcode != old_barcode
        )
        will_track_packages = bool(
            barcode or unit in [PantryProduct.UNIT_PIECE, PantryProduct.UNIT_PACKAGE]
        )
        raw_package_count = post.get('current_package_count')
        if raw_package_count not in [None, '']:
            package_count = parse_package_count(raw_package_count)
        elif packaging_changed:
            package_count = (
                estimated_package_count(current_quantity, quantity_per_scan)
                if will_track_packages
                else 0
            )
        else:
            package_count = old_package_count
        if (packaging_changed or package_count != old_package_count) and (
            will_track_packages or package_count > 0
        ):
            expected = estimated_package_count(current_quantity, quantity_per_scan)
            if package_count != expected:
                unit_label = dict(PantryProduct.UNIT_CHOICES).get(unit, unit)
                raise ValueError(
                    f'Liczba opakowań ({package_count}) nie pasuje do stanu '
                    f'{polish_number(current_quantity)} {unit_label} przy opakowaniu '
                    f'{polish_number(quantity_per_scan)} {unit_label} - powinno być {expected}.'
                )

        try:
            restock_lead_days = int(post.get('restock_lead_days', product.restock_lead_days) or 3)
        except (TypeError, ValueError):
            raise ValueError('Wyprzedzenie zakupu musi być liczbą dni.') from None
        if restock_lead_days < 0 or restock_lead_days > 365:
            raise ValueError('Wyprzedzenie zakupu musi mieścić się w zakresie 0–365 dni.')

        new_image = validate_pantry_product_image(request.FILES.get('image'))
        remove_image = post.get('remove_image') == '1' and not new_image

        product.name = name
        product.barcode = barcode
        product.category = category
        product.unit = unit
        product.quantity_per_scan = quantity_per_scan
        product.current_quantity = current_quantity
        product.current_package_count = package_count
        product.minimum_quantity = minimum_quantity
        product.restock_lead_days = restock_lead_days
        if 'notes' in post:
            product.notes = post.get('notes', '').strip()
        if 'group' in post:
            raw_group = post.get('group') or ''
            product.group = ProductGroup.objects.filter(pk=raw_group).first() if raw_group else None
        if new_image:
            product.image = new_image
        elif remove_image:
            product.image = None
        product.save()

        if (new_image or remove_image) and old_image:
            delete_file_after_commit(old_image)

        converted_movements = 0
        if unit_changed:
            converted_movements = convert_movement_history(
                product, old_unit, unit, PantryMovement,
            )
        if stock_changed:
            unit_label = product.display_unit
            PantryMovement.objects.create(
                product=product,
                movement_type=PantryMovement.ADJUST,
                quantity=(current_quantity - old_quantity_in_new_unit).quantize(Decimal('0.01')),
                occurred_on=timezone.localdate(),
                note=(
                    f'{ADJUST_NOTE}: {polish_number(old_quantity_in_new_unit)} → '
                    f'{polish_number(current_quantity)} {unit_label}'
                )[:255],
            )
        sync_open_shopping_items(
            product,
            old_name=old_name,
            old_category=old_category,
            old_unit=old_unit,
            shopping_item_model=ShoppingListItem,
            shopping_list_model=ShoppingList,
        )
        if product.barcode:
            remember_household_product(
                product.barcode,
                name=product.name,
                category=product.category,
                unit=product.unit,
                quantity_per_scan=product.quantity_per_scan,
            )

        notes = []
        if unit_changed:
            if unit_convertible:
                notes.append(
                    f'historia przeliczona na {product.display_unit} '
                    f'({polish_count(converted_movements, "ruch", "ruchy", "ruchów")})'
                )
            else:
                notes.append('historia ruchów bez przeliczenia (inna jednostka)')
        if stock_changed:
            notes.append('zmiana stanu zapisana jako korekta')
        if product.barcode:
            notes.append('następny skan tego kodu podpowie te dane całemu domowi')
        summary = f'Zapisano: {product.name}.'
        if notes:
            details = '; '.join(notes)
            summary += f' {details[0].upper()}{details[1:]}.'
        return summary


class DeletePantryProductView(LoginRequiredMixin, View):
    """Całkowite usunięcie produktu razem z historią ruchów.

    Pozycje na listach zakupów zostają (tracą tylko powiązanie z produktem),
    a zamknięcie listy z taką pozycją utworzy produkt od nowa. Pamięć domu dla
    kodu kreskowego zostaje, chyba że użytkownik zaznaczy "zapomnij kod".
    """

    def post(self, request, product_id):
        product = get_object_or_404(PantryProduct, pk=product_id)
        if request.POST.get('confirm') != '1':
            messages.error(request, 'Zaznacz potwierdzenie, żeby usunąć produkt.')
            return redirect('cooking:edit-pantry-product', product_id=product.pk)

        forget_barcode = request.POST.get('forget_barcode') == '1'
        with transaction.atomic():
            product = get_object_or_404(PantryProduct.objects.select_for_update(), pk=product_id)
            name = product.name
            barcode = product.barcode
            movement_count = product.movements.count()
            image = product.image
            product.delete()
            if image:
                delete_file_after_commit(image)
            forgotten = 0
            if forget_barcode and barcode:
                forgotten, _ = ProductCatalogEntry.objects.filter(
                    source=ProductCatalogEntry.SOURCE_HOUSEHOLD,
                    lookup_barcode=barcode,
                ).delete()

        message = (
            f'Usunięto produkt „{name}” razem z historią '
            f'({polish_count(movement_count, "ruch", "ruchy", "ruchów")}).'
        )
        if barcode:
            message += (
                ' Kod zapomniany - następny skan zacznie od zera.'
                if forgotten
                else ' Kod zostaje w pamięci domu - następny skan podpowie nazwę i kategorię.'
            )
        messages.success(request, message)
        return redirect('cooking:pantry')


class PantryMovementView(LoginRequiredMixin, View):
    @transaction.atomic
    def post(self, request, product_id):
        product = get_object_or_404(
            PantryProduct.objects.select_for_update(),
            id=product_id,
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
        shopping_lists = ShoppingList.objects.annotate(
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
        suggestions = build_shopping_suggestions()

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
            created_by=request.user,
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
        suggestions = build_shopping_suggestions()
        if not suggestions:
            messages.info(request, 'Nie znaleziono produktów wymagających uzupełnienia.')
            return redirect('cooking:shopping-list')

        shopping_list = ShoppingList.objects.create(
            created_by=request.user,
            title=f'Automatyczna lista {timezone.localdate():%d.%m.%Y}',
            source=ShoppingList.AUTOMATIC,
        )
        for suggestion in suggestions:
            ShoppingListItem.objects.create(
                shopping_list=shopping_list,
                pantry_product=suggestion['product'],
                pantry_group=suggestion['group'],
                name=suggestion['name'],
                quantity=suggestion['quantity'],
                unit=suggestion['unit'],
                category=suggestion['category'],
                note=suggestion['reason'],
            )

        messages.success(request, f'Utworzono automatyczną listę z {len(suggestions)} pozycjami.')
        return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)


class ShoppingListDetailView(LoginRequiredMixin, View):
    def get(self, request, list_id):
        shopping_list = get_object_or_404(
            ShoppingList.objects.select_related('shop').prefetch_related('items__pantry_product'),
            id=list_id,
        )
        items = sort_items_by_shop(shopping_list, shopping_list.items.all())
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
            pantry_rows=pantry_rows_for_list(items),
        )
        return render(request, 'cooking/shopping_detail.html', context)


class EditShoppingListView(LoginRequiredMixin, View):
    def context(self, shopping_list, **extra):
        context = {'shopping_list': shopping_list, 'shops': ShopLayout.objects.all()}
        context.update(extra)
        return context

    def get(self, request, list_id):
        shopping_list = get_object_or_404(ShoppingList, id=list_id)
        return render(request, 'cooking/shopping_edit.html', self.context(shopping_list))

    def post(self, request, list_id):
        shopping_list = get_object_or_404(ShoppingList, id=list_id)
        title = request.POST.get('title', '').strip()
        if not title:
            messages.error(request, 'Nazwa listy jest wymagana.')
            return render(
                request,
                'cooking/shopping_edit.html',
                self.context(shopping_list, form_values=request.POST),
            )

        # Sklep decyduje o kolejności kategorii (kolejność alejek). Kolejność
        # ustawia się w trybie zakupów na telefonie, tutaj wybiera się sklep.
        new_shop = request.POST.get('new_shop', '').strip()
        shop_id = request.POST.get('shop', '').strip()
        if new_shop:
            shop = ShopLayout.objects.filter(name__iexact=new_shop).first()
            if shop is None:
                shop = ShopLayout.objects.create(name=new_shop[:80], created_by=request.user)
            shopping_list.shop = shop
        elif shop_id:
            shopping_list.shop = ShopLayout.objects.filter(pk=shop_id).first()
        else:
            shopping_list.shop = None

        shopping_list.title = title
        shopping_list.save(update_fields=['title', 'shop', 'updated_at'])
        messages.success(request, 'Zapisano listę.')
        return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)


class DeleteShoppingListView(LoginRequiredMixin, View):
    def post(self, request, list_id):
        shopping_list = get_object_or_404(ShoppingList, id=list_id)
        title = shopping_list.title
        shopping_list.delete()
        messages.success(request, f'Usunięto listę: {title}.')
        return redirect('cooking:shopping-list')


class AddShoppingListItemView(LoginRequiredMixin, View):
    def post(self, request, list_id):
        shopping_list = get_object_or_404(ShoppingList, id=list_id)
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
            product = find_pantry_product(name)
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


GROUPS_VIEW_URL = 'grupy'


def groups_redirect():
    """Widok grup mieszka w spiżarni, pod przełącznikiem Grupy/Kategorie."""
    return redirect(f"{reverse('cooking:pantry')}?widok={GROUPS_VIEW_URL}")


class ProductGroupListView(LoginRequiredMixin, View):
    """Stary adres grup - zostaje, żeby zapisane odnośniki dalej działały."""

    def get(self, request):
        return groups_redirect()


def _group_products(request, field='products'):
    ids = [value for value in request.POST.getlist(field) if value.isdigit()]
    return list(PantryProduct.objects.filter(pk__in=ids))


class CreateProductGroupView(LoginRequiredMixin, View):
    def post(self, request):
        name = request.POST.get('name', '').strip()
        products = _group_products(request)
        try:
            if not name:
                raise ValueError('Nazwa grupy jest wymagana.')
            if len(name) > 160:
                raise ValueError('Nazwa grupy może mieć maksymalnie 160 znaków.')
            if ProductGroup.objects.filter(name__iexact=name).exists():
                raise ValueError(f'Grupa „{name}” już istnieje.')
            if len(products) < 2:
                raise ValueError('Grupa ma sens dla co najmniej dwóch produktów.')
            minimum = parse_package_count(request.POST.get('minimum_packages'), default=1)
            with transaction.atomic():
                group = ProductGroup.objects.create(
                    name=name,
                    category=request.POST.get('category', '').strip() or products[0].category,
                    minimum_packages=minimum,
                    created_by=request.user,
                )
                PantryProduct.objects.filter(
                    pk__in=[product.pk for product in products],
                ).update(group=group)
        except ValueError as exc:
            messages.error(request, f'Nie udało się utworzyć grupy: {exc}')
        else:
            messages.success(
                request,
                f'Utworzono grupę „{group.name}” z {polish_count(len(products), "marki", "marek", "marek")}.',
            )
        return groups_redirect()


class UpdateProductGroupView(LoginRequiredMixin, View):
    def post(self, request, group_id):
        group = get_object_or_404(ProductGroup, pk=group_id)
        try:
            name = request.POST.get('name', group.name).strip()
            if not name:
                raise ValueError('Nazwa grupy jest wymagana.')
            if ProductGroup.objects.filter(name__iexact=name).exclude(pk=group.pk).exists():
                raise ValueError(f'Grupa „{name}” już istnieje.')
            minimum = parse_package_count(request.POST.get('minimum_packages'), default=group.minimum_packages)
            with transaction.atomic():
                group.name = name[:160]
                group.category = request.POST.get('category', group.category).strip()
                group.minimum_packages = minimum
                group.save(update_fields=['name', 'category', 'minimum_packages', 'updated_at'])
                added = _group_products(request, 'add_products')
                if added:
                    PantryProduct.objects.filter(
                        pk__in=[product.pk for product in added],
                    ).update(group=group)
                removed = _group_products(request, 'remove_products')
                if removed:
                    PantryProduct.objects.filter(
                        pk__in=[product.pk for product in removed], group=group,
                    ).update(group=None)
        except ValueError as exc:
            messages.error(request, f'Nie udało się zapisać grupy: {exc}')
        else:
            messages.success(request, f'Zapisano grupę „{group.name}”.')
        return groups_redirect()


class DeleteProductGroupView(LoginRequiredMixin, View):
    def post(self, request, group_id):
        group = get_object_or_404(ProductGroup, pk=group_id)
        name = group.name
        # Marki zostają w spiżarni, tracą tylko przynależność do grupy.
        group.delete()
        messages.success(request, f'Usunięto grupę „{name}”. Produkty zostały w spiżarni.')
        return groups_redirect()


class AddPantryProductToShoppingListView(LoginRequiredMixin, View):
    """Dopisuje produkt ze spiżarni do listy jednym przyciskiem.

    To samo, co przycisk w trybie zakupów na telefonie: ilość i jednostkę
    wybiera serwer, więc produkt ważony trafia na listę jako „1 szt.”.
    """

    def post(self, request, list_id, product_id):
        shopping_list = get_object_or_404(ShoppingList, id=list_id)
        product = get_object_or_404(PantryProduct, id=product_id)
        if shopping_list.status == ShoppingList.COMPLETED:
            messages.info(request, 'Ta lista została już zakończona.')
            return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)

        add = shopping_item_for_product(product)
        already = shopping_list.items.filter(name__iexact=add['name']).first()
        if already is not None:
            messages.info(request, f'{add["name"]} już jest na liście.')
            return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)

        ShoppingListItem.objects.create(
            shopping_list=shopping_list,
            pantry_product=None if add['group'] else product,
            pantry_group=add['group'],
            name=add['name'],
            quantity=add['quantity'],
            unit=add['unit'],
            category=add['category'],
        )
        shopping_list.save(update_fields=['updated_at'])
        messages.success(request, f'Dodano do listy: {add["name"]}.')
        return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)


class UpdateShoppingListItemView(LoginRequiredMixin, View):
    def post(self, request, item_id):
        item = get_object_or_404(ShoppingListItem, id=item_id)
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
            product = find_pantry_product(name)

            with transaction.atomic():
                quantity_changed = quantity != item.quantity or unit != item.unit
                item.name = name
                item.quantity = quantity
                item.unit = unit
                item.category = request.POST.get('category', '').strip() or (product.category if product else '')
                item.note = request.POST.get('note', '').strip()
                item.pantry_product = product
                if quantity_changed:
                    item.quantity_changed_at = timezone.now()
                # Odhaczona pozycja już uzupełniła spiżarnię - poprawiamy ten zakup.
                warning = refresh_pantry_purchase(item, user=request.user)
                item.save(update_fields=[
                    'name',
                    'quantity',
                    'unit',
                    'category',
                    'note',
                    'pantry_product',
                    'pantry_movement',
                    'quantity_changed_at',
                    'updated_at',
                ])
                shopping_list.save(update_fields=['updated_at'])
            messages.success(request, f'Zapisano pozycję: {item.name}.')
            if warning:
                messages.warning(request, warning)
        except Exception as exc:
            messages.error(request, f'Nie udało się zapisać pozycji: {exc}')

        return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)


class ToggleShoppingListItemView(LoginRequiredMixin, View):
    def post(self, request, item_id):
        item = get_object_or_404(ShoppingListItem, id=item_id)
        if item.shopping_list.status == ShoppingList.COMPLETED:
            messages.info(request, 'Ta lista została już zakończona.')
            return redirect('cooking:shopping-list-detail', list_id=item.shopping_list.id)

        with transaction.atomic():
            item = ShoppingListItem.objects.select_for_update(of=('self',)).select_related(
                'shopping_list', 'pantry_movement',
            ).get(pk=item.pk)
            # Produkt ze spiżarni uzupełnia się od razu po odhaczeniu.
            warning = set_item_purchased(item, not item.is_purchased, user=request.user)
            item.shopping_list.save(update_fields=['updated_at'])
        if warning:
            messages.warning(request, warning)
        return redirect('cooking:shopping-list-detail', list_id=item.shopping_list.id)


class DeleteShoppingListItemView(LoginRequiredMixin, View):
    def post(self, request, item_id):
        item = get_object_or_404(ShoppingListItem, id=item_id)
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
    def post(self, request, list_id):
        with transaction.atomic():
            shopping_list = get_object_or_404(ShoppingList.objects.select_for_update(), id=list_id)
            if shopping_list.status == ShoppingList.COMPLETED:
                messages.info(request, 'Ta lista została już zakończona.')
                return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)
            added, already, errors = complete_shopping_list(shopping_list, user=request.user)

        if errors:
            for error in errors[:5]:
                messages.error(request, error)
            if len(errors) > 5:
                messages.error(request, f'Pozostałe błędy: {len(errors) - 5}.')
            return redirect('cooking:shopping-list-detail', list_id=shopping_list.id)
        messages.success(request, completion_message(added, already))
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
            'pantry_products': PantryProduct.objects.all(),
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

                product = PantryProduct.objects.filter(name__iexact=name).first()
                fulfilled_quantity = Decimal('0.00')
                before_package_count = 0
                if product is None:
                    validate_pantry_quantity_for_unit(quantity, source_unit)
                    product = PantryProduct.objects.create(
                        created_by=request.user,
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
                    'pantry_products': PantryProduct.objects.all(),
                    'units': PantryProduct.UNIT_CHOICES,
                    'categories': PANTRY_CATEGORIES,
                    'form_rows': zip(product_names, quantities, units, categories),
                })

        return redirect('cooking:pantry')
