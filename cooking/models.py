# cooking/models.py
import uuid
from decimal import Decimal

from django.db import models
from django.db.models.functions import Lower
from django.contrib.auth import get_user_model
from django.utils import timezone

from .storage import private_media_storage

User = get_user_model()

UNIT_PIECE = 'szt'
UNIT_GRAM = 'g'
UNIT_KILOGRAM = 'kg'
UNIT_MILLILITER = 'ml'
UNIT_LITER = 'l'
UNIT_PACKAGE = 'opak'
PANTRY_UNIT_CHOICES = [
    (UNIT_PIECE, 'szt.'),
    (UNIT_GRAM, 'g'),
    (UNIT_KILOGRAM, 'kg'),
    (UNIT_MILLILITER, 'ml'),
    (UNIT_LITER, 'l'),
    (UNIT_PACKAGE, 'opak.'),
]

# Recipe database
class Recipe(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='recipes')
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    ingredients = models.TextField()
    instructions = models.TextField()
    portions = models.PositiveIntegerField(default=1)
    kcal = models.PositiveIntegerField(default=0)
    preparation_time = models.PositiveIntegerField(help_text="Preparation time in minutes", default=5)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    kitchen_region = models.CharField(max_length=100, blank=True)
    meal_type = models.CharField(max_length=100, blank=True)
    type_of_dish = models.CharField(max_length=100, blank=True)
    image = models.ImageField(upload_to='recipes_img', blank=True, null=True)

    class Meta:
        db_table = 'recipes'
        ordering = ['-created_at']
        verbose_name = "Recipe"
        verbose_name_plural = "Recipes"
    
    def __str__(self):
        return f"{self.title} by {self.user.username}"


class RecipeStep(models.Model):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name='steps')
    order = models.PositiveIntegerField(default=1)
    title = models.CharField(max_length=160, blank=True)
    instruction = models.TextField()
    mix_after = models.BooleanField(default=False)
    duration_minutes = models.PositiveIntegerField(blank=True, null=True)

    class Meta:
        db_table = 'recipe_steps'
        ordering = ['order', 'id']

    def __str__(self):
        return f"{self.recipe.title} - krok {self.order}"


