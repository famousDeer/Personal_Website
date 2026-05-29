# cooking/models.py
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING

from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone

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

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='pantry_products')
    name = models.CharField(max_length=160)
    category = models.CharField(max_length=120, blank=True)
    unit = models.CharField(max_length=10, choices=UNIT_CHOICES, default=UNIT_PIECE)
    current_quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    minimum_quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    restock_lead_days = models.PositiveSmallIntegerField(default=3)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pantry_products'
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(fields=['user', 'name'], name='unique_user_pantry_product_name'),
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

    def average_daily_consumption(self, days=30):
        since = timezone.localdate() - timedelta(days=days)
        total = self.movements.filter(
            movement_type=PantryMovement.CONSUME,
            occurred_on__gte=since,
        ).aggregate(total=models.Sum('quantity'))['total'] or Decimal('0')
        return total / Decimal(days)

    def projected_depletion_date(self, days=30):
        average = self.average_daily_consumption(days=days)
        if average <= 0:
            return None
        days_left = self.current_quantity / average
        return timezone.localdate() + timedelta(days=int(days_left.to_integral_value(rounding=ROUND_CEILING)))

    def suggested_restock_date(self, days=30):
        depletion_date = self.projected_depletion_date(days=days)
        if depletion_date is None:
            return None
        return depletion_date - timedelta(days=self.restock_lead_days)


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
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    occurred_on = models.DateField(default=timezone.localdate)
    note = models.CharField(max_length=255, blank=True)
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

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='shopping_lists')
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