class RecipeStepIngredient(models.Model):
    step = models.ForeignKey(RecipeStep, on_delete=models.CASCADE, related_name='ingredients')
    order = models.PositiveIntegerField(default=1)
    name = models.CharField(max_length=160)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    unit = models.CharField(max_length=10, choices=PANTRY_UNIT_CHOICES, default=UNIT_GRAM)
    category = models.CharField(max_length=120, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = 'recipe_step_ingredients'
        ordering = ['step__order', 'order', 'id']

    def __str__(self):
        return f"{self.name} ({self.quantity} {self.unit})"


class PantryProduct(models.Model):
    UNIT_PIECE = UNIT_PIECE
    UNIT_GRAM = UNIT_GRAM
    UNIT_KILOGRAM = UNIT_KILOGRAM
    UNIT_MILLILITER = UNIT_MILLILITER
    UNIT_LITER = UNIT_LITER
    UNIT_PACKAGE = UNIT_PACKAGE
    UNIT_CHOICES = PANTRY_UNIT_CHOICES

    # Spiżarnia jest wspólna dla całego domu: każdy zalogowany użytkownik widzi
    # i edytuje wszystkie produkty. Pole mówi tylko, kto dodał produkt.
    # Kolumna zostaje "user_id", więc kod i baza sprzed zmiany dalej do siebie
    # pasują (to pozwala uruchomić podgląd łączenia duplikatów przed migracją).
    # SET_NULL: usunięcie konta domownika nie kasuje wspólnych zapasów.
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pantry_products',
        db_column='user_id',
        verbose_name='Dodane przez',
    )
    name = models.CharField(max_length=160)
    barcode = models.CharField(max_length=64, blank=True)
    quantity_per_scan = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('1.00'))
    category = models.CharField(max_length=120, blank=True)
    unit = models.CharField(max_length=10, choices=UNIT_CHOICES, default=UNIT_PIECE)
    current_quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    current_package_count = models.PositiveIntegerField(default=0)
    minimum_quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    restock_lead_days = models.PositiveSmallIntegerField(default=3)
    notes = models.TextField(blank=True)
    image = models.ImageField(
        upload_to='pantry_product_images',
        storage=private_media_storage,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pantry_products'
        ordering = ['name']
        constraints = [
            # Jedna spiżarnia = jedna pozycja na nazwę (bez względu na wielkość
            # liter) i jedna na kod kreskowy.
            models.UniqueConstraint(Lower('name'), name='unique_pantry_product_name_ci'),
            models.UniqueConstraint(
                fields=['barcode'],
                condition=~models.Q(barcode=''),
                name='unique_pantry_product_barcode',
            ),
            models.CheckConstraint(
                condition=models.Q(quantity_per_scan__gt=0),
                name='positive_pantry_product_quantity_per_scan',
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def display_unit(self):
        return dict(self.UNIT_CHOICES).get(self.unit, self.unit)

    @property
    def stock_status(self):
        if self.current_quantity <= 0:
            return 'empty'
        if self.minimum_quantity and self.current_quantity <= self.minimum_quantity:
            return 'low'
        return 'ok'

    @property
    def tracks_packages(self):
        return bool(
            self.barcode
            or self.current_package_count > 0
            or self.unit in [self.UNIT_PIECE, self.UNIT_PACKAGE]
        )

    @property
    def package_count_is_known(self):
        return self.tracks_packages

    def _pantry_forecast(self, days=90):
        from .services.pantry_forecast import forecast_pantry_product

        return forecast_pantry_product(self, max_history_days=max(int(days), 14))

    def average_daily_consumption(self, days=90):
        return self._pantry_forecast(days=days).rate

    def projected_depletion_date(self, days=90):
        return self._pantry_forecast(days=days).minimum_date_to

    def suggested_restock_date(self, days=90):
        return self._pantry_forecast(days=days).buy_date


class ProductCatalogEntry(models.Model):
    SOURCE_OPEN_FOOD_FACTS = 'open_food_facts'
    # Nazwa, kategoria i jednostka potwierdzone przez domowników. Wpis
    # powstaje, gdy ktoś doda produkt ze skanera albo poprawi produkt z kodem,
    # i ma pierwszeństwo przed Open Food Facts - również po usunięciu produktu
    # ze spiżarni i dla każdego użytkownika tej instalacji. Nie wygasa.
    SOURCE_HOUSEHOLD = 'household'
    SOURCE_CHOICES = [
        (SOURCE_OPEN_FOOD_FACTS, 'Open Food Facts'),
        (SOURCE_HOUSEHOLD, 'Zapamiętane w domu'),
    ]

    STATUS_PENDING = 'pending'
    STATUS_FOUND = 'found'
    STATUS_NOT_FOUND = 'not_found'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Oczekuje na pobranie'),
        (STATUS_FOUND, 'Znaleziony'),
        (STATUS_NOT_FOUND, 'Nie znaleziony'),
    ]

    source = models.CharField(
        max_length=40,
        choices=SOURCE_CHOICES,
        default=SOURCE_OPEN_FOOD_FACTS,
    )
    lookup_barcode = models.CharField(max_length=64)
    canonical_barcode = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    product_type = models.CharField(max_length=20, blank=True)
    product_name = models.CharField(max_length=160, blank=True)
    # Język, z którego pochodzi product_name ("pl", "en", "de"...). Pusty,
    # gdy baza go nie podała. Formularz skanera prosi o polską nazwę, gdy
    # podpowiedź jest w innym języku.
    name_language = models.CharField(max_length=8, blank=True)
    brand = models.CharField(max_length=160, blank=True)
    description = models.TextField(blank=True)
    ingredients = models.TextField(blank=True)
    external_category = models.CharField(max_length=255, blank=True)
    suggested_category = models.CharField(max_length=120, blank=True)
    quantity_text = models.CharField(max_length=80, blank=True)
    suggested_quantity_per_scan = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
    )
    suggested_unit = models.CharField(max_length=10, choices=PANTRY_UNIT_CHOICES, blank=True)
    image = models.ImageField(upload_to='pantry_catalog_images', blank=True)
    image_source_url = models.URLField(max_length=500, blank=True)
    attribution_url = models.URLField(max_length=500, blank=True)
    source_updated_at = models.DateTimeField(blank=True, null=True)
    fetched_at = models.DateTimeField(blank=True, null=True)
    valid_until = models.DateTimeField(blank=True, null=True, db_index=True)
    refresh_started_at = models.DateTimeField(blank=True, null=True)
    retry_after = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'product_catalog_entries'
        ordering = ['product_name', 'lookup_barcode']
        constraints = [
            models.UniqueConstraint(
                fields=['source', 'lookup_barcode'],
                name='unique_product_catalog_source_barcode',
            ),
        ]
        indexes = [
            models.Index(fields=['source', 'valid_until'], name='product_catalog_valid_idx'),
        ]

    def __str__(self):
        return self.product_name or self.lookup_barcode


class ProductCatalogQuota(models.Model):
    source = models.CharField(max_length=40, unique=True)
    window_started_at = models.DateTimeField(default=timezone.now)
    request_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'product_catalog_quotas'

    def __str__(self):
        return f'{self.source}: {self.request_count}'


class PantryMovement(models.Model):
    PURCHASE = 'purchase'
    CONSUME = 'consume'
    ADJUST = 'adjust'
    MOVEMENT_TYPE_CHOICES = [
        (PURCHASE, 'Uzupełnienie'),
        (CONSUME, 'Zużycie'),
        (ADJUST, 'Korekta'),
    ]

    product = models.ForeignKey(PantryProduct, on_delete=models.CASCADE, related_name='movements')
    movement_type = models.CharField(max_length=20, choices=MOVEMENT_TYPE_CHOICES)
    # ``quantity`` is the amount that actually changed the stock. The requested
    # values retain the user's intent for idempotency and stockout diagnostics.
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    package_count = models.PositiveIntegerField(blank=True, null=True)
    requested_quantity = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    requested_package_count = models.PositiveIntegerField(blank=True, null=True)
    stock_was_insufficient = models.BooleanField(default=False)
    occurred_on = models.DateField(default=timezone.localdate)
    note = models.CharField(max_length=255, blank=True)
    scan_id = models.UUIDField(blank=True, null=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pantry_movements'
        ordering = ['-occurred_on', '-created_at']

    def __str__(self):
        return f"{self.product.name}: {self.get_movement_type_display()} {self.quantity} {self.product.unit}"


class ShoppingList(models.Model):
    MANUAL = 'manual'
    AUTOMATIC = 'automatic'
    SOURCE_CHOICES = [
        (MANUAL, 'Ręczna'),
        (AUTOMATIC, 'Automatyczna'),
    ]

    ACTIVE = 'active'
    COMPLETED = 'completed'
    STATUS_CHOICES = [
        (ACTIVE, 'Aktywna'),
        (COMPLETED, 'Zakończona'),
    ]

    # Listy zakupów są wspólne, tak jak spiżarnia, z której powstają.
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='shopping_lists',
        db_column='user_id',
        verbose_name='Utworzona przez',
    )
    title = models.CharField(max_length=180)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=MANUAL)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'shopping_lists'
        ordering = ['-updated_at', '-created_at']

    def __str__(self):
        return self.title


class ShoppingListItem(models.Model):
    shopping_list = models.ForeignKey(ShoppingList, on_delete=models.CASCADE, related_name='items')
    pantry_product = models.ForeignKey(
        PantryProduct,
        on_delete=models.SET_NULL,
        related_name='shopping_items',
        blank=True,
        null=True,
    )
    name = models.CharField(max_length=160)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('1.00'))
    unit = models.CharField(max_length=10, choices=PANTRY_UNIT_CHOICES, default=UNIT_PIECE)
    category = models.CharField(max_length=120, blank=True)
    note = models.CharField(max_length=255, blank=True)
    is_purchased = models.BooleanField(default=False)
    # Stały identyfikator pozycji, także dla telefonu. Pozycja dopisana offline
    # dostaje go od razu na telefonie, więc ponowna synchronizacja nie tworzy
    # duplikatu, a kolejne zmiany (ilość, odhaczenie) trafiają we właściwą pozycję.
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    purchased_at = models.DateTimeField(blank=True, null=True)
    purchased_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        verbose_name='Kupione przez',
    )
    # Ruch w spiżarni, którym odhaczenie uzupełniło zapas. Cofnięcie odhaczenia
    # usuwa ruch i zdejmuje ilość ze stanu; zakończenie listy go pomija.
    pantry_movement = models.OneToOneField(
        'PantryMovement',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='shopping_item',
    )
    # Czas zmiany zapisany przez urządzenie ("ostatnia zmiana wygrywa"), osobno
    # dla odhaczenia i dla ilości, żeby jedna zmiana nie unieważniała drugiej.
    purchased_changed_at = models.DateTimeField(blank=True, null=True)
    quantity_changed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'shopping_list_items'
        ordering = ['is_purchased', 'category', 'name']

    def __str__(self):
        return f"{self.name} ({self.quantity} {self.unit})"

    @property
    def display_unit(self):
        return dict(PANTRY_UNIT_CHOICES).get(self.unit, self.unit)


class ShoppingSyncOperation(models.Model):
    """Operacja z kolejki telefonu, już przetworzona przez serwer.

    Telefon może wysłać tę samą paczkę dwa razy (zerwane połączenie w trakcie
    odpowiedzi). Serwer zapamiętuje wynik każdej operacji po jej identyfikatorze
    i przy powtórce zwraca go zamiast wykonywać operację drugi raz.
    """

    op_id = models.UUIDField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    op_type = models.CharField(max_length=40)
    status = models.CharField(max_length=20)
    message = models.CharField(max_length=255, blank=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'shopping_sync_operations'
        ordering = ['-received_at']

    def __str__(self):
        return f'{self.op_type} {self.op_id} ({self.status})'
